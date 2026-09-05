"""
simulate_corruption.py

Manual diagnostic tool, not an automated test (no pass/fail
assertions - you read the output and judge for yourself). Lives in
tests/ rather than src/ since it's not part of the actual application;
it exists purely to prove the receiver's per-chunk integrity checking
actually catches corruption, by deliberately mangling a couple of
chunks' CIPHERTEXT before sending them.

How it works: performs the real handshake (so the listener has a
valid session key to decrypt with), then for specific chunk indices,
corrupts the ENCRYPTED bytes after encryption - simulating real-world
transit corruption. This should get caught as a decryption failure
(AES-GCM's own built-in authentication tag rejects tampered
ciphertext) rather than a hash mismatch, though either outcome
demonstrates the same thing: corrupted data doesn't silently pass
through.

Usage: run `python cli.py`, then `listen <port>` and
`trust someone TEST_PASSPHRASE` (see TEST_PASSPHRASE below) to accept
this script's handshake, then run this script from the tests/ folder.
"""

import socket
import os
import sys
import hashlib

# This script lives in tests/, but the app modules live in src/ - a
# sibling folder, not a subfolder Python would search automatically.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from protocol import (
    pack_message,
    recv_message,
    pack_file_offer,
    pack_file_chunk,
    pack_hello_response,
    MSG_HELLO,
    MSG_HELLO_RESPONSE,
    MSG_HELLO_OK,
    MSG_FILE_OFFER,
    MSG_FILE_ACCEPT,
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
from chunk_crypto import ChunkCipher

TARGET_IP = "127.0.0.1"
TARGET_PORT = 5001

# Must match whatever passphrase you registered with `trust` on the
# listener side (see usage note above).
TEST_PASSPHRASE = "test-corruption-passphrase"

FILE_TO_SEND = os.path.join(os.path.dirname(__file__), "..", "src", "test_file.txt")

# Which chunk indices to deliberately corrupt (0-based).
CHUNKS_TO_CORRUPT = {1, 3}


def do_handshake(sock):
    """Same handshake as engine.py's sender side - duplicated here
    since this script intentionally stands alone rather than importing
    the engine."""
    msg_type, listener_public_bytes = recv_message(sock)
    if msg_type != MSG_HELLO:
        raise RuntimeError(f"Expected HELLO, got {msg_type}")

    sender_private, sender_public = generate_keypair()
    sender_public_bytes = public_key_to_bytes(sender_public)

    tag = compute_confirmation_tag(TEST_PASSPHRASE, listener_public_bytes, sender_public_bytes)
    sock.sendall(pack_message(MSG_HELLO_RESPONSE, pack_hello_response(sender_public_bytes, tag)))

    msg_type, listener_tag = recv_message(sock)
    if msg_type != MSG_HELLO_OK:
        raise RuntimeError("Handshake rejected")
    if not verify_confirmation_tag(listener_tag, TEST_PASSPHRASE, sender_public_bytes, listener_public_bytes):
        raise RuntimeError("Listener's confirmation tag invalid")

    listener_public = public_key_from_bytes(listener_public_bytes)
    shared_secret = compute_shared_secret(sender_private, listener_public)
    return ChunkCipher(derive_session_key(shared_secret))


def main():
    if not os.path.exists(FILE_TO_SEND):
        print(f"Error: '{FILE_TO_SEND}' not found.")
        return

    filesize = os.path.getsize(FILE_TO_SEND)
    announced_name = os.path.basename(FILE_TO_SEND)
    print(f"'{FILE_TO_SEND}' is {filesize} bytes.")
    print(f"Will deliberately corrupt chunks: {sorted(CHUNKS_TO_CORRUPT)}")

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((TARGET_IP, TARGET_PORT))
    print("Connected.")

    cipher = do_handshake(s)
    print("Handshake complete - session key established.")

    offer_payload = pack_file_offer(announced_name, filesize, os.urandom(TRANSFER_ID_LEN))
    s.sendall(pack_message(MSG_FILE_OFFER, offer_payload))
    msg_type, _ = recv_message(s)
    if msg_type != MSG_FILE_ACCEPT:
        print("File was not accepted. Aborting.")
        return

    whole_file_hasher = hashlib.sha256()

    with open(FILE_TO_SEND, "rb") as f:
        chunk_index = 0
        while True:
            chunk = f.read(CHUNK_SIZE)
            if chunk == b"":
                break

            whole_file_hasher.update(chunk)
            chunk_hash = hashlib.sha256(chunk).digest()
            encrypted = cipher.encrypt(chunk)

            if chunk_index in CHUNKS_TO_CORRUPT:
                # Corrupt the CIPHERTEXT after encryption - the hash
                # still reflects the correct original plaintext, so
                # this simulates real transit corruption rather than
                # "we sent the wrong data on purpose."
                encrypted = b"CORRUPTED_BYTES_HERE" + encrypted[21:]
                print(f"  [CORRUPTING] chunk {chunk_index} ciphertext before sending")

            payload = pack_file_chunk(chunk_index, chunk_hash, encrypted)
            s.sendall(pack_message(MSG_FILE_CHUNK, payload))
            print(f"  Sent chunk {chunk_index} ({len(chunk)} plaintext bytes)")

            chunk_index += 1

    final_hash = whole_file_hasher.digest()
    s.sendall(pack_message(MSG_DONE, final_hash))
    print(f"Sent DONE - whole-file hash: {final_hash.hex()}")

    s.sendall(pack_message(MSG_BYE, b""))
    s.close()
    print("Connection closed.")


if __name__ == "__main__":
    main()
