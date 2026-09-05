"""
experiment_3_dom_drop_paths.py

Tests pywebview's dedicated DOM-drop-event mechanism for getting REAL
file paths from a drag-and-drop - genuinely different from what we
tried before (a JS-side file.path property, which does NOT work and
was correctly removed from the real app). This is a first-class
pywebview feature (added in pywebview 5.0): subscribe to the DOM drop
event from PYTHON, via window.dom.document.events.drop, and the event
data includes event['domTransfer']['files'][idx]['pywebviewFullPath'].

REQUIRES pywebview >= 5.0 - check your version first:
    pip show pywebview
If it's older, upgrade: pip install --upgrade pywebview

WHAT TO LOOK FOR:
A window opens with a drop zone. Drag a real file onto it. If this
works, the terminal should print the REAL absolute path of the dropped
file. If it doesn't work (older pywebview, or the API shape has
changed since that GitHub thread), you'll see an error in the terminal
or dev tools console instead - which tells us definitively whether
this is usable on your actual setup, the same way the other
experiments settled the background-thread and close-interception
questions.
"""

import webview

HTML = """
<html>
<body style="font-family: sans-serif; text-align: center; padding-top: 80px;">
  <div id="dropzone" style="border: 2px dashed #888; padding: 60px; margin: 40px;">
    <h2>Drop a file here</h2>
    <p>Watch the terminal for the real file path.</p>
  </div>
  <p id="status">Waiting...</p>

  <script>
    // Prevent the browser's default "navigate to the dropped file"
    // behavior - we're relying entirely on the Python-side DOM event
    // subscription below, not a JS-side drop handler, for the actual
    // path extraction.
    const zone = document.getElementById('dropzone');
    zone.addEventListener('dragover', (e) => e.preventDefault());
    zone.addEventListener('drop', (e) => {
      e.preventDefault();
      document.getElementById('status').innerText =
        'Drop detected in JS - check the TERMINAL for the real path (Python side).';
    });
  </script>
</body>
</html>
"""


def on_drop(e):
    """
    Registered below as the Python-side handler for the DOM drop
    event - this is the actual mechanism under test. Printing the
    whole event first, then trying to pull out the specific path
    field, so we can see the REAL shape of what pywebview gives us
    even if the exact key name has changed since the GitHub thread.
    """
    print("[Python] Raw drop event data:")
    print(e)

    try:
        files = e["dataTransfer"]["files"]
        for f in files:
            path = f.get("pywebviewFullPath")
            name = f.get("name")
            print(f"[Python] File: {name} -> REAL PATH: {path}")
    except Exception as ex:
        print(f"[Python] Could not extract path using the expected shape: {ex}")
        print("[Python] The raw event above shows the ACTUAL current shape - "
              "compare against what the code expected.")


def main():
    window = webview.create_window("Experiment 3: DOM Drop Paths", html=HTML)

    def setup_dom_events():
        # This is the actual API under test - subscribing to the DOM
        # drop event from PYTHON, not JS. If this attribute path
        # doesn't exist on your pywebview version, this will raise
        # immediately and clearly, telling us the version requirement
        # genuinely isn't met.
        try:
            window.dom.document.events.drop += on_drop
            print("[Python] Successfully subscribed to window.dom.document.events.drop")
        except AttributeError as e:
            print(f"[Python] FAILED to subscribe - your pywebview version likely "
                  f"predates this feature (needs >= 5.0): {e}")

    window.events.loaded += setup_dom_events

    webview.start(debug=True)


if __name__ == "__main__":
    main()
