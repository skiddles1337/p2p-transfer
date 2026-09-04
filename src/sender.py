"""
sender.py

Connects to a listener and runs a full SESSION:
  1. HELLO handshake - receive a random challenge, respond with proof
     of knowing the shared passphrase (HMAC), without ever sending the
     passphrase itself
  2. For each file in FILES_TO_SEND: offer it, wait for accept/reject,
     send it (chunked, hashed) if accepted
  3. Send BYE, close the connection

Handshake detail: the listener sends a random CHALLENGE. We prove we
know the shared passphrase by computing HMAC-SHA256(challenge, key=
passphrase) and sending that back - the passphrase itself never
touches the wire. This is a meaningfully stronger design than just
sending the passphrase directly: even someone capturing this entire
exchange can't recover the passphrase from it, since HMAC is a one-way
function (its whole design goal is being infeasible to reverse).
"""

import socket
import os
import hashlib
from protocol import (
    pack_message,
    recv_message,
    pack_file_offer,
    pack_file_chunk,
    MSG_HELLO,
    MSG_HELLO_RESPONSE,
    MSG_HELLO_OK,
    MSG_HELLO_REJECT,
    MSG_FILE_OFFER,
    MSG_FILE_ACCEPT,
    MSG_FILE_REJECT,
    MSG_FILE_CHUNK,
    MSG_DONE,
    MSG_BYE,
    CHUNK_SIZE,
    TRANSFER_ID_LEN,
)
from auth import compute_response

TARGET_IP = "127.0.0.1"
TARGET_PORT = 5001

# Edit this list to test sending multiple files in one session.
FILES_TO_SEND = ["test_file1.txt", "test_file2.txt", "test_file3.txt"]


def do_handshake(sock) -> bool:
    """
    Wait for the listener's challenge, respond with proof of knowing
    the shared passphrase. Returns True if the listener accepted it.
    """
    msg_type, challenge = recv_message(sock)
    if msg_type != MSG_HELLO:
        print(f"Expected HELLO, got message type {msg_type}.")
        return False

    response = compute_response(challenge)
    sock.sendall(pack_message(MSG_HELLO_RESPONSE, response))

    msg_type, _ = recv_message(sock)
    if msg_type == MSG_HELLO_OK:
        print("Handshake OK.")
        return True
    else:
        print("Handshake REJECTED by listener - passphrase mismatch?")
        return False


def send_file(sock, filepath: str) -> None:
    """
    Offer one file, and if accepted, send it in hashed chunks followed
    by DONE. Same logic as previous steps.
    """
    filename = os.path.basename(filepath)
    filesize = os.path.getsize(filepath)
    transfer_id = os.urandom(TRANSFER_ID_LEN)

    print(f"Offering '{filename}' ({filesize} bytes), transfer_id={transfer_id.hex()}")
    offer_payload = pack_file_offer(filename, filesize, transfer_id)
    sock.sendall(pack_message(MSG_FILE_OFFER, offer_payload))

    msg_type, _ = recv_message(sock)
    if msg_type == MSG_FILE_REJECT:
        print(f"'{filename}' was rejected by the peer. Skipping.")
        return
    elif msg_type != MSG_FILE_ACCEPT:
        print(f"Unexpected response to offer: message type {msg_type}. Skipping.")
        return

    print(f"'{filename}' accepted - sending...")

    whole_file_hasher = hashlib.sha256()
    bytes_sent = 0
    chunk_index = 0

    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if chunk == b"":
                break

            whole_file_hasher.update(chunk)
            chunk_payload = pack_file_chunk(chunk_index, chunk)
            sock.sendall(pack_message(MSG_FILE_CHUNK, chunk_payload))

            bytes_sent += len(chunk)
            print(f"  Sent chunk {chunk_index} ({len(chunk)} bytes, "
                  f"{bytes_sent}/{filesize} total)")
            chunk_index += 1

    final_hash = whole_file_hasher.digest()
    sock.sendall(pack_message(MSG_DONE, final_hash))
    print(f"Done sending '{filename}'. {chunk_index} chunks, {bytes_sent} bytes.")


def main():
    for filepath in FILES_TO_SEND:
        if not os.path.exists(filepath):
            print(f"Error: '{filepath}' not found. Aborting.")
            return

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    print(f"Connecting to {TARGET_IP}:{TARGET_PORT}...")
    client_socket.connect((TARGET_IP, TARGET_PORT))
    print("Connected.")

    if not do_handshake(client_socket):
        print("Handshake failed. Closing connection.")
        client_socket.close()
        return

    for filepath in FILES_TO_SEND:
        send_file(client_socket, filepath)

    client_socket.sendall(pack_message(MSG_BYE, b""))
    print("Sent BYE.")

    client_socket.close()
    print("Connection closed.")


if __name__ == "__main__":
    main()
