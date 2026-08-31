"""
sender.py

Connects to a listener and sends a real file: first a FILE_OFFER
message (just the filename), then a FILE_DATA message containing the
file's entire raw contents as one blob.

No chunking, hashing, or encryption yet - this step is purely about
proving we can move a real file's bytes from one program to another
and have it land correctly on disk.

Run listener.py first (in one terminal), then run this script (in a
second terminal) while the listener is waiting.
"""

import socket
import os
from protocol import pack_message, MSG_FILE_OFFER, MSG_FILE_DATA

TARGET_IP = "127.0.0.1"
TARGET_PORT = 5001

# Hardcoded for now - we don't have a file picker yet (that's a GUI
# feature, later). Put a small test file next to this script with
# this exact name to try it out.
FILE_TO_SEND = "test_file.txt"


def main():
    if not os.path.exists(FILE_TO_SEND):
        print(f"Error: '{FILE_TO_SEND}' not found. Create it first, "
              f"in the same folder as this script.")
        return

    # "rb" = read binary. We want raw bytes, not text - this matters
    # even for a .txt file, since we're about to send it over a
    # socket, which only understands bytes.
    with open(FILE_TO_SEND, "rb") as f:
        file_bytes = f.read()

    print(f"Read '{FILE_TO_SEND}' - {len(file_bytes)} bytes.")

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    print(f"Connecting to {TARGET_IP}:{TARGET_PORT}...")
    client_socket.connect((TARGET_IP, TARGET_PORT))
    print("Connected.")

    # Step 1: tell the receiver what file is coming.
    filename_bytes = FILE_TO_SEND.encode("utf-8")
    offer_message = pack_message(MSG_FILE_OFFER, filename_bytes)
    client_socket.sendall(offer_message)
    print(f"Sent FILE_OFFER for '{FILE_TO_SEND}'")

    # Step 2: send the actual file content.
    data_message = pack_message(MSG_FILE_DATA, file_bytes)
    client_socket.sendall(data_message)
    print(f"Sent FILE_DATA - {len(file_bytes)} bytes")

    client_socket.close()
    print("Done.")


if __name__ == "__main__":
    main()
