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
import hashlib

# --- Message types ---
# Each is just a distinct integer (0-255, since we're using 1 byte).
MSG_HELLO = 1
MSG_FILE_OFFER = 2  # payload: filesize (8 bytes) + filename (UTF-8 bytes)
MSG_FILE_DATA = 3   # payload: the raw file content (whole file) - being
                     # replaced by chunked transfer, kept for reference
MSG_FILE_CHUNK = 4  # payload: chunk index (4 bytes) + chunk hash (32 bytes)
                     # + chunk data (remaining bytes)
MSG_DONE = 5        # payload: SHA-256 hash of the ENTIRE file (32 bytes) -
                     # sent once, after all chunks, to signal "that's
                     # everything" and let the receiver do a final check

# How many bytes we read/send per chunk. 1 MB is a reasonable default -
# big enough to be efficient, small enough to keep memory usage low
# and give frequent progress/checkpoint opportunities.
CHUNK_SIZE = 1024 * 1024

# struct format just for the filesize field within a FILE_OFFER payload.
# Same idea as the main header format, just reused for this sub-piece.
FILE_OFFER_SIZE_FORMAT = "!Q"
FILE_OFFER_SIZE_FIELD_LEN = struct.calcsize(FILE_OFFER_SIZE_FORMAT)  # 8

# struct format for the chunk index field within a FILE_CHUNK payload.
# "I" = 4-byte unsigned int - plenty of range (over 4 billion chunks,
# which at 1MB each is far more data than we'll ever realistically send).
CHUNK_INDEX_FORMAT = "!I"
CHUNK_INDEX_LEN = struct.calcsize(CHUNK_INDEX_FORMAT)  # 4

# SHA-256 hashes are always exactly 32 bytes, regardless of input size.
# We can ask hashlib itself for this number rather than hardcoding it.
CHUNK_HASH_LEN = hashlib.sha256().digest_size  # 32

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


def pack_file_offer(filename: str, filesize: int) -> bytes:
    """
    Build the payload for a FILE_OFFER message: a fixed-size filesize
    field, followed by the filename (whatever's left over).

    Putting the fixed-size field FIRST means the receiver always knows
    exactly where it ends - the filename is simply "everything after
    that point", no separate length field needed for it.
    """
    size_bytes = struct.pack(FILE_OFFER_SIZE_FORMAT, filesize)
    filename_bytes = filename.encode("utf-8")
    return size_bytes + filename_bytes


def unpack_file_offer(payload: bytes) -> tuple[str, int]:
    """
    Given a FILE_OFFER payload, return (filename, filesize).
    """
    size_bytes = payload[:FILE_OFFER_SIZE_FIELD_LEN]
    filename_bytes = payload[FILE_OFFER_SIZE_FIELD_LEN:]

    (filesize,) = struct.unpack(FILE_OFFER_SIZE_FORMAT, size_bytes)
    filename = filename_bytes.decode("utf-8")

    return filename, filesize


def pack_file_chunk(chunk_index: int, chunk_data: bytes) -> bytes:
    """
    Build the payload for a FILE_CHUNK message:

        [ 4 bytes  : chunk index ]
        [ 32 bytes : SHA-256 hash of chunk_data ]
        [ remaining bytes : chunk_data itself ]

    The index lets both sides refer to "chunk #7" unambiguously (useful
    later for requesting a specific chunk be resent). The hash lets the
    receiver verify this specific chunk arrived intact.
    """
    index_bytes = struct.pack(CHUNK_INDEX_FORMAT, chunk_index)
    chunk_hash = hashlib.sha256(chunk_data).digest()
    return index_bytes + chunk_hash + chunk_data


def unpack_file_chunk(payload: bytes) -> tuple[int, bytes, bytes]:
    """
    Given a FILE_CHUNK payload, return (chunk_index, chunk_hash, chunk_data).

    Note: this does NOT verify the hash - it just splits the payload
    into its three parts. Verifying is the caller's job (compare
    chunk_hash against hashlib.sha256(chunk_data).digest()).
    """
    index_bytes = payload[:CHUNK_INDEX_LEN]
    hash_bytes = payload[CHUNK_INDEX_LEN:CHUNK_INDEX_LEN + CHUNK_HASH_LEN]
    chunk_data = payload[CHUNK_INDEX_LEN + CHUNK_HASH_LEN:]

    (chunk_index,) = struct.unpack(CHUNK_INDEX_FORMAT, index_bytes)

    return chunk_index, hash_bytes, chunk_data


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
