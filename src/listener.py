"""
listener.py

Listens on a port, accepts ONE incoming connection, expects a
FILE_OFFER message followed by a FILE_DATA message, and writes the
received bytes to disk under received/<filename>.

No chunking, hashing, or encryption yet - see sender.py for the
matching notes on scope.
"""

import socket
import os
from protocol import recv_message, MSG_FILE_OFFER, MSG_FILE_DATA

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
        # First message: the file offer, telling us the filename.
        msg_type, payload = recv_message(conn)
        if msg_type != MSG_FILE_OFFER:
            print(f"Expected FILE_OFFER, got message type {msg_type}. Aborting.")
            return

        filename = payload.decode("utf-8")
        print(f"Received FILE_OFFER: '{filename}'")

        # Second message: the actual file content.
        msg_type, payload = recv_message(conn)
        if msg_type != MSG_FILE_DATA:
            print(f"Expected FILE_DATA, got message type {msg_type}. Aborting.")
            return

        print(f"Received FILE_DATA - {len(payload)} bytes")

        # Make sure the save directory exists, then write the file.
        os.makedirs(SAVE_DIR, exist_ok=True)

        # os.path.basename strips any path info from the filename,
        # just in case - we only ever want to write inside SAVE_DIR,
        # never wherever the filename might otherwise point to.
        safe_filename = os.path.basename(filename)
        save_path = os.path.join(SAVE_DIR, safe_filename)

        with open(save_path, "wb") as f:
            f.write(payload)

        print(f"Saved to {save_path}")

    server_socket.close()
    print("Done.")


if __name__ == "__main__":
    main()
