"""
connection_string.py

Builds and parses the shareable "connection string" a person pastes
into Discord (or texts, says aloud, etc.) to hand their friend
everything needed to connect - see docs/DESIGN.md's layered security
design for the full reasoning. Two DIFFERENT secrets are involved here,
deliberately:

  - The short PAIRING CODE (e.g. "6666") - said aloud, typed by both
    people. Its only job is encrypting this connection string, so the
    IP/port/name aren't sitting in plaintext in a Discord chat log.
  - The actual session PASSPHRASE (auto-generated, long, random) -
    carried INSIDE the encrypted string. This is what actually gets
    used for the handshake's confirmation tags (see auth.py) - never
    the weak pairing code directly. This is what gives the real
    handshake meaningful strength even though the pairing code is
    deliberately simple enough to say out loud.

Reuses chunk_crypto.ChunkCipher (AES-256-GCM) for the actual
encryption - no need for a second encryption mechanism when the
existing one already does exactly what's needed here (encrypt a small
blob, authenticate it, done).
"""

import json
import base64
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from chunk_crypto import ChunkCipher

MARKER = "----"

# A fixed salt is a deliberate, informed choice here, not an oversight:
# PBKDF2's salt exists to stop an attacker from precomputing one
# lookup table that works against every possible use of this scheme
# everywhere - but a connection string is short-lived and single-use
# by design, so that precomputation concern matters far less here than
# it would for something like a stored password hash. A random salt
# would need to travel WITH the string in plaintext (salts aren't
# secret), adding complexity for a use case that doesn't need it.
_PBKDF2_SALT = b"p2p-transfer-connection-string-v1"
_PBKDF2_ITERATIONS = 200_000


def _derive_key_from_pairing_code(pairing_code: str) -> bytes:
    """
    Turn a short, human-typed pairing code into a proper 32-byte key
    suitable for AES-256-GCM. PBKDF2 deliberately makes this slow
    (200,000 iterations) - since the pairing code itself is short and
    guessable, this doesn't make it strong, but it does make brute-
    forcing meaningfully more expensive per guess than a plain hash
    would, for whatever that's worth against an offline attacker.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_PBKDF2_SALT,
        iterations=_PBKDF2_ITERATIONS,
    )
    return kdf.derive(pairing_code.encode("utf-8"))


def generate_connection_string(name: str, ip: str, port: int,
                                passphrase: str, pairing_code: str) -> str:
    """
    Build the shareable string: encrypts {name, ip, port, passphrase}
    using a key derived from the short pairing code, base64-encodes
    the result so it's safe to paste as plain text, and wraps it in
    ---- markers so it's easy to visually spot and select in a chat.
    """
    payload = json.dumps({
        "name": name,
        "ip": ip,
        "port": port,
        "passphrase": passphrase,
    }).encode("utf-8")

    key = _derive_key_from_pairing_code(pairing_code)
    cipher = ChunkCipher(key)
    encrypted = cipher.encrypt(payload)

    encoded = base64.urlsafe_b64encode(encrypted).decode("ascii")
    return f"{MARKER}{encoded}{MARKER}"


def parse_connection_string(connection_string: str, pairing_code: str):
    """
    Reverse of generate_connection_string(). Returns
    {"name", "ip", "port", "passphrase"} on success, or None on ANY
    failure - wrong pairing code, corrupted/truncated string, not
    actually a connection string at all.

    Deliberately returns a single generic None rather than
    distinguishing WHY it failed: telling an attacker "that was a
    valid string but the wrong code" versus "that wasn't even a valid
    string" hands them a free signal to calibrate a guessing attack
    against. A legitimate user who mistypes the code just needs to
    know "didn't work, try again" - the same message covers everything.
    """
    text = connection_string.strip()
    if not (text.startswith(MARKER) and text.endswith(MARKER)):
        return None

    encoded = text[len(MARKER):-len(MARKER)]

    try:
        encrypted = base64.urlsafe_b64decode(encoded.encode("ascii"))
        key = _derive_key_from_pairing_code(pairing_code)
        cipher = ChunkCipher(key)
        payload = cipher.decrypt(encrypted)
        data = json.loads(payload.decode("utf-8"))

        # Basic shape validation - a decrypted-but-malformed payload
        # (shouldn't normally happen, since AES-GCM's authentication
        # would already have rejected tampered ciphertext, but cheap
        # to double check) still just returns None, same as any other
        # failure mode.
        if not all(k in data for k in ("name", "ip", "port", "passphrase")):
            return None

        return data

    except Exception:
        # Deliberately broad: ANY failure (bad base64, decryption
        # failure, malformed JSON) collapses to the same generic
        # "didn't work" outcome, for the reason explained above.
        return None


if __name__ == "__main__":
    print("=== Round-trip test ===")
    pairing_code = "6666"
    generated = generate_connection_string(
        name="Alex", ip="82.14.55.10", port=5001,
        passphrase="a-long-random-session-passphrase", pairing_code=pairing_code
    )
    print(f"Generated string: {generated}")
    print(f"Length: {len(generated)} characters")

    parsed = parse_connection_string(generated, pairing_code)
    print(f"Parsed with correct code: {parsed}")
    assert parsed == {
        "name": "Alex", "ip": "82.14.55.10", "port": 5001,
        "passphrase": "a-long-random-session-passphrase",
    }
    print("Round-trip matches: OK")

    print("\n=== Wrong pairing code ===")
    wrong = parse_connection_string(generated, "0000")
    print(f"Parsed with WRONG code: {wrong}")
    assert wrong is None
    print("Correctly rejected: OK")

    print("\n=== Garbage input ===")
    garbage = parse_connection_string("not a connection string at all", pairing_code)
    print(f"Parsed garbage: {garbage}")
    assert garbage is None
    print("Correctly rejected: OK")

    print("\n=== Corrupted (tampered) string ===")
    tampered = generated[:-10] + "XXXXXXXXXX"
    tampered_result = parse_connection_string(tampered, pairing_code)
    print(f"Parsed tampered string: {tampered_result}")
    assert tampered_result is None
    print("Correctly rejected: OK")
