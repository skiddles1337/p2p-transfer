"""
logger.py

A tiny wrapper around printing status messages, used everywhere in
place of a bare print(). The point isn't functionality (right now it
just... prints) - it's a SEAM for the future GUI.

Today: log("some message") prints to the terminal, exactly like
print() would.

Later (GUI phase): the GUI can replace what log() actually DOES (e.g.
append text to a scrollable "Details" panel behind a button) without
needing to touch any of the calling code in listener.py/sender.py -
every place that currently calls log(...) keeps working unchanged,
it's only this one function's internals that would need to change.

This is a common pattern: centralize a cross-cutting concern (how
status messages get shown to the user) behind one small function,
even before you know exactly how it'll ultimately be displayed.
"""

import datetime


def log(message: str) -> None:
    """
    Record/display a status message. Currently just prints with a
    timestamp; the timestamp is included now since it's exactly the
    kind of detail a future "technical details" log view would want,
    and it costs nothing to add while we're already touching every
    call site.
    """
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}")
