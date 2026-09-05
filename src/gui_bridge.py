"""
gui_bridge.py

Thin coordination layer between the engine/pairing/contacts/history
modules and a pywebview frontend - analogous to cli.py, but built for
a GUI to call into rather than a human typing commands.

Deliberately decoupled from pywebview itself: the actual "push a
message to the frontend" mechanism is injected as a callable
(`push_to_frontend`), never hardcoded to `window.evaluate_js()`
directly. This is what lets the ENTIRE bridge - command dispatch,
event draining, JSON serialization - be tested headlessly (see
tests/test_gui_bridge.py), with no real window and no display, before
wiring in the real pywebview call at all. Same principle as
engine.py's presentation-agnostic design, just one layer further out.

COMMANDS: thin wrappers around Engine + pairing.py/contacts.py/
history.py/my_identity.py/network_info.py functions, translating
between JS-friendly arguments (plain strings/numbers/bools/dicts -
JS can't pass arbitrary Python objects) and what the underlying
functions actually expect.

EVENTS: a background thread drains engine.event_queue continuously,
JSON-encodes each event, and calls push_to_frontend(json_string).
"""

import json
import threading

from engine import Engine
import pairing
import contacts as contacts_module
import history as history_module
import my_identity
import network_info


class Bridge:
    def __init__(self, push_to_frontend):
        """
        push_to_frontend: a callable taking one string argument (a
        JSON-encoded event). In the real app, this wraps
        window.evaluate_js(...) to hand the event to JS; in tests,
        this can be anything - e.g. a function that just appends to a
        list for later inspection.
        """
        self.engine = Engine()
        self._push = push_to_frontend
        self._stop_flag = threading.Event()

        # Without this, a fresh Engine() has no memory of previously-
        # paired contacts - see pairing.load_contacts_into_engine's
        # docstring for the real bug this prevents.
        pairing.load_contacts_into_engine(self.engine)

        self._event_thread = threading.Thread(target=self._drain_events, daemon=True)
        self._event_thread.start()

    def _drain_events(self):
        while not self._stop_flag.is_set():
            try:
                event = self.engine.event_queue.get(timeout=0.5)
            except Exception:
                continue
            self._emit_to_frontend(event)

    def _emit_to_frontend(self, event: dict) -> None:
        """
        Push one event to the frontend. Wrapped in its own try/except
        so a frontend-side hiccup (window closed, a JS exception, the
        push callable itself misbehaving) can't crash this background
        thread and silently stop EVERY future event from ever reaching
        the UI again - same "one bad thing shouldn't take down
        everything else" principle used throughout engine.py.
        """
        try:
            payload = json.dumps(event)
            self._push(payload)
        except Exception as e:
            print(f"[gui_bridge] Failed to push event to frontend: {e}")

    def stop(self) -> None:
        self._stop_flag.set()

    # ---------- Engine commands ----------

    def start_listening(self, port):
        self.engine.start_listening(int(port))

    def stop_listening(self):
        self.engine.stop_listening()

    def get_state_snapshot(self):
        return self.engine.get_state_snapshot()

    def send_file(self, session_id, filepath):
        self.engine.send_file(int(session_id), filepath)

    def cancel_transfer(self, session_id, transfer_id_hex):
        self.engine.cancel_transfer(int(session_id), transfer_id_hex)

    def respond_to_offer(self, offer_id, accept):
        self.engine.respond_to_offer(int(offer_id), bool(accept))

    def close_session(self, session_id):
        self.engine.close_session(int(session_id))

    def has_active_transfers(self):
        return self.engine.has_active_transfers()

    # ---------- Pairing / contacts commands ----------
    # These ALWAYS go through pairing.py, never call
    # engine.set_known_passphrase() directly - see DESIGN.md's
    # invariant about why that matters.

    def quick_share(self, pairing_code):
        try:
            invite = pairing.quick_share(self.engine, pairing_code)
            return {"success": True, "invite": invite}
        except RuntimeError as e:
            # e.g. public IP couldn't be determined - see
            # pairing.quick_share's docstring.
            return {"success": False, "error": str(e)}

    def paste_and_connect(self, clipboard_text, pairing_code):
        session_id = pairing.paste_and_connect(self.engine, clipboard_text, pairing_code)
        if session_id is None:
            return {"success": False}
        return {"success": True, "session_id": session_id}

    def get_contacts(self):
        return contacts_module.load_contacts()

    def remove_contact(self, name):
        return pairing.forget_contact(self.engine, name)

    def rename_contact(self, old_name, new_name):
        return pairing.rename_contact(self.engine, old_name, new_name)

    def set_contact_alias(self, name, alias):
        return contacts_module.set_alias(name, alias)

    def get_contact_freshness(self, name):
        return pairing.get_contact_freshness(name)

    # ---------- History ----------

    def get_history(self):
        return history_module.load_history()

    def clear_history(self):
        history_module.clear_history()

    # ---------- Identity / network ----------

    def get_identity(self):
        return my_identity.get_identity()

    def set_identity(self, name=None, port=None):
        my_identity.set_identity(name=name, port=port)

    def get_public_ip(self):
        # Wrapped in a dict (rather than returning the bare string or
        # None) so JS can cleanly distinguish "got an IP" from "lookup
        # failed" without special-casing a bare null return value.
        ip = network_info.get_public_ip()
        return {"ip": ip}

    def open_port_check_tool(self):
        network_info.open_port_check_tool()
