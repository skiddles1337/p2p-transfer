"""
contacts.py

Persists saved contacts (people you've connected with before) to disk,
independent of engine.py or any UI. Each contact stores: name, ip,
port, passphrase, paired_at (a timestamp - see pairing.py's
get_contact_freshness() for how this becomes a "this needs re-pairing"
staleness nudge in the GUI).

Uses the same platformdirs-based approach as storage.py, rather than
"a JSON file next to the app" (fragile for a packaged, double-clicked
executable - see storage.py's docstring for why). Contacts are
configuration, not downloaded content, so they live in a config
directory rather than alongside downloads.
"""

import os
import time
import platformdirs
import json_store

APP_NAME = "p2p-transfer"
CONTACTS_PATH = os.path.join(platformdirs.user_config_dir(APP_NAME, appauthor=False), "contacts.json")


def load_contacts() -> dict:
    """
    Return all saved contacts as {name: {"ip": ..., "port": ...,
    "passphrase": ..., "paired_at": ...}}. Returns an empty dict if no
    contacts file exists yet (first run), or if the file is somehow
    corrupted - see json_store.safe_load_json for how that's handled.
    """
    return json_store.safe_load_json(CONTACTS_PATH, default={})


def save_contacts(contacts: dict) -> None:
    """
    Overwrite the contacts file with the given full set, atomically -
    see json_store.atomic_write_json. Callers typically load, modify
    the dict, then save - see add_contact/remove_contact below for the
    common single-contact case.
    """
    json_store.atomic_write_json(CONTACTS_PATH, contacts)


def add_contact(name: str, ip: str, port: int, passphrase: str) -> None:
    """
    Add or update (if the name already exists) a single contact.
    Always stamps paired_at with the CURRENT time - this is
    deliberate: re-pairing with someone you already know (e.g. after
    their IP changed, or just because it's been a while) naturally
    resets their staleness clock, with no separate "renew" action
    needed - the act of adding/updating IS the renewal.
    """
    contacts = load_contacts()
    contacts[name] = {
        "ip": ip,
        "port": port,
        "passphrase": passphrase,
        "paired_at": time.time(),
    }
    save_contacts(contacts)


def remove_contact(name: str) -> bool:
    """Remove a contact by name. Returns True if it existed and was
    removed, False if there was no such contact."""
    contacts = load_contacts()
    if name not in contacts:
        return False
    del contacts[name]
    save_contacts(contacts)
    return True


def get_contact(name: str) -> dict | None:
    """Look up a single contact by name, or None if not found."""
    return load_contacts().get(name)


if __name__ == "__main__":
    # Quick round-trip self-test: add, verify persistence by reloading
    # from disk (not just checking the in-memory dict), remove, confirm gone.
    print(f"Contacts file location: {CONTACTS_PATH}")

    add_contact("Alex", "82.14.55.10", 5001, "correct-horse-battery")
    add_contact("Sam", "10.0.0.5", 5002, "another-passphrase")

    reloaded = load_contacts()
    print(f"After adding two contacts, reloaded from disk: {reloaded}")

    assert reloaded["Alex"]["ip"] == "82.14.55.10"
    assert reloaded["Sam"]["port"] == 5002
    assert "paired_at" in reloaded["Alex"]
    assert "paired_at" in reloaded["Sam"]
    print("Contacts match expected values, paired_at present: OK")

    removed = remove_contact("Alex")
    print(f"Removed 'Alex': {removed}")

    after_removal = load_contacts()
    print(f"After removal, reloaded from disk: {after_removal}")
    assert "Alex" not in after_removal
    assert "Sam" in after_removal
    print("Removal correctly persisted: OK")

    # Clean up after the self-test so repeated runs start fresh.
    remove_contact("Sam")
    print("Self-test cleanup complete.")
