"""
my_identity.py

Persists the user's own default identity info (display name, default
listening port) so the "quick share" workflow (see pairing.py's
quick_share()) doesn't require retyping these every time.

The public IP is a special case, deliberately NOT treated the same
way as name/port:
  - By default (ip_auto=True), it's always fetched fresh at the moment
    of sharing (see network_info.get_public_ip()), never cached here -
    a cached value could go stale the moment your home IP changes,
    silently breaking for whoever receives an invite built from it.
  - BUT if auto-detection genuinely doesn't work for someone's setup,
    they need a manual override that actually persists - otherwise
    they'd have to retype their IP every single launch. ip_auto=False
    means "use manual_ip instead of trying to auto-detect."
  - manual_ip is stored even while ip_auto is True (so switching auto
    off later doesn't lose whatever was last typed there) - it's just
    not the ACTIVE value used unless ip_auto is explicitly off.

Deliberately does NOT store the pairing code - meant to stay a
short-lived, spoken/typed secret for one exchange, not a long-term
stored value; persisting it indefinitely would undermine the whole
reason it's kept simple.
"""

import os
import platformdirs
import json_store

APP_NAME = "p2p-transfer"
IDENTITY_PATH = os.path.join(platformdirs.user_config_dir(APP_NAME, appauthor=False), "identity.json")

_DEFAULTS = {"name": "", "port": 5001, "ip_auto": True, "manual_ip": "", "auto_listen": False}


def get_identity() -> dict:
    """Return {"name", "port", "ip_auto", "manual_ip", "auto_listen"},
    falling back to defaults for any field never explicitly set (e.g.
    a genuine first run)."""
    data = json_store.safe_load_json(IDENTITY_PATH, default={})
    merged = dict(_DEFAULTS)
    merged.update(data)
    return merged


def set_identity(name: str = None, port: int = None,
                  ip_auto: bool = None, manual_ip: str = None,
                  auto_listen: bool = None) -> None:
    """
    Update any subset of fields, leaving the others untouched if not
    given - so a GUI can save just one field changing (e.g. just the
    port, on that field losing focus) without needing to also know/
    resend every other current value.
    """
    current = get_identity()
    if name is not None:
        current["name"] = name
    if port is not None:
        current["port"] = port
    if ip_auto is not None:
        current["ip_auto"] = ip_auto
    if manual_ip is not None:
        current["manual_ip"] = manual_ip
    if auto_listen is not None:
        current["auto_listen"] = auto_listen
    json_store.atomic_write_json(IDENTITY_PATH, current)


if __name__ == "__main__":
    print(f"Identity file location: {IDENTITY_PATH}")

    print(f"Defaults before anything is set: {get_identity()}")
    assert get_identity() == {"name": "", "port": 5001, "ip_auto": True,
                              "manual_ip": "", "auto_listen": False}

    set_identity(name="Alex")
    print(f"After setting only name: {get_identity()}")
    assert get_identity()["name"] == "Alex"
    assert get_identity()["port"] == 5001  # untouched

    set_identity(port=6001)
    print(f"After setting only port: {get_identity()}")
    assert get_identity()["name"] == "Alex"  # untouched
    assert get_identity()["port"] == 6001

    print("\n=== IP auto/manual override ===")
    set_identity(ip_auto=False, manual_ip="203.0.113.7")
    identity = get_identity()
    print(f"After setting manual IP: {identity}")
    assert identity["ip_auto"] is False
    assert identity["manual_ip"] == "203.0.113.7"

    # Switching back to auto should NOT wipe out the manual value -
    # someone might toggle back and forth if auto-detection is flaky.
    set_identity(ip_auto=True)
    identity = get_identity()
    print(f"After switching back to auto: {identity}")
    assert identity["ip_auto"] is True
    assert identity["manual_ip"] == "203.0.113.7"  # preserved, just not active
    print("Manual IP preserved even while auto mode is on: OK")

    print("\n=== auto_listen ===")
    set_identity(auto_listen=True)
    assert get_identity()["auto_listen"] is True
    print("auto_listen correctly persisted: OK")

    # Clean slate for next run.
    json_store.atomic_write_json(IDENTITY_PATH, dict(_DEFAULTS))
    print("Self-test cleanup complete.")
