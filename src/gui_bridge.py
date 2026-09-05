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
import time

from engine import Engine
import pairing
import contacts as contacts_module
import history as history_module
import my_identity
import network_info
import storage


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

        # Set via set_window() once the real pywebview window exists -
        # needed for pick_files() below, which calls a method on the
        # window object itself (create_file_dialog isn't part of the
        # engine/pairing layer, it's pywebview-specific, so it lives
        # here rather than being exposed as a plain command elsewhere).
        self._window = None

        # name -> time.monotonic() of the last connect_to_contact()
        # attempt - see connect_to_contact()'s own docstring for why
        # this cooldown exists.
        self._last_connect_attempt = {}
        self.CONNECT_COOLDOWN_SECONDS = 10

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

    def set_window(self, window) -> None:
        """Called once from gui_app.py right after the real pywebview
        window is created - see __init__'s note on why this can't be
        passed in at construction time (the window doesn't exist yet
        when the Bridge is created)."""
        self._window = window

    def pick_files(self):
        """
        Opens the NATIVE OS file picker (not a browser <input
        type=file>, which can't return real filesystem paths) and
        returns the chosen path(s) as a list - empty if the person
        cancelled. This is the reliable way to get a real path to pass
        to send_file(), unlike drag-and-drop (see index.html's own
        notes on why that's uncertain across environments).

        Imports pywebview locally rather than at module level -
        pick_files() is the only thing in this whole module that
        actually needs it (everything else is pure engine/pairing
        logic), so the rest of the bridge stays importable and
        testable even in an environment without pywebview installed.
        """
        if self._window is None:
            return []
        import webview
        paths = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True)
        return list(paths) if paths else []

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

    def connect_to_contact(self, name):
        """
        Connect to an ALREADY-saved contact directly - deliberately
        separate from quick_share/paste_and_connect, which are both
        for FIRST-TIME pairing (needing a pairing code to decrypt a
        fresh connection string). Reconnecting to someone you've
        already paired with needs no code at all - their real
        passphrase is already saved from the original pairing.

        Two protections against creating redundant connections to the
        same contact - both needed, for different reasons:

        1. A short cooldown against rapid re-clicking. The underlying
           TCP connect() can sit pending for a long time if the target
           isn't actively refusing yet (common with NAT/router
           behavior) - it doesn't fail instantly the way a clean
           "nothing's listening" refusal would. Click "connect" several
           times quickly and each click starts its own pending attempt,
           with no session registered yet to check against - all of
           which can then succeed in a sudden burst once the peer
           finally starts listening. Reproduced and confirmed via
           real-world use, not just theorized.
        2. A check against sessions that are ALREADY fully connected -
           one session already handles any number of files in either
           direction, so there's no reason to open a second one to
           someone you're already talking to.
        """
        contact = contacts_module.get_contact(name)
        if contact is None:
            return {"success": False, "error": f"No saved contact named '{name}'"}

        now = time.monotonic()
        last_attempt = self._last_connect_attempt.get(name, 0)
        if now - last_attempt < self.CONNECT_COOLDOWN_SECONDS:
            return {"success": False, "error": "Already attempting to connect - please wait a moment",
                   "cooldown": True}
        self._last_connect_attempt[name] = now

        snapshot = self.engine.get_state_snapshot()
        for session_info in snapshot["sessions"]:
            if session_info["peer_name"] == name:
                return {"success": True, "session_id": session_info["session_id"],
                       "already_connected": True}

        try:
            session_id = self.engine.connect_to_peer(
                contact["ip"], contact["port"], contact["passphrase"], peer_name=name
            )
            return {"success": True, "session_id": session_id, "already_connected": False}
        except OSError as e:
            return {"success": False, "error": str(e)}

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

    def set_identity(self, name=None, port=None, ip_auto=None, manual_ip=None):
        my_identity.set_identity(name=name, port=port, ip_auto=ip_auto, manual_ip=manual_ip)

    def get_public_ip(self):
        # Wrapped in a dict (rather than returning the bare string or
        # None) so JS can cleanly distinguish "got an IP" from "lookup
        # failed" without special-casing a bare null return value.
        ip = network_info.get_public_ip()
        return {"ip": ip}

    def open_port_check_tool(self):
        network_info.open_port_check_tool()

    def get_save_dir(self):
        return storage.get_save_dir()

    def open_save_folder(self):
        storage.open_save_dir_in_explorer()

    def pick_save_folder(self):
        """
        Opens the native OS folder picker and, if something was
        chosen, sets it as the new save location immediately. Returns
        {"success": True, "path": ...} on success, or
        {"success": False, "error": ...} if the chosen folder isn't
        usable (e.g. no write permission) - {"success": False,
        "error": None} specifically means the person just cancelled,
        not a real error.
        """
        if self._window is None:
            return {"success": False, "error": "No window available"}

        import webview
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return {"success": False, "error": None}

        chosen = result[0]
        try:
            storage.set_save_dir(chosen)
            return {"success": True, "path": chosen}
        except OSError as e:
            return {"success": False, "error": str(e)}
