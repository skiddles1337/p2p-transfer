# P2P File Transfer

A learning project: a Python app with a real GUI that lets two people
send files directly to each other over the internet, with no server
in between.

Built from scratch as a way to learn networking, cryptography, and
concurrent programming — see [docs/DESIGN.md](docs/DESIGN.md) for the
full current design, and [docs/HISTORY.md](docs/HISTORY.md) for the
story of how it got here (real bugs found, decisions made along the way).

## What it does

- A real GUI (Connections, Contacts, History, Partial Downloads,
  Settings) plus a full-parity CLI for anyone who prefers it
- Direct, encrypted, peer-to-peer file transfer (no middleman server)
- One connection handles a whole session — either side can send
  multiple files, in either direction, without reconnecting
- One listener can handle several different friends connected at once
- Multiple files can be offered at once and decided independently, in
  any order — actual byte-transfer stays sequential underneath
- A live, per-chunk progress grid, with rate/ETA and average-speed
  stats once finished
- Real authenticated encryption (X25519 key exchange + AES-256-GCM),
  with forward secrecy
- A one-click pairing flow: share one connection string with a friend,
  they paste it in, and you're connected — no manual IP/port typing
- Saved contacts, with staleness nudges and cosmetic aliasing
- Cancel an in-progress transfer from either side
- Real drag-and-drop, plus a native file browser, for sending files
- Incomplete transfers are kept (not deleted) in a visible, browsable
  "Partial Downloads" section — groundwork for a future resume feature

Not yet built: actual resume (the groundwork above, not the
negotiation itself), a public-key identity model, and a few smaller
gaps tracked in `DESIGN.md`'s "Known limitations" section.

## Requirements

- Python 3.10+
- Install dependencies: `pip install -r requirements.txt`
  (installs `cryptography`, `platformdirs`, and `pywebview>=5.0`)

## Running it

**GUI** (the primary way to use this):
```
cd src
python gui_app.py
```

**CLI**, if you prefer a terminal — has full command parity with the
GUI's pairing/contacts flow:
```
cd src
python cli.py
```
Type `help` inside it for the full command list.

Either way, you'll need to forward the port you listen on through your
home router for a friend to reach you from outside your network — the
app can't do this automatically (no reliable way to for arbitrary home
routers; see `DESIGN.md` §3 and §10 for why). A "test connectivity"
button/command can help confirm your port forwarding actually works.

## Building a real Windows installer

See [packaging/BUILD.md](packaging/BUILD.md) for turning this into an
actual double-click-to-install Windows app (PyInstaller + Inno Setup)
— the original goal of this whole project. Genuinely untested as of
this writing; that doc is honest about what still needs verifying on
a real Windows machine.

## Project layout

```
src/        - application code (see DESIGN.md for what each module does)
src/gui/    - the GUI's HTML/CSS/JS frontend
tests/      - diagnostic/test scripts, run directly with python
packaging/  - Windows build/installer configuration
docs/       - DESIGN.md (current spec) and HISTORY.md (how we got here)
```

## Status

Core engine, encryption, pairing, GUI, and CLI are all built and
tested. Windows packaging is in progress — see `packaging/BUILD.md`.
See `docs/DESIGN.md` for the full current state and `docs/
HISTORY.md`/commit history for how it got here.
