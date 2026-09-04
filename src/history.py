"""
history.py

Persists a local record of past transfers (sent and received), so
there's something to show beyond whatever scrolled past in logs during
a session that has since ended. Standalone and engine-independent,
same pattern as contacts.py - the GUI (or a future automation-CLI
mode) is responsible for calling record_transfer() when a
file_complete event arrives; this module doesn't listen to the engine
directly, keeping it decoupled and independently testable.
"""

import os
import time
import platformdirs
import json_store

APP_NAME = "p2p-transfer"
HISTORY_PATH = os.path.join(platformdirs.user_data_dir(APP_NAME, appauthor=False), "history.json")

# Keep the history from growing without bound - a personal file
# transfer tool doesn't need to keep years of records, and an
# unbounded file would slowly get more expensive to load/save.
MAX_ENTRIES = 500


def load_history() -> list[dict]:
    """
    Return all recorded transfers, most recent first. Returns an empty
    list if no history file exists yet (first run), or if the file is
    somehow corrupted - see json_store.safe_load_json.
    """
    return json_store.safe_load_json(HISTORY_PATH, default=[])


def record_transfer(direction: str, peer_name: str, filename: str,
                     filesize: int, success: bool, detail: str) -> None:
    """
    Append one completed transfer to the history. Called by the GUI
    (or CLI) when a file_complete event arrives - direction is "sent"
    or "received", matching how the event was observed.

    New entries go at the FRONT of the list (most recent first), so
    a UI showing "recent activity" can just take the first N entries
    without needing to sort.
    """
    if direction not in ("sent", "received"):
        raise ValueError(f"direction must be 'sent' or 'received', got {direction!r}")

    entry = {
        "timestamp": time.time(),
        "direction": direction,
        "peer_name": peer_name,
        "filename": filename,
        "filesize": filesize,
        "success": success,
        "detail": detail,
    }

    entries = load_history()
    entries.insert(0, entry)
    entries = entries[:MAX_ENTRIES]  # trim oldest if over the cap

    json_store.atomic_write_json(HISTORY_PATH, entries)


def clear_history() -> None:
    """Erase all recorded history - e.g. for a 'clear history' button."""
    if os.path.exists(HISTORY_PATH):
        os.remove(HISTORY_PATH)


if __name__ == "__main__":
    print(f"History file location: {HISTORY_PATH}")

    clear_history()  # clean slate for this self-test

    record_transfer("sent", "Alex", "vacation.jpg", 2_500_000, True, "sent")
    record_transfer("received", "Sam", "notes.pdf", 500_000, True,
                     "/Downloads/P2P Transfer/notes.pdf")
    record_transfer("sent", "Alex", "big_file.zip", 900_000_000, False,
                     "3 chunk(s) failed: [12, 45, 100]")

    reloaded = load_history()
    print(f"\n{len(reloaded)} entries, most recent first:")
    for entry in reloaded:
        status = "OK" if entry["success"] else "FAILED"
        print(f"  [{status}] {entry['direction']} '{entry['filename']}' "
              f"({entry['filesize']} bytes) - {entry['peer_name']}")

    assert len(reloaded) == 3
    assert reloaded[0]["filename"] == "big_file.zip"  # most recent first
    print("\nOrder and content correct: OK")

    clear_history()
    print("Self-test cleanup complete.")
