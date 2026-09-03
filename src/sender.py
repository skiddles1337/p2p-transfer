"""
sender.py

Connects to a listener and sends a real file, broken into fixed-size
chunks: first a FILE_OFFER message (filename + total filesize), then
a sequence of FILE_CHUNK messages until the whole file has been sent.

No hashing or encryption yet - see protocol.py for what's coming next.

Run listener.py first (in one terminal), then run this script (in a
second terminal) while the listener is waiting.
"""

import socket
import os
from protocol import (
    pack_message,
    pack_file_offer,
    MSG_FILE_OFFER,
    MSG_FILE_CHUNK,
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

    # Step 1: tell the receiver what's coming - filename AND filesize
    # this time, so it knows when to expect the transfer to be done.
    offer_payload = pack_file_offer(FILE_TO_SEND, filesize)
    offer_message = pack_message(MSG_FILE_OFFER, offer_payload)
    client_socket.sendall(offer_message)
    print(f"Sent FILE_OFFER - filename='{FILE_TO_SEND}', size={filesize}")

    # Step 2: send the file in fixed-size chunks, instead of reading
    # the whole thing into memory at once.
    bytes_sent = 0
    chunk_count = 0

    with open(FILE_TO_SEND, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)

            # An empty read means we've reached the end of the file -
            # nothing left to send.
            if chunk == b"":
                break

            chunk_message = pack_message(MSG_FILE_CHUNK, chunk)
            client_socket.sendall(chunk_message)

            bytes_sent += len(chunk)
            chunk_count += 1
            print(f"  Sent chunk {chunk_count} ({len(chunk)} bytes, "
                  f"{bytes_sent}/{filesize} total)")

    print(f"Done sending. {chunk_count} chunks, {bytes_sent} bytes total.")

    client_socket.close()
    print("Connection closed.")


if __name__ == "__main__":
    main()
