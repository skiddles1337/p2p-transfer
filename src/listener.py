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

LISTEN_PORT = 5001
SAVE_DIR = "received"

# How long to wait for a connection before giving up, in seconds. This
# ONLY applies while waiting for someone to connect - once a session
# is underway, this timeout is turned off (see main()), so a slow but
# active transfer is never killed just for taking a while.
IDLE_TIMEOUT_SECONDS = 10


def preallocate_file(path: str, filesize: int) -> None:
    with open(path, "wb") as f:
        if filesize > 0:
            f.seek(filesize - 1)
            f.write(b"\x00")


def do_handshake(conn):
    """
    Perform the authenticated key exchange (see docs/DESIGN.md):
      1. Generate our own ephemeral X25519 keypair, send our public key
      2. Receive the peer's public key + their confirmation tag; verify
         the tag proves they know the shared passphrase for THESE
         specific public keys (catches tampering/MITM)
      3. Compute the shared secret, derive a session key
      4. Send our own confirmation tag back, so the peer can verify us too

    Returns a Fernet instance (ready to encrypt/decrypt chunks) on
    success, or None on failure (caller should close the connection).
    """
    listener_private, listener_public = generate_keypair()
    listener_public_bytes = public_key_to_bytes(listener_public)
    conn.sendall(pack_message(MSG_HELLO, listener_public_bytes))

    msg_type, payload = recv_message(conn)
    if msg_type != MSG_HELLO_RESPONSE:
        print(f"Expected HELLO_RESPONSE, got message type {msg_type}.")
        conn.sendall(pack_message(MSG_HELLO_REJECT, b""))
        return None

    sender_public_bytes, sender_tag = unpack_hello_response(payload)

    # Verify the sender's tag proves they know the passphrase, tied to
    # THESE exact two public keys (order: listener_pub, sender_pub).
    if not verify_confirmation_tag(sender_tag, listener_public_bytes, sender_public_bytes):
        conn.sendall(pack_message(MSG_HELLO_REJECT, b""))
        print("Handshake FAILED - confirmation tag mismatch (wrong "
              "passphrase, or a tampered/substituted key in transit).")
        return None

    # Tag verified - now compute the shared secret and session key.
    sender_public = public_key_from_bytes(sender_public_bytes)
    shared_secret = compute_shared_secret(listener_private, sender_public)
    session_key = derive_session_key(shared_secret)

    # Send OUR confirmation tag back, so the sender can verify us too -
    # note the REVERSED order (sender_pub, listener_pub) vs. the tag we
    # just checked, to prevent a reflection attack.
    our_tag = compute_confirmation_tag(sender_public_bytes, listener_public_bytes)
    conn.sendall(pack_message(MSG_HELLO_OK, our_tag))

    print("Handshake OK - key exchange authenticated, session key established.")
    return Fernet(session_key)


def receive_file(conn, filename: str, filesize: int, transfer_id: bytes, fernet: Fernet) -> None:
    """
    Receive one file's worth of FILE_CHUNK messages, followed by DONE.
    Each chunk arrives ENCRYPTED - we decrypt first, then hash the
    resulting plaintext to check against the sender's hash (which was
    computed over the original plaintext on their end).
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
            chunk_index, expected_hash, encrypted_data = unpack_file_chunk(msg_payload)

            try:
                chunk_data = fernet.decrypt(encrypted_data)
                actual_hash = hashlib.sha256(chunk_data).digest()
                chunk_ok = (actual_hash == expected_hash)
            except InvalidToken:
                # Fernet's own authentication failed - the ciphertext
                # was tampered with or corrupted badly enough that it
                # doesn't even decrypt cleanly. Treat this the same as
                # a hash mismatch: this chunk failed, but keep going.
                chunk_data = b""
                chunk_ok = False
                print(f"  Chunk {chunk_index}: DECRYPTION FAILED (corrupted ciphertext)")

            if not chunk_ok:
                failed_chunk_indices.append(chunk_index)
                if chunk_data:  # only print hash-mismatch message if we got this far
                    print(f"  Chunk {chunk_index}: HASH MISMATCH")
            else:
                verified_chunks.append(chunk_index)
                print(f"  Chunk {chunk_index}: OK ({len(chunk_data)} bytes)")

            if chunk_data:
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

    fernet = do_handshake(conn)
    if fernet is None:
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
                receive_file(conn, filename, filesize, transfer_id, fernet)
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

    # This makes accept() give up and raise socket.timeout after this
    # many seconds of no connection, instead of blocking forever. This
    # is what makes it POSSIBLE to ever notice "nothing's happening,
    # should I stop?" - a truly blocking accept() can't be interrupted
    # at all. This is also exactly the mechanism a future GUI's "Stop
    # Listening" button would rely on: same idea, just checking a
    # button-driven flag instead of a fixed time limit.
    server_socket.settimeout(IDLE_TIMEOUT_SECONDS)

    print(f"Listening on port {LISTEN_PORT} "
          f"(will stop after {IDLE_TIMEOUT_SECONDS}s with no connection)...")

    while True:
        try:
            conn, addr = server_socket.accept()
        except socket.timeout:
            print(f"No connection received in {IDLE_TIMEOUT_SECONDS} seconds. Stopping.")
            break

        # Once a real connection comes in, we don't want THIS timeout
        # applying to it - a slow file transfer isn't "idle", and
        # shouldn't get killed just for taking a while. Sockets
        # returned by accept() have their own independent timeout
        # setting, separate from the listening socket's, so this only
        # affects the one connection we're about to handle.
        conn.settimeout(None)

        with conn:
            handle_session(conn, addr)
        print("Session closed. Waiting for next connection...\n")

    server_socket.close()


if __name__ == "__main__":
    main()
