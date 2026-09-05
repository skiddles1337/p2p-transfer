# P2P File Transfer

A learning project: a Python app that lets two people send files
directly to each other over the internet, with no server in between.

Built from scratch as a way to learn networking, cryptography, and
concurrent programming — see [docs/DESIGN.md](docs/DESIGN.md) for the
full current design, and [docs/HISTORY.md](docs/HISTORY.md) for the
story of how it got here (real bugs found, decisions made along the way).

## What it does

- Direct, encrypted, peer-to-peer file transfer (no middleman server)
- One connection handles a whole session — either side can send
  multiple files, in either direction, without reconnecting
- One listener can handle several different friends connected at once
- Chunked transfer with per-chunk integrity checking and live
  rate/ETA reporting
- Real authenticated encryption (X25519 key exchange + AES-256-GCM),
  with forward secrecy
- A simple pairing flow: share one connection string with a friend
  (built from a short spoken code + your info), they paste it in and
  you're connected — no manual IP/port typing beyond the initial setup
- Saved contacts, with staleness nudges, aliasing, and renaming
- Cancel an in-progress transfer from either side
- Incomplete transfers are kept (not deleted) in a visible, browsable
  "Partial Downloads" folder — groundwork for a future resume feature

Not yet built: an actual GUI (currently CLI-only), real resume, and a
few smaller gaps tracked in `DESIGN.md`'s "Known limitations" section.

## Requirements

- Python 3.10+
- Install dependencies: `pip install -r requirements.txt`
  (installs `cryptography` and `platformdirs`)

## Running it

```
cd src
python cli.py
```

This opens an interactive prompt. Type `help` to see available
commands. Basic flow to receive a file from a friend:

```
trust Sam <a-shared-passphrase>
listen 5001
```

...then have them `connect <your-ip> 5001 <same-passphrase>`, `send
<session_id> <path>` you a file, and you `accept <offer_id>` it when
prompted.

You'll also need to forward the port you listen on through your home
router for a friend to reach you from outside your network — `cli.py`
doesn't do this automatically (no reliable way to for arbitrary home
routers; see `DESIGN.md` §3 and §10 for why).

## Project layout

```
src/    - application code (see DESIGN.md for what each module does)
tests/  - diagnostic/test scripts, run directly with python
docs/   - DESIGN.md (current spec) and HISTORY.md (how we got here)
```

## Status

Core engine, encryption, pairing, and CLI are built and tested. GUI is
next. See `docs/DESIGN.md` for the full current state and `docs/
HISTORY.md`/commit history for how it got here.
