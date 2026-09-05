"""
experiment_1_background_thread.py

Tests whether pywebview's window.evaluate_js() can be safely called
from a background thread - NOT the thread that called webview.start().
This matters because our engine's events arrive on arbitrary
background threads (each session has its own reader/sender threads),
and the planned design has a thread draining the event queue and
pushing updates into the page as they arrive - if evaluate_js() isn't
safe to call this way, that whole design needs to change to a
different hand-off mechanism instead.

WHAT TO LOOK FOR:
A window opens showing a counter that updates once per second, with
the text ALSO changing color each time. If this updates smoothly with
no crashes, no frozen window, and no errors printed in the terminal -
background-thread evaluate_js() calls are safe to build on.

If the window is unresponsive, the text never updates, or you see
errors in the terminal mentioning threading - that tells us we need a
different mechanism (e.g. a queue only the main thread drains).

Install first if needed: pip install pywebview
"""

import threading
import time
import webview

HTML = """
<html>
<body style="font-family: sans-serif; text-align: center; padding-top: 100px;">
  <h1 id="counter">Waiting for background thread...</h1>
  <p>If this number is counting up smoothly, background-thread
     evaluate_js() calls are working correctly.</p>
</body>
</html>
"""


def background_updater(window):
    """
    Runs on its OWN thread, separate from whatever thread pywebview's
    main event loop runs on - this is the exact pattern our real
    engine-event-draining thread would use.
    """
    count = 0
    colors = ["red", "blue", "green", "purple", "orange"]
    while True:
        time.sleep(1)
        count += 1
        color = colors[count % len(colors)]
        # This is the actual call under test: pushing a JS update from
        # a background thread, not the thread that started the window.
        js = f"document.getElementById('counter').innerText = 'Count: {count}'; " \
             f"document.getElementById('counter').style.color = '{color}';"
        try:
            window.evaluate_js(js)
            print(f"[background thread] Successfully pushed update #{count}")
        except Exception as e:
            print(f"[background thread] ERROR calling evaluate_js: {e}")


def main():
    window = webview.create_window("Experiment 1: Background Thread Test", html=HTML)

    # Start the background thread BEFORE webview.start() - since
    # webview.start() blocks the main thread until the window closes,
    # this mirrors how our real app would start the engine's
    # event-draining thread before entering the GUI's main loop.
    updater_thread = threading.Thread(target=background_updater, args=(window,), daemon=True)
    updater_thread.start()

    webview.start()


if __name__ == "__main__":
    main()
