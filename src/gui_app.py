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

    window = webview.create_window(
        "P2P Transfer (dev skeleton)",
        url=HTML_PATH,
        js_api=bridge,
        width=900,
        height=800,
    )
    window_holder["window"] = window

    # debug=True opens dev tools automatically (or makes them
    # available via right-click) - genuinely useful right now, since
    # any JS-side error (a typo, a bad selector) would otherwise fail
    # silently with no visible symptom other than "nothing happened."
    webview.start(debug=True)


if __name__ == "__main__":
    main()
