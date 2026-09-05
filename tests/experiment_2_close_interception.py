"""
experiment_2_close_interception.py

Tests whether pywebview's window close event can be genuinely
INTERCEPTED and CANCELLED - needed for the planned "you have an active
transfer, are you sure you want to close?" warning. If pywebview only
NOTIFIES on close (rather than letting us block it), that whole
planned behavior needs a different approach (e.g. minimize instead of
a true cancel).

WHAT TO LOOK FOR:
A window opens with a button toggling a simulated "transfer in
progress" flag. Try closing the window (the X button) in each state:

  - With the flag ON ("transfer in progress"): the window should
    REFUSE to close - it should stay open, and the terminal should
    print a message saying the close was blocked.
  - With the flag OFF: the window should close normally.

If closing is genuinely blocked while the flag is on, and works
normally while it's off, closing-interception works as needed. If the
window closes regardless of the flag, pywebview only notifies and
doesn't let us prevent it - a real, structural finding we'd need to
design around differently.

Install first if needed: pip install pywebview
"""

import webview

HTML = """
<html>
<body style="font-family: sans-serif; text-align: center; padding-top: 80px;">
  <h1 id="status">Transfer in progress: OFF</h1>
  <button onclick="toggle()" style="font-size: 20px; padding: 10px 20px;">
    Toggle "transfer in progress"
  </button>
  <p>Try closing this window (the X button) in each state.<br>
     Watch the terminal for what happens.</p>

  <script>
    let transferActive = false;
    function toggle() {
      transferActive = !transferActive;
      document.getElementById('status').innerText =
        'Transfer in progress: ' + (transferActive ? 'ON' : 'OFF');
      window.pywebview.api.set_transfer_active(transferActive);
    }
  </script>
</body>
</html>
"""


class Api:
    """
    Exposed to JS as window.pywebview.api - this is the actual
    mechanism pywebview uses for JS-to-Python calls, tested here
    alongside the close-interception behavior since we'll need both
    for the real app anyway.
    """
    def __init__(self):
        self.transfer_active = False

    def set_transfer_active(self, value):
        self.transfer_active = value
        print(f"[Python] transfer_active is now: {value}")


def main():
    api = Api()
    window = webview.create_window(
        "Experiment 2: Close Interception Test", html=HTML, js_api=api
    )

    def on_closing():
        """
        Registered as the 'closing' event handler below. Per
        pywebview's documented behavior, returning False from this
        handler should CANCEL the close - that's exactly what we're
        testing is genuinely true in practice, not just documented.
        """
        if api.transfer_active:
            print("[Python] Close BLOCKED - transfer_active is True. "
                  "(In the real app, this is where we'd show a "
                  "confirmation dialog instead of just refusing.)")
            return False  # cancel the close
        else:
            print("[Python] Close ALLOWED - transfer_active is False.")
            return True  # allow the close (or just don't return False)

    window.events.closing += on_closing

    webview.start()
    print("Window closed - program ending.")


if __name__ == "__main__":
    main()
