"""
gui_app.py

The actual (deliberately unstyled) pywebview application - Phase 3 of
the GUI build plan: prove the whole real workflow works through a real
window at least once, before any layout/styling work begins. Every
control in gui/index.html is plain, unstyled HTML - the only goal here
is proving gui_bridge.Bridge works correctly when driven by an actual
window and actual JS calls, not just our headless test harness
(tests/test_gui_bridge.py).
"""

import os
import base64
import webview
from gui_bridge import Bridge
import my_identity

HTML_PATH = os.path.join(os.path.dirname(__file__), "gui", "index.html")


def main():
    # The window object doesn't exist until AFTER webview.create_window()
    # returns, but the Bridge (and its background event-draining
    # thread) needs to exist BEFORE create_window(), since it's passed
    # in as js_api. This small holder lets push_to_frontend reference
    # whatever window eventually gets created, via a closure, without
    # needing the window to exist yet at Bridge-creation time.
    window_holder = {"window": None}

    def push_to_frontend(json_string: str) -> None:
        """
        The actual mechanism confirmed safe in
        tests/gui_experiments/experiment_1_background_thread.py:
        calling window.evaluate_js() from a background thread (this
        runs on Bridge's own event-draining thread, not the main
        thread pywebview's own loop runs on).

        The JSON is base64-encoded before being embedded in the JS
        call - this sidesteps any need to escape quotes/backslashes/
        newlines that might appear inside a filename or log message;
        the JS side simply base64-decodes it back before parsing.
        """
        window = window_holder["window"]
        if window is None:
            # A vanishingly small startup window before the real
            # window object exists - nothing meaningful could have
            # happened yet at this point, so dropping an event here
            # is fine for this skeleton phase.
            return

        encoded = base64.b64encode(json_string.encode("utf-8")).decode("ascii")
        window.evaluate_js(f"onEngineEvent('{encoded}')")

    bridge = Bridge(push_to_frontend)

    # Auto-listen-on-launch, if the person has turned this on in
    # Settings - doesn't need the window to exist at all, since
    # start_listening() is a pure engine call. Wrapped in try/except
    # since a saved port could genuinely be unavailable (already in
    # use by something else) - shouldn't prevent the app from opening
    # at all, just means listening doesn't auto-start this time.
    identity = my_identity.get_identity()
    if identity["auto_listen"]:
        try:
            bridge.start_listening(identity["port"])
        except OSError as e:
            print(f"[gui_app] Auto-listen failed (port {identity['port']}): {e}")

    window = webview.create_window(
        "P2P Transfer (dev skeleton)",
        url=HTML_PATH,
        js_api=bridge,
        width=900,
        height=800,
    )
    window_holder["window"] = window
    bridge.set_window(window)

    def on_dom_drop(e):
        """
        Real drag-and-drop, using pywebview's dedicated DOM-drop-event
        mechanism (confirmed working via
        tests/gui_experiments/experiment_3_dom_drop_paths.py on a real
        Windows/WebView2 setup) - NOT the JS-side file.path approach we
        tried first, which genuinely doesn't work and was removed.

        This subscription is GLOBAL (the whole document), not scoped
        to one element - so to know WHICH connection card a file was
        dropped onto, each card's dedicated drop-zone element carries
        a unique id ("drop-zone-<session_id>"), read back here via
        e['target']['id']. The drop-zone element deliberately has no
        interactive children (no buttons/inputs) so a drop landing
        anywhere inside it always reports itself as the target,
        never some nested child instead.
        """
        target_id = (e.get("target") or {}).get("id", "")
        if not target_id.startswith("drop-zone-"):
            return  # dropped somewhere else on the page - not ours to handle

        session_id_str = target_id[len("drop-zone-"):]
        try:
            session_id = int(session_id_str)
        except ValueError:
            return

        files = (e.get("dataTransfer") or {}).get("files", [])
        for f in files:
            path = f.get("pywebviewFullPath")
            if path:
                bridge.send_file(session_id, path)

    def setup_dom_events():
        try:
            window.dom.document.events.drop += on_dom_drop
        except AttributeError:
            # Older pywebview (pre-5.0) doesn't have this API at all -
            # fail quietly rather than crashing the whole app; the
            # "browse..." button remains fully functional regardless.
            pass

    window.events.loaded += setup_dom_events

    # debug=True opens dev tools automatically (or makes them
    # available via right-click) - genuinely useful right now, since
    # any JS-side error (a typo, a bad selector) would otherwise fail
    # silently with no visible symptom other than "nothing happened."
    webview.start(debug=True)


if __name__ == "__main__":
    main()
