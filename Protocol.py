"""
protocol.py

Defines the message envelope format used for all communication between
peers. Every message on the wire looks like:

    [ 1 byte  : message type   ]
    [ 8 bytes : payload length ]
    [ N bytes : payload        ]

This module only deals with turning a (type, payload) pair into bytes,
and back again. It knows nothing about sockets or files yet.
"""

import struct

# --- Message types ---
# Each is just a distinct integer (0-255, since we're using 1 byte).
# We'll add more of these in later steps (FILE_OFFER, FILE_CHUNK, etc.)
MSG_HELLO = 1

# struct format string for the header:
#   "!" = big-endian (network byte order, the standard for network protocols)
#   "B" = 1 byte, unsigned  (the message type)
#   "Q" = 8 bytes, unsigned (the payload length)
HEADER_FORMAT = "!BQ"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # should be 9


def pack_message(msg_type: int, payload: bytes) -> bytes:
    """
    Turn a message type + payload into the full bytes to send on the wire.
    """
    header = struct.pack(HEADER_FORMAT, msg_type, len(payload))
    return header + payload


def unpack_header(header_bytes: bytes) -> tuple[int, int]:
    """
    Given exactly HEADER_SIZE bytes, return (msg_type, payload_length).
    """
    msg_type, payload_length = struct.unpack(HEADER_FORMAT, header_bytes)
    return msg_type, payload_length


if __name__ == "__main__":
    # Quick manual test: pack a message, then unpack it, and check
    # we get back what we put in.
    test_payload = b"hello world"
    packed = pack_message(MSG_HELLO, test_payload)

    print(f"Packed bytes ({len(packed)} total): {packed}")

    header_part = packed[:HEADER_SIZE]
    payload_part = packed[HEADER_SIZE:]

    msg_type, payload_length = unpack_header(header_part)
    print(f"Unpacked header -> type: {msg_type}, length: {payload_length}")
    print(f"Payload matches original: {payload_part == test_payload}")