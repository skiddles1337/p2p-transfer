"""
auth.py

Handles proving both sides of a key exchange share the same
passphrase, WITHOUT the passphrase ever touching the wire, and WITHOUT
just trusting the exchanged public keys blindly (which alone would be
vulnerable to a man-in-the-middle - see docs/DESIGN.md).

Passphrases are now per-relationship, not a single global value: you
and one friend might agree on "6666", while you and a different friend
use something else entirely. This matters most for a LISTENER, which
doesn't know in advance who's about to connect - it has to try each
known passphrase and see which one (if any) produces a matching tag.
As a side effect, whichever passphrase matches also tells you WHO is
connecting, before they've said anything else.
"""

import hmac
import hashlib


def compute_confirmation_tag(passphrase: str, first_pubkey_bytes: bytes,
                              second_pubkey_bytes: bytes) -> bytes:
    """
    Compute a confirmation tag binding a SPECIFIC passphrase to a
    SPECIFIC pair of exchanged public keys: HMAC-SHA256(passphrase,
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
    key = passphrase.encode("utf-8")
    message = first_pubkey_bytes + second_pubkey_bytes
    return hmac.new(key, message, hashlib.sha256).digest()


def verify_confirmation_tag(tag: bytes, passphrase: str, first_pubkey_bytes: bytes,
                             second_pubkey_bytes: bytes) -> bool:
    """
    Recompute the expected tag for the given passphrase and public key
    pair (in the given order), and compare against a received tag.
    Uses a constant-time comparison rather than a plain == , since a
    normal byte comparison can leak timing information about where a
    mismatch occurs.
    """
    expected = compute_confirmation_tag(passphrase, first_pubkey_bytes, second_pubkey_bytes)
    return hmac.compare_digest(tag, expected)


def find_matching_passphrase(tag: bytes, first_pubkey_bytes: bytes,
                              second_pubkey_bytes: bytes,
                              candidates: dict[str, str]) -> str | None:
    """
    Given a received tag and a set of candidate passphrases (as a
    {name: passphrase} mapping - e.g. your saved contacts), find which
    one (if any) produces a matching tag for this specific public key
    pair. Returns the matching NAME, or None if nothing matches.

    This is the core of listener-side authentication: since a listener
    doesn't know in advance who's connecting, it tries each known
    passphrase in turn. Whichever one matches both authenticates the
    connection AND identifies who it is - a nice two-for-one.

    Honest limitation: checking candidates one at a time like this
    means the TOTAL time taken can, in principle, reveal something
    about how many candidates were tried before a match (or that none
    matched) - a coarser signal than the per-comparison timing that
    hmac.compare_digest protects against, but a real one for a very
    large contact list under adversarial conditions. Not a practical
    concern for a personal, friends-only contact list of a handful of
    entries; worth knowing about if this app's usage ever changes.
    """
    for name, passphrase in candidates.items():
        if verify_confirmation_tag(tag, passphrase, first_pubkey_bytes, second_pubkey_bytes):
            return name
    return None
