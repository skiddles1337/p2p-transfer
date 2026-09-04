"""
listener.py

Listens on a port, accepts ONE incoming connection, and receives a
file sent as indexed, hashed chunks (see sender.py).

Incoming files are written to a STAGING area (see storage.py) keyed by
transfer_id, not directly to their final name. A manifest file tracks
which chunks have been verified so far, updated after every chunk -
this is groundwork for future resume support, even though the actual
"reconnect and continue" logic isn't built yet.

Only once a transfer completes with ZERO failed chunks and a matching
whole-file hash does the file get moved into its final, human-readable
name (with collision-safe renaming). If anything failed, the staged
file and manifest are left in place rather than discarded.
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
from storage import staging_paths, write_manifest, finalize_transfer, STAGING_DIR

LISTEN_PORT = 5001
SAVE_DIR = "received"


def preallocate_file(path: str, filesize: int) -> None:
    """
    Create a file of exactly `filesize` bytes, filled with zeros -
    see previous step's notes for why (safe random-offset writes,
    and an early failure if there's not enough disk space).
    """
    with open(path, "wb") as f:
        if filesize > 0:
            f.seek(filesize - 1)
            f.write(b"\x00")


def main():
    os.makedirs(STAGING_DIR, exist_ok=True)

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("0.0.0.0", LISTEN_PORT))
    server_socket.listen(1)

    print(f"Listening on port {LISTEN_PORT}... waiting for a connection.")

    conn, addr = server_socket.accept()
    print(f"Connection received from {addr}")

    with conn:
        msg_type, payload = recv_message(conn)
        if msg_type != MSG_FILE_OFFER:
            print(f"Expected FILE_OFFER, got message type {msg_type}. Aborting.")
            return

        filename, filesize, transfer_id = unpack_file_offer(payload)
        print(f"Received FILE_OFFER: '{filename}', {filesize} bytes expected, "
              f"transfer_id={transfer_id.hex()}")

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
                    print(f"  Chunk {chunk_index}: HASH MISMATCH "
                          f"(expected {expected_hash.hex()[:8]}..., "
                          f"got {actual_hash.hex()[:8]}...)")
                else:
                    verified_chunks.append(chunk_index)
                    print(f"  Chunk {chunk_index}: OK ({len(chunk_data)} bytes)")

                output_file.seek(chunk_index * CHUNK_SIZE)
                output_file.write(chunk_data)
                whole_file_hasher.update(chunk_data)
                chunks_received += 1

                # Update the manifest after every chunk, so the
                # on-disk record of "what's verified so far" survives
                # even if the process crashes or the connection drops
                # right after this point.
                write_manifest(manifest_path, filename, filesize,
                                CHUNK_SIZE, verified_chunks)

            elif msg_type == MSG_DONE:
                sender_whole_hash = msg_payload
                our_whole_hash = whole_file_hasher.digest()

                print(f"Received DONE. {chunks_received} chunks processed, "
                      f"{len(failed_chunk_indices)} failed.")

                output_file.close()

                if failed_chunk_indices:
                    print(f"Chunks needing resend: {failed_chunk_indices}")
                    print(f"File left in staging (not finalized): {data_path}")
                    print("(Resume/resend isn't built yet - for now, this "
                          "transfer must be redone from scratch.)")
                elif our_whole_hash != sender_whole_hash:
                    print("WARNING: no chunks failed individually, but the "
                          "whole-file hash does NOT match. Leaving file in "
                          "staging rather than finalizing.")
                else:
                    final_path = finalize_transfer(
                        data_path, manifest_path, SAVE_DIR, filename
                    )
                    print(f"Whole-file hash MATCHES. Transfer verified successful.")
                    print(f"Saved to {final_path}")

                break

            else:
                print(f"Unexpected message type {msg_type}. Aborting.")
                output_file.close()
                return

    server_socket.close()
    print("Done.")


if __name__ == "__main__":
    main()
