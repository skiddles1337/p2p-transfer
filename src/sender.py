"""
sender.py

Connects to a listener and sends a file in indexed, hashed chunks:
  1. FILE_OFFER   - filename + total filesize
  2. FILE_CHUNK   (one per chunk) - index + hash + data
  3. DONE         - SHA-256 hash of the entire file, computed
                    incrementally as chunks were read (no second pass
                    over the file needed)

No encryption or passphrase yet - see protocol.py for what's coming.

Run listener.py first (in one terminal), then run this script (in a
second terminal) while the listener is waiting.
"""

import socket
import os
import hashlib
from protocol import (
    pack_message,
    pack_file_offer,
    pack_file_chunk,
    MSG_FILE_OFFER,
    MSG_FILE_CHUNK,
    MSG_DONE,
    CHUNK_SIZE,
)

TARGET_IP = "127.0.0.1"
TARGET_PORT = 5001

FILE_TO_SEND = "test_file.txt"


def main():
    if not os.path.exists(FILE_TO_SEND):
        print(f"Error: '{FILE_TO_SEND}' not found. Create it first, "
              f"in the same folder as this script.")
        return

    filesize = os.path.getsize(FILE_TO_SEND)
    print(f"'{FILE_TO_SEND}' is {filesize} bytes.")

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    print(f"Connecting to {TARGET_IP}:{TARGET_PORT}...")
    client_socket.connect((TARGET_IP, TARGET_PORT))
    print("Connected.")

    # Step 1: announce the incoming file.
    offer_payload = pack_file_offer(FILE_TO_SEND, filesize)
    offer_message = pack_message(MSG_FILE_OFFER, offer_payload)
    client_socket.sendall(offer_message)
    print(f"Sent FILE_OFFER - filename='{FILE_TO_SEND}', size={filesize}")

    # A hash object we'll keep feeding chunks into as we go, so the
    # whole-file hash is ready the moment we finish reading - no need
    # to open and re-read the file a second time just to hash it.
    whole_file_hasher = hashlib.sha256()

    bytes_sent = 0
    chunk_index = 0

    with open(FILE_TO_SEND, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)

            if chunk == b"":
                break

            # Feed this chunk into the running whole-file hash.
            whole_file_hasher.update(chunk)

            chunk_payload = pack_file_chunk(chunk_index, chunk)
            chunk_message = pack_message(MSG_FILE_CHUNK, chunk_payload)
            client_socket.sendall(chunk_message)

            bytes_sent += len(chunk)
            print(f"  Sent chunk {chunk_index} ({len(chunk)} bytes, "
                  f"{bytes_sent}/{filesize} total)")

            chunk_index += 1

    total_chunks = chunk_index  # after the loop, this is the count sent
    print(f"Done sending chunks. {total_chunks} chunks, {bytes_sent} bytes total.")

    # Step 3: send the final whole-file hash so the receiver can do
    # one last end-to-end integrity check.
    final_hash = whole_file_hasher.digest()
    done_message = pack_message(MSG_DONE, final_hash)
    client_socket.sendall(done_message)
    print(f"Sent DONE - whole-file hash: {final_hash.hex()}")

    client_socket.close()
    print("Connection closed.")


if __name__ == "__main__":
    main()
