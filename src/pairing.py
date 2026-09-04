"""
pairing.py

Coordinates the "add a friend" workflow, tying together
connection_string.py (encode/decode), contacts.py (persistence), and
engine.py (which passphrases a listener currently accepts). These
three modules are deliberately decoupled from each other - none of
them know about the others exist - so this module exists specifically
to sequence them correctly, since skipping or misordering a step here
produces a real, confusing bug rather than an obvious crash. Two such
gaps, found by tracing the full workflow through before building any
GUI around it:

  GAP A: generating a connection string doesn't, by itself, register
  that passphrase with your own engine - so the very friend you just
  invited would get rejected when they try to connect, since your
  listener was never told to expect them.

  GAP B: contacts persist to disk correctly, but a FRESH Engine()
  instance (e.g. after restarting the app) starts with empty
  known_passphrases - nothing re-loads previously-paired contacts back
  into it. A friend you successfully paired with yesterday would be
  rejected today, purely because the app restarted in between.

Both are fixed here by making the coordinated sequence the ONLY way
these actions happen, rather than leaving each step as something a
caller (cli.py today, a GUI later) has to remember to do correctly and
in order, every time.
"""

import secrets
import contacts as contacts_module
import connection_string as connection_string_module


def create_invite(engine, my_name: str, my_ip: str, my_port: int, pairing_code: str) -> str:
    """
    The "invite a friend" action, from the side that will be LISTENING
    for the incoming connection. Generates a fresh, long, random
    session passphrase (never typed or spoken by a human - only the
    short pairing_code is), builds the shareable connection string
    containing it, and - fixing Gap A - immediately registers that
    passphrase with THIS engine and saves the contact, so the
    resulting string is guaranteed usable the moment it's shared.

    Returns the connection string to actually copy/share.
    """
    passphrase = secrets.token_urlsafe(24)

    conn_str = connection_string_module.generate_connection_string(
        name=my_name, ip=my_ip, port=my_port, passphrase=passphrase,
        pairing_code=pairing_code,
    )

    engine.set_known_passphrase(my_name, passphrase)
    contacts_module.add_contact(my_name, my_ip, my_port, passphrase)

    return conn_str


def accept_invite(engine, received_string: str, pairing_code: str):
    """
    The "add a friend from their connection string" action, from the
    side that RECEIVED it. Parses the string; on success, saves the
    contact AND registers the passphrase with THIS engine too - since
    the passphrase is shared/symmetric once exchanged, this lets that
    same contact connect back TO you later (a new, unprompted session)
    and still be correctly authenticated and identified, not just the
    connection you're about to make right now.

    Returns the parsed {"name", "ip", "port", "passphrase"} dict on
    success, or None on failure (wrong pairing code, corrupted string -
    see connection_string.parse_connection_string for why this is a
    single generic failure rather than a specific reason).
    """
    parsed = connection_string_module.parse_connection_string(received_string, pairing_code)
    if parsed is None:
        return None

    engine.set_known_passphrase(parsed["name"], parsed["passphrase"])
    contacts_module.add_contact(parsed["name"], parsed["ip"], parsed["port"], parsed["passphrase"])

    return parsed


def load_contacts_into_engine(engine) -> int:
    """
    Fixes Gap B: reads ALL saved contacts and registers each one's
    passphrase with the given engine. Meant to be called ONCE, right
    after creating a fresh Engine() - typically at app startup - so
    previously-paired contacts are immediately recognized again,
    rather than only working until the app happens to restart.

    Returns how many contacts were loaded (useful for a startup log
    message or a quick sanity check in tests).
    """
    all_contacts = contacts_module.load_contacts()
    for name, info in all_contacts.items():
        engine.set_known_passphrase(name, info["passphrase"])
    return len(all_contacts)


if __name__ == "__main__":
    # End-to-end simulation of the full realistic workflow, including
    # the "app restart" scenario that Gap B was hiding.
    from engine import Engine

    # Clean slate for this self-test.
    contacts_module.save_contacts({})

    print("=== Step 1: Alex creates an invite (Alex is the listener) ===")
    engine_alex = Engine()
    invite = create_invite(engine_alex, "Alex", "203.0.113.5", 5001, "6666")
    print(f"Connection string: {invite[:50]}...")
    print(f"Alex's engine now knows passphrases for: {list(engine_alex.known_passphrases.keys())}")
    assert "Alex" in engine_alex.known_passphrases

    print("\n=== Step 2: Sam receives and accepts it ===")
    engine_sam = Engine()
    parsed = accept_invite(engine_sam, invite, "6666")
    print(f"Sam parsed: {parsed}")
    assert parsed is not None
    assert parsed["name"] == "Alex"
    print(f"Sam's engine now knows passphrases for: {list(engine_sam.known_passphrases.keys())}")
    assert "Alex" in engine_sam.known_passphrases

    print("\n=== Step 3: verify contacts were actually saved to disk (not just in memory) ===")
    saved = contacts_module.load_contacts()
    print(f"Contacts on disk: {saved}")
    assert "Alex" in saved

    print("\n=== Step 4: simulate an app restart on ALEX's side (fresh Engine()) ===")
    engine_alex_restarted = Engine()
    print(f"Fresh engine's known_passphrases BEFORE loading contacts: "
          f"{list(engine_alex_restarted.known_passphrases.keys())} (should be empty)")
    assert engine_alex_restarted.known_passphrases == {}

    loaded_count = load_contacts_into_engine(engine_alex_restarted)
    print(f"Loaded {loaded_count} contact(s) into the restarted engine")
    print(f"known_passphrases AFTER loading: {list(engine_alex_restarted.known_passphrases.keys())}")
    assert "Alex" in engine_alex_restarted.known_passphrases  # Alex's OWN contact entry from step 1

    print("\n=== Step 5: wrong pairing code should fail cleanly, register nothing ===")
    engine_eve = Engine()
    failed = accept_invite(engine_eve, invite, "0000")
    print(f"Result with wrong code: {failed}")
    assert failed is None
    assert engine_eve.known_passphrases == {}
    print("Correctly rejected, nothing registered: OK")

    # Clean up after the self-test.
    contacts_module.save_contacts({})
    print("\nAll checks passed.")
