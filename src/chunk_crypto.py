"""
chunk_crypto.py

Wraps AES-256-GCM directly (via the `cryptography` library's AESGCM
class), replacing the earlier use of Fernet for encrypting file
chunks.

WHY THE CHANGE: Fernet base64-encodes its output, which inflates every
encrypted chunk by roughly 33% (base64 always expands data by a fixed
4:3 ratio, regardless of chunk size - see docs/DESIGN.md). For large
files, that's a meaningful amount of wasted bandwidth. AES-GCM used
directly has the same security properties (authenticated encryption -
tampering is still detected, not just "not readable") but only adds a
small FIXED overhead per chunk (a 12-byte nonce + a 16-byte
authentication tag = 28 bytes total), with no multiplicative blow-up
regardless of chunk size.

The session key itself is derived exactly the same way as before
(X25519 exchange -> HKDF) - only the actual encrypt/decrypt mechanism
changes.
"""

import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

# AES-GCM's recommended nonce size. A nonce must NEVER be reused with
# the same key - we generate a fresh random one for every single
# chunk, which is safe as long as chunks are few enough that random
# collisions are vanishingly unlikely (true here: even millions of
# chunks per session are nowhere near the point where random 96-bit
# nonces would be expected to collide).
NONCE_LEN = 12


class ChunkCipher:
    """
    A thin wrapper around AESGCM for our specific use: encrypt/decrypt
    one chunk at a time, each with its own fresh random nonce
    prepended to the ciphertext, so the receiver always has what it
    needs to decrypt without any separate bookkeeping.
    """

    def __init__(self, key: bytes):
        # AESGCM expects a raw key (not base64, unlike Fernet) - 32
        # bytes for AES-256. Our HKDF derivation already produces
        # exactly this, once we stop additionally base64-encoding it
        # for Fernet's sake (see keyexchange.py).
        self._aesgcm = AESGCM(key)

    def encrypt(self, plaintext: bytes) -> bytes:
        """
        Encrypt one chunk. Returns [nonce (12 bytes)][ciphertext+tag],
        ready to send as-is - the nonce travels alongside the
        ciphertext since the receiver needs it to decrypt, and nonces
        aren't secret, only required to be unique per encryption.
        """
        nonce = os.urandom(NONCE_LEN)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext, associated_data=None)
        return nonce + ciphertext

    def decrypt(self, data: bytes) -> bytes:
        """
        Reverse of encrypt(): split off the nonce, decrypt the rest.
        Raises InvalidTag if the data was tampered with or corrupted -
        callers should catch this the same way they'd have caught
        Fernet's InvalidToken.
        """
        nonce = data[:NONCE_LEN]
        ciphertext = data[NONCE_LEN:]
        return self._aesgcm.decrypt(nonce, ciphertext, associated_data=None)


if __name__ == "__main__":
    # Quick round-trip + overhead comparison, run directly for a sanity check.
    key = AESGCM.generate_key(bit_length=256)
    cipher = ChunkCipher(key)

    plaintext = os.urandom(1024 * 1024)  # 1 MB, same as our real chunk size
    encrypted = cipher.encrypt(plaintext)
    decrypted = cipher.decrypt(encrypted)

    print(f"Plaintext size:  {len(plaintext)} bytes")
    print(f"Encrypted size:  {len(encrypted)} bytes")
    print(f"Overhead:        {len(encrypted) - len(plaintext)} bytes "
          f"({(len(encrypted) / len(plaintext) - 1) * 100:.4f}%)")
    print(f"Round-trip matches: {decrypted == plaintext}")

    # Tampering should be caught.
    tampered = encrypted[:-1] + bytes([encrypted[-1] ^ 0xFF])
    try:
        cipher.decrypt(tampered)
        print("TAMPERING NOT DETECTED - this would be a bug")
    except InvalidTag:
        print("Tampering correctly detected (InvalidTag raised)")
