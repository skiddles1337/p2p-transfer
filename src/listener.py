"""
listener.py

Listens for connections and handles each one as a persistent SESSION:
  1. Authenticated key exchange (see keyexchange.py, auth.py)
  2. loop: receive FILE_OFFER -> prompt to accept/reject -> if
     accepted, receive the file; repeat for as many files as the
     sender sends
  3. BYE - sender signals it's done; connection closes

After a session ends (cleanly OR due to an error), the listener goes
back to waiting for the next connection, rather than exiting or
crashing - this is the core robustness goal of this version: no single
bad connection should be able to take down the whole listener.
"""

import socket
import os
import hashlib
from cryptography.fernet import Fernet, InvalidToken
from protocol import (
    recv_message,
    pack_message,
    pack_hello_response,
    unpack_hello_response,
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
    CHUNK_SIZE,
)
from auth import compute_confirmation_tag, verify_confirmation_tag
from keyexchange import (
    generate_keypair,
    public_key_to_bytes,
    public_key_from_bytes,
    compute_shared_secret,
    derive_session_key,
)
from storage import staging_paths, write_manifest, finalize_transfer, STAGING_DIR
from logger import log

LISTEN_PORT = 5001
SAVE_DIR = "received"
IDLE_TIMEOUT_SECONDS = 10


def preallocate_file(path: str, filesize: int) -> None:
    with open(path, "wb") as f:
        if filesize > 0:
            f.seek(filesize - 1)
            f.write(b"\x00")


def do_handshake(conn):
    """
    Perform the authenticated key exchange. Returns a Fernet instance
    on success, or None on failure (caller should close the
    connection). Network errors during the handshake itself (e.g. the
    peer disconnecting mid-handshake) propagate up as exceptions - the
    caller (handle_session, and ultimately main()) is responsible for
    catching those, since a handshake-time disconnect and a
    handshake-time REJECTION are different things for the caller to
    react to.
    """
    listener_private, listener_public = generate_keypair()
    listener_public_bytes = public_key_to_bytes(listener_public)
    conn.sendall(pack_message(MSG_HELLO, listener_public_bytes))

    msg_type, payload = recv_message(conn)
    if msg_type != MSG_HELLO_RESPONSE:
        log(f"Expected HELLO_RESPONSE, got message type {msg_type}.")
        conn.sendall(pack_message(MSG_HELLO_REJECT, b""))
        return None

    sender_public_bytes, sender_tag = unpack_hello_response(payload)

    if not verify_confirmation_tag(sender_tag, listener_public_bytes, sender_public_bytes):
        conn.sendall(pack_message(MSG_HELLO_REJECT, b""))
        log("Handshake FAILED - confirmation tag mismatch (wrong "
            "passphrase, or a tampered/substituted key in transit).")
        return None

    sender_public = public_key_from_bytes(sender_public_bytes)
    shared_secret = compute_shared_secret(listener_private, sender_public)
    session_key = derive_session_key(shared_secret)

    our_tag = compute_confirmation_tag(sender_public_bytes, listener_public_bytes)
    conn.sendall(pack_message(MSG_HELLO_OK, our_tag))

    log("Handshake OK - key exchange authenticated, session key established.")
    return Fernet(session_key)


def receive_file(conn, filename: str, filesize: int, transfer_id: bytes, fernet: Fernet) -> None:
    """
    Receive one file's worth of FILE_CHUNK messages, followed by DONE.
    """
    data_path, manifest_path = staging_paths(transfer_id)

    try:
        preallocate_file(data_path, filesize)
    except OSError as e:
        log(f"Could not allocate space for incoming file: {e}")
        return

    log(f"Pre-allocated {filesize} bytes at {data_path}")
    output_file = open(data_path, "r+b")

    whole_file_hasher = hashlib.sha256()
    verified_chunks = []
    failed_chunk_indices = []
    chunks_received = 0

    try:
        while True:
            msg_type, msg_payload = recv_message(conn)

            if msg_type == MSG_FILE_CHUNK:
                chunk_index, expected_hash, encrypted_data = unpack_file_chunk(msg_payload)

                try:
                    chunk_data = fernet.decrypt(encrypted_data)
                    actual_hash = hashlib.sha256(chunk_data).digest()
                    chunk_ok = (actual_hash == expected_hash)
                except InvalidToken:
                    chunk_data = b""
                    chunk_ok = False
                    log(f"  Chunk {chunk_index}: DECRYPTION FAILED (corrupted ciphertext)")

                if not chunk_ok:
                    failed_chunk_indices.append(chunk_index)
                    if chunk_data:
                        log(f"  Chunk {chunk_index}: HASH MISMATCH")
                else:
                    verified_chunks.append(chunk_index)
                    log(f"  Chunk {chunk_index}: OK ({len(chunk_data)} bytes)")

                if chunk_data:
                    output_file.seek(chunk_index * CHUNK_SIZE)
                    output_file.write(chunk_data)
                    whole_file_hasher.update(chunk_data)

                chunks_received += 1
                write_manifest(manifest_path, filename, filesize, CHUNK_SIZE, verified_chunks)

            elif msg_type == MSG_DONE:
                sender_whole_hash = msg_payload
                our_whole_hash = whole_file_hasher.digest()
                log(f"Received DONE. {chunks_received} chunks processed, "
                    f"{len(failed_chunk_indices)} failed.")

                if failed_chunk_indices:
                    log(f"Chunks needing resend: {failed_chunk_indices}")
                    log(f"File left in staging (not finalized): {data_path}")
                elif our_whole_hash != sender_whole_hash:
                    log("WARNING: whole-file hash mismatch despite no per-chunk "
                        "failures. Leaving file in staging.")
                else:
                    final_path = finalize_transfer(data_path, manifest_path, SAVE_DIR, filename)
                    log(f"Whole-file hash MATCHES. Saved to {final_path}")
                return

            else:
                log(f"Unexpected message type {msg_type} during file receive.")
                return

    except (ConnectionError, OSError) as e:
        # The connection dropped mid-transfer (e.g. peer's network
        # died, they closed the app, wifi hiccup). This is exactly the
        # kind of thing the staging system was built for: whatever
        # chunks arrived and were verified are already safely recorded
        # in the manifest, so this isn't silent data loss - it's a
        # resumable partial transfer sitting in staging, waiting for a
        # future resume feature (or a manual retry, today).
        log(f"Connection lost during file transfer: {e}")
        log(f"Partial data preserved in staging: {data_path}")

    finally:
        # Whatever happened above - success, failure, or a dropped
        # connection - make sure the file handle gets closed. Without
        # this, an exception path above could leave the file open.
        output_file.close()


def handle_session(conn, addr) -> None:
    """
    Run one full session with a connected peer: handshake, then loop
    handling file offers until BYE.
    """
    log(f"Connection received from {addr}")

    fernet = do_handshake(conn)
    if fernet is None:
        log("Handshake failed - closing connection.")
        return

    while True:
        msg_type, payload = recv_message(conn)

        if msg_type == MSG_FILE_OFFER:
            filename, filesize, transfer_id = unpack_file_offer(payload)
            log(f"Incoming file offer: '{filename}' ({filesize} bytes) from {addr}")

            answer = input("Accept this file? [y/n]: ").strip().lower()

            if answer == "y":
                conn.sendall(pack_message(MSG_FILE_ACCEPT, b""))
                receive_file(conn, filename, filesize, transfer_id, fernet)
            else:
                conn.sendall(pack_message(MSG_FILE_REJECT, b""))
                log(f"Rejected '{filename}'.")

        elif msg_type == MSG_BYE:
            log("Peer sent BYE - session ending.")
            return

        else:
            log(f"Unexpected message type {msg_type} - ending session.")
            return


def main():
    os.makedirs(STAGING_DIR, exist_ok=True)

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("0.0.0.0", LISTEN_PORT))
    server_socket.listen(1)
    server_socket.settimeout(IDLE_TIMEOUT_SECONDS)

    log(f"Listening on port {LISTEN_PORT} "
        f"(will stop after {IDLE_TIMEOUT_SECONDS}s with no connection)...")

    while True:
        try:
            conn, addr = server_socket.accept()
        except socket.timeout:
            log(f"No connection received in {IDLE_TIMEOUT_SECONDS} seconds. Stopping.")
            break

        conn.settimeout(None)

        # This is the key robustness addition: ANY unexpected error
        # while handling this one connection - a dropped connection,
        # a peer sending malformed data, a bug we haven't anticipated -
        # gets caught HERE, logged, and the outer loop continues to
        # the next connection. Without this, an unhandled exception
        # anywhere inside handle_session() would propagate all the way
        # up and crash the entire listener - meaning one bad or
        # malicious connection could take down "always listening"
        # entirely. This matters more, not less, once a GUI is relying
        # on this loop running forever in the background.
        try:
            with conn:
                handle_session(conn, addr)
        except (ConnectionError, OSError) as e:
            log(f"Connection error during session with {addr}: {e}")
        except Exception as e:
            # Deliberately broad: we genuinely want NOTHING to be able
            # to crash this loop. Anything we didn't specifically
            # anticipate still gets logged rather than propagating.
            log(f"Unexpected error during session with {addr}: {e}")

        log("Session closed. Waiting for next connection...")

    server_socket.close()


if __name__ == "__main__":
    main()
