"""
simulate_corruption.py

Manual diagnostic tool, not an automated test (no pass/fail
assertions - you read the output and judge for yourself). Lives in
tests/ rather than src/ since it's not part of the actual application;
it exists purely to prove the receiver's per-chunk hash checking
actually catches corruption, by deliberately mangling a couple of
chunks before sending them.

How it works: we build each chunk's payload normally (which includes
the hash of the ORIGINAL, correct data), but then for specific chunk
indices, we swap in mangled data while keeping the original hash. This
guarantees a mismatch when the receiver re-hashes what it actually
got - simulating real-world corruption (e.g. a flaky connection or
disk error), since real corruption also wouldn't affect the hash the
sender already computed and sent.

Usage: run src/listener.py first, then run this script from the
tests/ folder (not sender.py) to feed it a deliberately corrupted
transfer.
"""

import socket
import os
import sys
import struct
import hashlib

# This script lives in tests/, but protocol.py lives in src/ - a
# sibling folder, not a subfolder Python would search automatically.
# This adds src/ to Python's list of places to look for imports, so
# the next import line can find protocol.py.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from protocol import (
    pack_message,
    pack_file_offer,
    pack_file_chunk,
    unpack_file_chunk,
    CHUNK_INDEX_FORMAT,
    MSG_FILE_OFFER,
    MSG_FILE_CHUNK,
    MSG_DONE,
    CHUNK_SIZE,
    TRANSFER_ID_LEN,
)

TARGET_IP = "127.0.0.1"
TARGET_PORT = 5001

# The sample test file lives in src/, alongside the real app files -
# this script lives in tests/, a sibling folder, so we reach across.
FILE_TO_SEND = os.path.join(os.path.dirname(__file__), "..", "src", "test_file.txt")

# Which chunk indices to deliberately corrupt (0-based).
CHUNKS_TO_CORRUPT = {1, 3}


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

    offer_payload = pack_file_offer(announced_name, filesize, os.urandom(TRANSFER_ID_LEN))
    s.sendall(pack_message(MSG_FILE_OFFER, offer_payload))
    print(f"Sent FILE_OFFER - filename='{announced_name}', size={filesize}")

    # Hash the ORIGINAL, uncorrupted bytes - this represents "what the
    # file should be," same as a real sender would compute.
    whole_file_hasher = hashlib.sha256()

    with open(FILE_TO_SEND, "rb") as f:
        chunk_index = 0
        while True:
            chunk = f.read(CHUNK_SIZE)
            if chunk == b"":
                break

            whole_file_hasher.update(chunk)

            # Build the payload normally first - this embeds the hash
            # of the correct, original data.
            chunk_payload = pack_file_chunk(chunk_index, chunk)

            if chunk_index in CHUNKS_TO_CORRUPT:
                # Pull the payload back apart, replace the data with
                # garbage, but keep the ORIGINAL hash - so it's
                # guaranteed not to match once the receiver re-hashes
                # what actually arrived.
                _, original_hash, original_data = unpack_file_chunk(chunk_payload)
                corrupted_data = b"XX_CORRUPTED_XX" + original_data[15:]
                chunk_payload = (
                    struct.pack(CHUNK_INDEX_FORMAT, chunk_index)
                    + original_hash
                    + corrupted_data
                )
                print(f"  [CORRUPTING] chunk {chunk_index} before sending")

            s.sendall(pack_message(MSG_FILE_CHUNK, chunk_payload))
            print(f"  Sent chunk {chunk_index} ({len(chunk)} bytes)")

            chunk_index += 1

    final_hash = whole_file_hasher.digest()
    s.sendall(pack_message(MSG_DONE, final_hash))
    print(f"Sent DONE - whole-file hash: {final_hash.hex()}")

    s.close()
    print("Connection closed.")


if __name__ == "__main__":
    main()
