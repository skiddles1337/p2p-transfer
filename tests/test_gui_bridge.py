"""
test_gui_bridge.py

Proves gui_bridge.Bridge works correctly - command dispatch, event
draining, JSON round-tripping - entirely headlessly. No pywebview, no
window, no display needed: the "push to frontend" mechanism is just a
function appending JSON strings to a list, standing in for what would
otherwise be window.evaluate_js() in the real app.

This is deliberately structured like tests/test_engine.py, just one
layer further out - proving the BRIDGE (not just the engine
underneath it) behaves correctly when driven the way a real frontend
would drive it: via its command methods, reacting to whatever JSON
events show up in its own recorded event list.
"""

import sys
import os
import time
import json
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import contacts
import history
import network_info
from gui_bridge import Bridge

TEST_PORT = 9100


def make_recording_bridge(label, event_log):
    """
    Creates a Bridge whose push_to_frontend just decodes the JSON and
    appends it to event_log, tagged with a label - standing in for
    "the frontend received this event," fully inspectable afterward
    without needing any real window.
    """
    def push(json_string):
        event = json.loads(json_string)
        event_log.append((label, event))
    return Bridge(push)


def wait_for_event(event_log, event_type, timeout=3):
    """Poll the shared event log until an event of the given type
    shows up, or give up after timeout seconds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for label, event in event_log:
            if event["type"] == event_type:
                return label, event
        time.sleep(0.05)
    return None, None


def main():
    contacts.save_contacts({})
    history.clear_history()

    # network_info.get_public_ip() needs real internet, which this
    # sandbox doesn't have - monkeypatched here the same way we did
    # when first testing pairing.quick_share() directly.
    network_info.get_public_ip = lambda: "203.0.113.50"

    # Set identity explicitly - without this, quick_share falls back
    # to a default name ("Me"), which is correct behavior but would
    # make this test's own assertions below about "Alex" wrong, not
    # the bridge.
    import my_identity
    my_identity.set_identity(name="Alex", port=5001)

    event_log = []

    print("=== Creating two bridges, simulating two separate GUI windows ===")
    alex_bridge = make_recording_bridge("ALEX", event_log)
    sam_bridge = make_recording_bridge("SAM", event_log)

    def auto_accept_offers():
        """Simulates a person clicking 'accept' the moment an offer
        event shows up - runs continuously in the background, checking
        both bridges' share of the event log."""
        seen = set()
        while True:
            for label, event in list(event_log):
                key = (label, event.get("session_id"), event.get("offer_id"))
                if event["type"] == "file_offer_received" and key not in seen:
                    seen.add(key)
                    bridge = alex_bridge if label == "ALEX" else sam_bridge
                    bridge.respond_to_offer(event["offer_id"], True)
            time.sleep(0.1)

    threading.Thread(target=auto_accept_offers, daemon=True).start()

    print("\n=== Alex: quick_share (one command, does everything) ===")
    result = alex_bridge.quick_share("6666")
    print(f"quick_share result: success={result['success']}")
    assert result["success"]
    invite = result["invite"]

    print(f"Alex listening_port via snapshot: "
          f"{alex_bridge.get_state_snapshot()['listening_port']}")
    assert alex_bridge.get_state_snapshot()["listening_port"] == 5001

    # quick_share used the real (mocked) public IP - swap it for
    # loopback so this test can actually connect, same trick as our
    # earlier direct pairing.py test.
    import connection_string
    parsed = connection_string.parse_connection_string(invite, "6666")
    loopback_invite = connection_string.generate_connection_string(
        parsed["name"], "127.0.0.1", parsed["port"], parsed["passphrase"], "6666"
    )

    print("\n=== Sam: paste_and_connect (parses AND connects immediately) ===")
    result = sam_bridge.paste_and_connect(loopback_invite, "6666")
    print(f"paste_and_connect result: {result}")
    assert result["success"]
    session_id = result["session_id"]

    label, event = wait_for_event(event_log, "handshake_result")
    print(f"Handshake result event (from {label}): {event}")
    assert event["success"]

    print("\n=== Sending a real file through the bridge ===")
    test_file = os.path.join(os.path.dirname(__file__), "..", "src", "test_file.txt")
    sam_bridge.send_file(session_id, test_file)

    label, event = wait_for_event(event_log, "file_complete", timeout=5)
    print(f"file_complete event (from {label}): {event}")
    assert event is not None
    assert event["success"]

    print("\n=== Verifying state snapshot works mid-session ===")
    snapshot = sam_bridge.get_state_snapshot()
    print(f"Sam's snapshot session count: {len(snapshot['sessions'])}")
    assert len(snapshot["sessions"]) == 1

    print("\n=== Verifying contacts/history commands work through the bridge ===")
    alex_contacts = alex_bridge.get_contacts()
    print(f"Alex's contacts: {list(alex_contacts.keys())}")
    assert "Alex" in alex_contacts  # quick_share registers yourself too

    print("\n=== Verifying has_active_transfers via the bridge ===")
    assert sam_bridge.has_active_transfers() is False  # transfer already finished

    print("\nAll bridge checks passed.")

    contacts.save_contacts({})
    history.clear_history()
    my_identity.set_identity(name="", port=5001)  # reset to defaults


if __name__ == "__main__":
    main()
