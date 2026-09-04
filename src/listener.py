"""
listener.py

Listens on a port, accepts ONE incoming connection, and receives a
file sent as indexed, hashed chunks (see sender.py). For each chunk:
  - verifies its hash
  - writes it to disk at its correct byte offset (index * CHUNK_SIZE),
    regardless of whether the hash matched - so a failed chunk can be
    overwritten later without needing to rebuild the whole file
  - keeps receiving even if a chunk fails, tracking which ones failed

The output file is pre-allocated to its final size up front, both so
random-offset writes are safe, and so we find out immediately if
there isn't enough disk space, rather than partway through.

No encryption or passphrase yet - see protocol.py for what's coming.
"""

import socket
import os
import hashlib
from protocol import (
    recv_message,
    unpack_file_offer,
    unpack_file_chunk,
    MSG_FILE_OFFER,
    MSG_FILE_CHUNK,
    MSG_DONE,
    CHUNK_SIZE,
)

LISTEN_PORT = 5001
SAVE_DIR = "received"


def preallocate_file(path: str, filesize: int) -> None:
    """
    Create a file of exactly `filesize` bytes, filled with zeros.

    Seeking to the last byte and writing a single zero forces the
    filesystem to commit to a file of that final size right away. On
    most filesystems, this will fail immediately (raising an error we
    can catch) if there isn't enough free disk space - much better
    than discovering that partway through a multi-gigabyte transfer.
    """
    with open(path, "wb") as f:
        if filesize > 0:
            f.seek(filesize - 1)
            f.write(b"\x00")
        # if filesize is 0, an empty file from `open(..., "wb")` alone
        # is already correct - nothing further to do.


def main():
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("0.0.0.0", LISTEN_PORT))
    server_socket.listen(1)

    print(f"Listening on port {LISTEN_PORT}... waiting for a connection.")

    conn, addr = server_socket.accept()
    print(f"Connection received from {addr}")

    with conn:
        # First message: the file offer, telling us filename + size.
        msg_type, payload = recv_message(conn)
        if msg_type != MSG_FILE_OFFER:
            print(f"Expected FILE_OFFER, got message type {msg_type}. Aborting.")
            return

        filename, filesize = unpack_file_offer(payload)
        print(f"Received FILE_OFFER: '{filename}', {filesize} bytes expected")

        os.makedirs(SAVE_DIR, exist_ok=True)
        safe_filename = os.path.basename(filename)
        save_path = os.path.join(SAVE_DIR, safe_filename)

        try:
            preallocate_file(save_path, filesize)
        except OSError as e:
            print(f"Could not allocate space for incoming file: {e}")
            return

        print(f"Pre-allocated {filesize} bytes at {save_path}")

        # "r+b" = read/write binary, WITHOUT truncating the existing
        # file (unlike "wb", which would erase what we just
        # pre-allocated). This lets us seek anywhere in the file and
        # write at arbitrary offsets.
        output_file = open(save_path, "r+b")

        whole_file_hasher = hashlib.sha256()
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
                    print(f"  Chunk {chunk_index}: HASH MISMATCH "
                          f"(expected {expected_hash.hex()[:8]}..., "
                          f"got {actual_hash.hex()[:8]}...)")
                else:
                    print(f"  Chunk {chunk_index}: OK ({len(chunk_data)} bytes)")

                # Write regardless of whether the hash matched - we
                # want SOME data at this offset, and this design
                # assumes we may overwrite it later via a resend.
                output_file.seek(chunk_index * CHUNK_SIZE)
                output_file.write(chunk_data)

                # Feed into the whole-file hash in the order chunks
                # were received. This only produces a meaningful
                # final hash if chunks arrive in order with none
                # missing - true here, since TCP preserves order on a
                # single connection and we're not skipping any.
                whole_file_hasher.update(chunk_data)
                chunks_received += 1

            elif msg_type == MSG_DONE:
                sender_whole_hash = msg_payload
                our_whole_hash = whole_file_hasher.digest()

                print(f"Received DONE. {chunks_received} chunks processed, "
                      f"{len(failed_chunk_indices)} failed.")

                output_file.close()

                if failed_chunk_indices:
                    print(f"Chunks needing resend: {failed_chunk_indices}")
                    print("(Whole-file hash check skipped - failures already known.)")
                elif our_whole_hash == sender_whole_hash:
                    print("Whole-file hash MATCHES. Transfer verified successful.")
                else:
                    # No chunks flagged as bad, but the final hash still
                    # doesn't match - something is wrong that our
                    # per-chunk checks didn't catch (e.g. a chunk
                    # written at the wrong offset). Worth flagging
                    # distinctly since it's unexpected.
                    print("WARNING: no chunks failed individually, but the "
                          "whole-file hash does NOT match. Something is "
                          "wrong that per-chunk checks didn't catch.")

                break

            else:
                print(f"Unexpected message type {msg_type}. Aborting.")
                output_file.close()
                return

    server_socket.close()
    print("Done.")


if __name__ == "__main__":
    main()
