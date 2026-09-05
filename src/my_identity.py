"""
my_identity.py

Persists the user's own default identity info (display name, default
listening port) so the "quick share" workflow (see pairing.py's
quick_share()) doesn't require retyping these every time.

Deliberately does NOT store:
  - the public IP - always fetched fresh at the moment of sharing (see
    network_info.get_public_ip()), since a cached value could go stale
    the moment your home IP changes, silently breaking for whoever
    receives an invite built from it - the exact "stale IP" problem
    we designed around earlier.
  - the pairing code - meant to stay a short-lived, spoken/typed
    secret for one exchange, not a long-term stored value; persisting
    it indefinitely would undermine the whole reason it's kept simple.
"""

import os
import platformdirs
import json_store

APP_NAME = "p2p-transfer"
IDENTITY_PATH = os.path.join(platformdirs.user_config_dir(APP_NAME, appauthor=False), "identity.json")

_DEFAULTS = {"name": "", "port": 5001}


def get_identity() -> dict:
    """Return {"name": ..., "port": ...}, falling back to defaults for
    any field never explicitly set (e.g. a genuine first run)."""
    data = json_store.safe_load_json(IDENTITY_PATH, default={})
    merged = dict(_DEFAULTS)
    merged.update(data)
    return merged


def set_identity(name: str = None, port: int = None) -> None:
    """
    Update one or both fields, leaving the other untouched if not
    given - so a GUI can save just the name field changing without
    needing to also know/resend the current port, and vice versa.
    """
    current = get_identity()
    if name is not None:
        current["name"] = name
    if port is not None:
        current["port"] = port
    json_store.atomic_write_json(IDENTITY_PATH, current)


if __name__ == "__main__":
    print(f"Identity file location: {IDENTITY_PATH}")

    print(f"Defaults before anything is set: {get_identity()}")
    assert get_identity() == {"name": "", "port": 5001}

    set_identity(name="Alex")
    print(f"After setting only name: {get_identity()}")
    assert get_identity()["name"] == "Alex"
    assert get_identity()["port"] == 5001  # untouched

    set_identity(port=6001)
    print(f"After setting only port: {get_identity()}")
    assert get_identity()["name"] == "Alex"  # untouched
    assert get_identity()["port"] == 6001

    # Clean slate for next run.
    json_store.atomic_write_json(IDENTITY_PATH, dict(_DEFAULTS))
    print("Self-test cleanup complete.")
