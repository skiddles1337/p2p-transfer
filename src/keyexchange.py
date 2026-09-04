"""
keyexchange.py

Wraps the X25519 Diffie-Hellman key exchange primitives we'll use for
session encryption. This file starts as an ISOLATED PROOF that the
core math works - two independently generated keypairs, exchanging
only their PUBLIC keys, land on the identical shared secret - before
we wire any of this into the real handshake in sender.py/listener.py.

X25519 is a specific, modern, widely-trusted implementation of
elliptic-curve Diffie-Hellman (the same underlying idea used in
TLS/HTTPS). We're not implementing any cryptographic math ourselves -
just correctly using a vetted library's implementation of it, same
spirit as how we've used hashlib and hmac.
"""

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import base64


def generate_keypair():
    """
    Generate a fresh, random X25519 private key (and its matching
    public key). This should be called ONCE PER SESSION, not reused
    across sessions - a fresh keypair every time is exactly what gives
    us forward secrecy (see docs/DESIGN.md).
    """
    private_key = X25519PrivateKey.generate()
    public_key = private_key.public_key()
    return private_key, public_key


def public_key_to_bytes(public_key: X25519PublicKey) -> bytes:
    """
    Convert a public key object into raw bytes, suitable for sending
    over the wire as a message payload. X25519 public keys are always
    exactly 32 bytes.
    """
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def public_key_from_bytes(data: bytes) -> X25519PublicKey:
    """
    Reconstruct a public key object from raw bytes received over the
    wire (the reverse of public_key_to_bytes).
    """
    return X25519PublicKey.from_public_bytes(data)


def compute_shared_secret(own_private_key: X25519PrivateKey,
                           peer_public_key: X25519PublicKey) -> bytes:
    """
    Compute the shared secret: combining YOUR private key with the
    OTHER side's public key. This is the core Diffie-Hellman property -
    when the other side does the same thing in reverse (their private
    key + your public key), they arrive at the exact same result,
    despite neither side ever transmitting a private key or the
    secret itself.
    """
    return own_private_key.exchange(peer_public_key)


def derive_session_key(shared_secret: bytes) -> bytes:
    """
    Turn the raw Diffie-Hellman shared secret into a proper encryption
    key, using HKDF (HMAC-based Key Derivation Function).

    Why not just use the shared secret directly as the encryption key?
    A raw DH shared secret isn't guaranteed to be UNIFORMLY RANDOM
    across all possible bit patterns the way a good encryption key
    should be - it's a mathematical byproduct of the curve arithmetic,
    which can have subtle structure/bias. HKDF takes that secret as
    input and produces output that's been properly "whitened" into
    something safe to use directly as a cryptographic key. This is
    the standard, expected step between "I have a shared secret" and
    "I have an encryption key" in any real protocol using DH exchange.

    Returns bytes already formatted as a valid Fernet key (Fernet
    requires a 32-byte key, base64-encoded in its "urlsafe" variant).
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"p2p-transfer session key",
    )
    raw_key = hkdf.derive(shared_secret)
    return base64.urlsafe_b64encode(raw_key)


if __name__ == "__main__":
    from auth import compute_confirmation_tag, verify_confirmation_tag

    def run_exchange(listener_passphrase_override=None, tamper=False):
        """
        Simulate a full authenticated exchange. If tamper=True,
        simulate an attacker swapping in a different public key after
        the sender computed its tag - this should be caught.
        """
        listener_private, listener_public = generate_keypair()
        sender_private, sender_public = generate_keypair()

        listener_public_bytes = public_key_to_bytes(listener_public)
        sender_public_bytes = public_key_to_bytes(sender_public)

        # Sender computes its confirmation tag over (listener_pub, sender_pub).
        sender_tag = compute_confirmation_tag(listener_public_bytes, sender_public_bytes)

        transmitted_sender_public_bytes = sender_public_bytes
        if tamper:
            # Simulate an attacker substituting a different public key
            # in transit, AFTER the tag was computed over the original.
            _, attacker_public = generate_keypair()
            transmitted_sender_public_bytes = public_key_to_bytes(attacker_public)

        # Listener verifies using whatever bytes actually "arrived".
        ok = verify_confirmation_tag(
            sender_tag, listener_public_bytes, transmitted_sender_public_bytes
        )
        print(f"{'[TAMPERED] ' if tamper else ''}Listener verifies sender's tag: "
              f"{'OK' if ok else 'REJECTED'}")
        return ok

    print("=== Test 1: normal exchange, no tampering ===")
    run_exchange(tamper=False)

    print("\n=== Test 2: simulated key substitution in transit ===")
    run_exchange(tamper=True)

    print("\n=== Test 3: original raw shared-secret proof (no auth) ===")
    listener_private, listener_public = generate_keypair()
    sender_private, sender_public = generate_keypair()
    listener_public_bytes = public_key_to_bytes(listener_public)
    sender_public_bytes = public_key_to_bytes(sender_public)
    received_sender_public = public_key_from_bytes(sender_public_bytes)
    received_listener_public = public_key_from_bytes(listener_public_bytes)
    listener_secret = compute_shared_secret(listener_private, received_sender_public)
    sender_secret = compute_shared_secret(sender_private, received_listener_public)
    print(f"Shared secrets match: {listener_secret == sender_secret}")

