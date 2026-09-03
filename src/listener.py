"""
listener.py

Listens on a port, accepts ONE incoming connection, expects a
FILE_OFFER message (filename + total filesize) followed by a sequence
of FILE_CHUNK messages, and writes the received bytes to disk under
received/<filename>, stopping once the expected total size is reached.

No hashing or encryption yet - see protocol.py for what's coming next.
"""

import socket
import os
from protocol import recv_message, unpack_file_offer, MSG_FILE_OFFER, MSG_FILE_CHUNK

LISTEN_PORT = 5001
SAVE_DIR = "received"


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

        # Now receive chunks until we've collected the full filesize.
        # This mirrors the sender's loop: instead of "keep sending
        # until the file is exhausted", it's "keep receiving until
        # we've reached the size we were told to expect".
        bytes_received = 0
        chunk_count = 0

        with open(save_path, "wb") as f:
            while bytes_received < filesize:
                msg_type, chunk_payload = recv_message(conn)

                if msg_type != MSG_FILE_CHUNK:
                    print(f"Expected FILE_CHUNK, got message type {msg_type}. Aborting.")
                    return

                f.write(chunk_payload)
                bytes_received += len(chunk_payload)
                chunk_count += 1
                print(f"  Received chunk {chunk_count} ({len(chunk_payload)} bytes, "
                      f"{bytes_received}/{filesize} total)")

        print(f"Done receiving. {chunk_count} chunks, {bytes_received} bytes total.")
        print(f"Saved to {save_path}")

    server_socket.close()
    print("Done.")


if __name__ == "__main__":
    main()
