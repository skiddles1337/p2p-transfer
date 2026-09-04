"""
listener.py

Listens for connections and handles each one as a persistent SESSION:
  1. HELLO handshake - prove the connecting peer knows the shared
     passphrase, without either side ever transmitting it
  2. loop: receive FILE_OFFER -> prompt to accept/reject -> if
     accepted, receive the file (as in previous steps); repeat for as
     many files as the sender sends
  3. BYE - sender signals it's done; connection closes

After a session ends, the listener goes back to waiting for the next
connection, rather than exiting - matching the "always listening"
design goal.
"""

import socket
import os
import hmac
import hashlib
from protocol import (
    recv_message,
    pack_message,
    unpack_file_offer,
    unpack_file_chunk,
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
    CHALLENGE_LEN,
    CHUNK_SIZE,
)
from auth import compute_response
from storage import staging_paths, write_manifest, finalize_transfer, STAGING_DIR

LISTEN_PORT = 5001
SAVE_DIR = "received"


def preallocate_file(path: str, filesize: int) -> None:
    with open(path, "wb") as f:
        if filesize > 0:
            f.seek(filesize - 1)
            f.write(b"\x00")


def do_handshake(conn) -> bool:
    """
    Send a random challenge, verify the peer's response proves they
    know the shared passphrase. Returns True if the handshake
    succeeded, False otherwise (caller should close the connection on
    False).
    """
    challenge = os.urandom(CHALLENGE_LEN)
    conn.sendall(pack_message(MSG_HELLO, challenge))

    msg_type, payload = recv_message(conn)
    if msg_type != MSG_HELLO_RESPONSE:
        print(f"Expected HELLO_RESPONSE, got message type {msg_type}.")
        conn.sendall(pack_message(MSG_HELLO_REJECT, b""))
        return False

    expected_response = compute_response(challenge)

    # hmac.compare_digest instead of == : a plain == comparison on
    # secret-derived values can, in principle, leak timing information
    # (it often returns as soon as the first mismatched byte is found,
    # so comparing early-differing values is faster than comparing
    # values that match for a while first). compare_digest always
    # takes the same amount of time regardless of where a mismatch
    # occurs, closing off that side-channel. Good habit any time
    # you're comparing something that gates access.
    if hmac.compare_digest(payload, expected_response):
        conn.sendall(pack_message(MSG_HELLO_OK, b""))
        print("Handshake OK - passphrase verified.")
        return True
    else:
        conn.sendall(pack_message(MSG_HELLO_REJECT, b""))
        print("Handshake FAILED - passphrase mismatch.")
        return False


def receive_file(conn, filename: str, filesize: int, transfer_id: bytes) -> None:
    """
    Receive one file's worth of FILE_CHUNK messages, followed by DONE.
    Same logic as previous steps - staging, offset writes, per-chunk
    hashing, finalize-only-on-full-success.
    """
    data_path, manifest_path = staging_paths(transfer_id)

    try:
        preallocate_file(data_path, filesize)
    except OSError as e:
        print(f"Could not allocate space for incoming file: {e}")
        return

    print(f"Pre-allocated {filesize} bytes at {data_path}")
    output_file = open(data_path, "r+b")

    whole_file_hasher = hashlib.sha256()
    verified_chunks = []
    failed_chunk_indices = []
    chunks_received = 0

    while True:
        msg_type, msg_payload = recv_message(conn)

        if msg_type == MSG_FILE_CHUNK:
            chunk_index, expected_hash, chunk_data = unpack_file_chunk(msg_payload)
            actual_hash = hashlib.sha256(chunk_data).digest()
            chunk_ok = (actual_hash == expected_hash)

            if not chunk_ok:
                failed_chunk_indices.append(chunk_index)
                print(f"  Chunk {chunk_index}: HASH MISMATCH")
            else:
                verified_chunks.append(chunk_index)
                print(f"  Chunk {chunk_index}: OK ({len(chunk_data)} bytes)")

            output_file.seek(chunk_index * CHUNK_SIZE)
            output_file.write(chunk_data)
            whole_file_hasher.update(chunk_data)
            chunks_received += 1

            write_manifest(manifest_path, filename, filesize, CHUNK_SIZE, verified_chunks)

        elif msg_type == MSG_DONE:
            sender_whole_hash = msg_payload
            our_whole_hash = whole_file_hasher.digest()
            print(f"Received DONE. {chunks_received} chunks processed, "
                  f"{len(failed_chunk_indices)} failed.")
            output_file.close()

            if failed_chunk_indices:
                print(f"Chunks needing resend: {failed_chunk_indices}")
                print(f"File left in staging (not finalized): {data_path}")
            elif our_whole_hash != sender_whole_hash:
                print("WARNING: whole-file hash mismatch despite no per-chunk "
                      "failures. Leaving file in staging.")
            else:
                final_path = finalize_transfer(data_path, manifest_path, SAVE_DIR, filename)
                print(f"Whole-file hash MATCHES. Saved to {final_path}")
            return

        else:
            print(f"Unexpected message type {msg_type} during file receive.")
            output_file.close()
            return


def handle_session(conn, addr) -> None:
    """
    Run one full session with a connected peer: handshake, then loop
    handling file offers until BYE.
    """
    print(f"Connection received from {addr}")

    if not do_handshake(conn):
        print("Handshake failed - closing connection.")
        return

    while True:
        msg_type, payload = recv_message(conn)

        if msg_type == MSG_FILE_OFFER:
            filename, filesize, transfer_id = unpack_file_offer(payload)
            print(f"\nIncoming file offer: '{filename}' ({filesize} bytes) "
                  f"from {addr}")

            # Stand-in for the future GUI accept/reject dialog.
            answer = input("Accept this file? [y/n]: ").strip().lower()

            if answer == "y":
                conn.sendall(pack_message(MSG_FILE_ACCEPT, b""))
                receive_file(conn, filename, filesize, transfer_id)
            else:
                conn.sendall(pack_message(MSG_FILE_REJECT, b""))
                print(f"Rejected '{filename}'.")

        elif msg_type == MSG_BYE:
            print("Peer sent BYE - session ending.")
            return

        else:
            print(f"Unexpected message type {msg_type} - ending session.")
            return


def main():
    os.makedirs(STAGING_DIR, exist_ok=True)

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("0.0.0.0", LISTEN_PORT))
    server_socket.listen(1)

    print(f"Listening on port {LISTEN_PORT}...")

    # Outer loop: after each session ends, go back to waiting for the
    # next connection, rather than exiting the program.
    while True:
        conn, addr = server_socket.accept()
        with conn:
            handle_session(conn, addr)
        print("Session closed. Waiting for next connection...\n")


if __name__ == "__main__":
    main()
