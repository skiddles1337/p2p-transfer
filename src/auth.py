"""
auth.py

Handles the passphrase challenge/response used to authenticate a
session, without ever transmitting the passphrase itself over the
wire.

TEMPORARY: SHARED_PASSPHRASE is hardcoded here for now, standing in
for the real design (a short code typed fresh per session, e.g. "6666"
said aloud over Discord). Wiring that up is GUI-phase work - see
docs/DESIGN.md. Both sender.py and listener.py import from here so
they can never accidentally disagree on the passphrase or the HMAC
logic.
"""

import hmac
import hashlib

# TEMPORARY - both sides must currently share this exact value.
SHARED_PASSPHRASE = "6666"


def compute_response(challenge: bytes) -> bytes:
    """
    Given a challenge (random bytes from the listener), compute the
    proof-of-passphrase-knowledge response: an HMAC-SHA256 of the
    challenge, keyed by the shared passphrase.

    HMAC (keyed-hash message authentication code) is a standard,
    well-vetted construction for exactly this purpose - proving you
    know a secret, without revealing the secret itself. Both sides can
    independently compute this same value if (and only if) they have
    the same passphrase; there's no way to work backwards from the
    response to recover the passphrase.
    """
    key = SHARED_PASSPHRASE.encode("utf-8")
    return hmac.new(key, challenge, hashlib.sha256).digest()
