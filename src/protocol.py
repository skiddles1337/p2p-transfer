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
MSG_HELLO = 1
MSG_FILE_OFFER = 2  # payload: filename, as UTF-8 bytes
MSG_FILE_DATA = 3   # payload: the raw file content (whole file, for now)

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


def recv_exact(sock, num_bytes: int) -> bytes:
    """
    Read exactly num_bytes from a socket, even if it takes multiple
    recv() calls to get there.

    A single sock.recv(n) call is only a REQUEST for up to n bytes -
    the OS is free to hand back fewer, especially on slower or busier
    connections. So we keep calling recv() in a loop, accumulating
    bytes, until we've collected exactly what we asked for.
    """
    chunks = []
    bytes_received = 0

    while bytes_received < num_bytes:
        remaining = num_bytes - bytes_received
        chunk = sock.recv(remaining)

        if chunk == b"":
            # An empty result means the other side closed the connection.
            raise ConnectionError(
                f"Socket closed before receiving all data "
                f"(got {bytes_received} of {num_bytes} bytes)"
            )

        chunks.append(chunk)
        bytes_received += len(chunk)

    return b"".join(chunks)


def recv_message(sock) -> tuple[int, bytes]:
    """
    Read one full message (header + payload) from a socket.
    Returns (msg_type, payload).
    """
    header_bytes = recv_exact(sock, HEADER_SIZE)
    msg_type, payload_length = unpack_header(header_bytes)
    payload = recv_exact(sock, payload_length)
    return msg_type, payload


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
