"""
sender.py

Connects to a listener and runs a full SESSION:
  1. Authenticated key exchange (X25519 Diffie-Hellman, confirmed via
     HMAC tags derived from the shared passphrase)
  2. For each file in FILES_TO_SEND: offer it, wait for accept/reject,
     send it (chunked, hashed, encrypted) if accepted
  3. Send BYE, close the connection
"""

import socket
import os
import hashlib
from cryptography.fernet import Fernet
from protocol import (
    pack_message,
    recv_message,
    pack_file_offer,
    pack_file_chunk,
    pack_hello_response,
    unpack_hello_response,
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
from auth import compute_confirmation_tag, verify_confirmation_tag
from keyexchange import (
    generate_keypair,
    public_key_to_bytes,
    public_key_from_bytes,
    compute_shared_secret,
    derive_session_key,
)
from logger import log

TARGET_IP = "127.0.0.1"
TARGET_PORT = 5001

FILES_TO_SEND = ["test_file.txt"]


def do_handshake(sock):
    """
    Perform the authenticated key exchange from the sender's side.
    Returns a Fernet instance on success, or None on failure.
    """
    msg_type, listener_public_bytes = recv_message(sock)
    if msg_type != MSG_HELLO:
        log(f"Expected HELLO, got message type {msg_type}.")
        return None

    sender_private, sender_public = generate_keypair()
    sender_public_bytes = public_key_to_bytes(sender_public)

    tag = compute_confirmation_tag(listener_public_bytes, sender_public_bytes)
    response_payload = pack_hello_response(sender_public_bytes, tag)
    sock.sendall(pack_message(MSG_HELLO_RESPONSE, response_payload))

    msg_type, listener_tag = recv_message(sock)
    if msg_type != MSG_HELLO_OK:
        log("Handshake REJECTED by listener - passphrase mismatch?")
        return None

    if not verify_confirmation_tag(listener_tag, sender_public_bytes, listener_public_bytes):
        log("Handshake FAILED - listener's confirmation tag is invalid. "
            "Possible tampering - aborting.")
        return None

    listener_public = public_key_from_bytes(listener_public_bytes)
    shared_secret = compute_shared_secret(sender_private, listener_public)
    session_key = derive_session_key(shared_secret)

    log("Handshake OK - key exchange authenticated, session key established.")
    return Fernet(session_key)


def send_file(sock, filepath: str, fernet: Fernet) -> None:
    """
    Offer one file, and if accepted, send it in hashed, encrypted
    chunks followed by DONE.
    """
    filename = os.path.basename(filepath)
    filesize = os.path.getsize(filepath)
    transfer_id = os.urandom(TRANSFER_ID_LEN)

    log(f"Offering '{filename}' ({filesize} bytes), transfer_id={transfer_id.hex()}")
    offer_payload = pack_file_offer(filename, filesize, transfer_id)
    sock.sendall(pack_message(MSG_FILE_OFFER, offer_payload))

    msg_type, _ = recv_message(sock)
    if msg_type == MSG_FILE_REJECT:
        log(f"'{filename}' was rejected by the peer. Skipping.")
        return
    elif msg_type != MSG_FILE_ACCEPT:
        log(f"Unexpected response to offer: message type {msg_type}. Skipping.")
        return

    log(f"'{filename}' accepted - sending...")

    whole_file_hasher = hashlib.sha256()
    bytes_sent = 0
    chunk_index = 0

    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if chunk == b"":
                break

            whole_file_hasher.update(chunk)
            chunk_hash = hashlib.sha256(chunk).digest()
            encrypted_chunk = fernet.encrypt(chunk)

            chunk_payload = pack_file_chunk(chunk_index, chunk_hash, encrypted_chunk)
            sock.sendall(pack_message(MSG_FILE_CHUNK, chunk_payload))

            bytes_sent += len(chunk)
            log(f"  Sent chunk {chunk_index} ({len(chunk)} plaintext bytes, "
                f"{len(encrypted_chunk)} encrypted, {bytes_sent}/{filesize} total)")
            chunk_index += 1

    final_hash = whole_file_hasher.digest()
    sock.sendall(pack_message(MSG_DONE, final_hash))
    log(f"Done sending '{filename}'. {chunk_index} chunks, {bytes_sent} bytes.")


def main():
    for filepath in FILES_TO_SEND:
        if not os.path.exists(filepath):
            log(f"Error: '{filepath}' not found. Aborting.")
            return

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # connect() is the first point failure is likely if the peer isn't
    # actually reachable - their app isn't running, the port isn't
    # forwarded, a typo in the IP, etc. Without this, the person would
    # see a raw ConnectionRefusedError traceback instead of a clear
    # explanation of what to check.
    log(f"Connecting to {TARGET_IP}:{TARGET_PORT}...")
    try:
        client_socket.connect((TARGET_IP, TARGET_PORT))
    except ConnectionRefusedError:
        log(f"Connection refused by {TARGET_IP}:{TARGET_PORT}. "
            f"Is the listener running? Is the port forwarded correctly?")
        return
    except socket.timeout:
        log(f"Connection to {TARGET_IP}:{TARGET_PORT} timed out. "
            f"Check the address and that the port is reachable.")
        return
    except OSError as e:
        log(f"Could not connect to {TARGET_IP}:{TARGET_PORT}: {e}")
        return

    log("Connected.")

    # Everything from here on can also fail if the connection drops
    # partway through (peer closes the app, network hiccup, etc.) -
    # wrapping the rest of the session in one try/except means a
    # dropped connection produces a clear message and a clean exit,
    # rather than a raw traceback.
    try:
        fernet = do_handshake(client_socket)
        if fernet is None:
            log("Handshake failed. Closing connection.")
            return

        for filepath in FILES_TO_SEND:
            send_file(client_socket, filepath, fernet)

        client_socket.sendall(pack_message(MSG_BYE, b""))
        log("Sent BYE.")

    except (ConnectionError, OSError) as e:
        log(f"Connection lost during session: {e}")

    finally:
        client_socket.close()
        log("Connection closed.")


if __name__ == "__main__":
    main()
