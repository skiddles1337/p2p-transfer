"""
auth.py

Handles proving both sides of a key exchange share the same
passphrase, WITHOUT the passphrase ever touching the wire, and WITHOUT
just trusting the exchanged public keys blindly (which alone would be
vulnerable to a man-in-the-middle - see docs/DESIGN.md).

TEMPORARY: SHARED_PASSPHRASE is hardcoded here for now, standing in
for the real design (a short code typed fresh per session, e.g. "6666"
said aloud over Discord). Wiring that up is GUI-phase work.
"""

import hmac
import hashlib

# TEMPORARY - both sides must currently share this exact value.
SHARED_PASSPHRASE = "6666"


def compute_confirmation_tag(first_pubkey_bytes: bytes,
                              second_pubkey_bytes: bytes) -> bytes:
    """
    Compute a confirmation tag binding the passphrase to a SPECIFIC
    pair of exchanged public keys: HMAC-SHA256(passphrase,
    first_pubkey_bytes + second_pubkey_bytes).

    This is what closes the man-in-the-middle gap in a raw
    Diffie-Hellman exchange: knowing the passphrase isn't enough for
    an attacker to forge a valid tag for keys they substituted in,
    since the tag is tied to the EXACT bytes of both public keys
    actually in play. Anyone tampering with either key in transit
    causes the recomputed tag to mismatch.

    IMPORTANT: the two sides must agree on ORDER (whose key comes
    first) when computing a given tag, and the two tags used in a
    single handshake (one each direction) should use DIFFERENT
    orderings - otherwise an attacker could simply echo one side's
    tag back as if it were their own valid response (a "reflection
    attack").
    """
    key = SHARED_PASSPHRASE.encode("utf-8")
    message = first_pubkey_bytes + second_pubkey_bytes
    return hmac.new(key, message, hashlib.sha256).digest()


def verify_confirmation_tag(tag: bytes, first_pubkey_bytes: bytes,
                             second_pubkey_bytes: bytes) -> bool:
    """
    Recompute the expected tag for the given public key pair (in the
    given order) and compare against a received tag. Uses a
    constant-time comparison (see listener.py's earlier handshake for
    why that matters) rather than a plain ==.
    """
    expected = compute_confirmation_tag(first_pubkey_bytes, second_pubkey_bytes)
    return hmac.compare_digest(tag, expected)
