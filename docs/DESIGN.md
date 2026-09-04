# P2P File Transfer — Design Doc

## Goal
A small app, run by two people (friends), that lets them send files to
each other directly over the internet, without a middleman server.

## Networking model
- Each instance of the app can act as **both** a listener (server) and a
  connector (client) — roles are per-action, not fixed.
- Because this runs over the public internet (not LAN), NAT means each
  person must manually forward a port on their router for incoming
  connections to reach them. The app cannot do this part automatically.
- Public IP is fetched via an external "what's my IP" service (e.g.
  ipify) and shown in the UI so it can be shared.

## Handshake / connection string
To make sharing connection info easy (e.g. pasting into Discord), the
"ready to receive" action generates a single string containing
everything needed to connect — but see Security below: the IP, port,
and name are **encrypted** inside this string, not plaintext, so a
short human-spoken code (e.g. "6666") doesn't mean an eavesdropper
gets your public IP and open port for free.

Rough shape: `----<base64 of encrypted blob>----`, where the encrypted
blob (once decrypted with the shared short code) contains the host's
name, public IP, and port.

The receiving friend pastes this whole string into their app to
"knock" / initiate a connection attempt.

## Security — layered design

We deliberately separate two different secrets with two different
strength requirements, rather than using one passphrase for
everything:

**1. The short pairing code** (e.g. a 4-digit number, said aloud over
Discord or texted). Its job is narrow and short-lived:
- Encrypts the connection string itself (IP/port/name), so that string
  isn't plaintext-readable if it ever leaks (e.g. sitting in Discord
  chat history).
- Authenticates the session handshake (proves "the peer I'm connecting
  to actually knows the code," and that neither public key was
  substituted in transit) via HMAC confirmation tags computed over the
  two exchanged X25519 public keys (implemented in `auth.py`) —
  **the code itself is never transmitted on the wire, in any form**,
  so there's nothing for an eavesdropper to directly capture and crack.
- Deliberately weak/guessable is an accepted tradeoff here, since the
  exposure window and blast radius are both small (see below) — this
  is what keeps the UX simple enough for "say a number out loud."

**2. The actual file-encryption key.** This must NOT be derived from
or transmitted using the short code, or we'd inherit its weakness for
something much more sensitive (file contents, possibly containing
PII). Instead:
- Established via a live key exchange during connection setup
  (Diffie-Hellman, e.g. X25519 via the `cryptography` library) — both
  sides independently *compute* the same shared secret through math,
  rather than one side generating it and transmitting it (even
  encrypted) to the other.
- The short code's challenge/response (above) authenticates this
  exchange, preventing an impostor from completing it in place of your
  real friend.
- This gives us **forward secrecy**: since the real key is never put
  on the wire in any form, someone who recorded your traffic and later
  brute-forces your short code gains nothing — there's no captured key
  material to retroactively decrypt.
- Each chunk is encrypted using this session key (`cryptography`'s
  `Fernet`, or an AEAD cipher directly) before sending.

**Why not just make the pairing code itself strong?** It would work
cryptographically, but breaks the desired UX — a code strong enough to
directly protect file contents isn't something you can casually say
aloud or remember. Splitting the two secrets lets the human-facing
code stay simple while the actual file protection is full-strength.

**Honest limitation:** this design assumes source code / protocol
details being public doesn't help an attacker (Kerckhoffs's
principle) — deliberately verified, not just assumed. It does NOT
defend against a compromised endpoint (e.g. malware on either
person's own machine reading files after decryption) — out of scope,
same as basically all end-to-end encrypted tools.

## Transfer model — persistent session connection
- **One connection per session** (revised from an earlier "one
  connection per file" plan) — this connection doubles as both the
  control channel (handshake, accept/reject, future cancel/resend) and
  the data channel (file chunks), since our generic message envelope
  makes interleaving both kinds of messages on one socket
  straightforward. Avoids needing a second socket/port.
- A session covers: connect → handshake (HELLO) → loop of file offers
  (each accepted/rejected, then transferred if accepted) → BYE to
  close.
- **Bidirectional sending is fully implemented (not just planned):**
  once a session is authenticated, EITHER side can call `send_file()`
  at any time — not just the side that initiated the connection.
  Verified experimentally: peer B connects to peer A, B sends A a
  file, then A sends B a file back on that same still-open connection,
  with no reconnection needed.
- **Multiple simultaneous sessions are fully implemented:** a single
  listener can handle several different peers connected at the same
  time, each independently. Still only ONE port is ever needed - each
  accepted connection gets its own socket and its own thread; the
  listening port itself is never "used up" by a connection (see
  Architecture below for how).
- Rejecting one file does NOT close the session — the loop continues,
  ready for the next offer. Only an explicit BYE (or connection
  failure) ends it. (In the GUI, this is the "big X to close the
  connection" button.)
- Failure isolation no longer comes from separate connections per
  file — it comes from per-file, per-chunk state (transfer_id +
  manifest, see Staging below) that survives independently of any
  particular connection's lifetime. A dropped connection mid-session
  can reconnect and resume based on this state (resume logic itself is
  still a future phase — the groundwork is in place now).

## Architecture: engine / presentation separation (implemented)
`engine.py` is the core of the application, deliberately built with NO
knowledge of how it's presented to a person - not CLI, not GUI,
nothing UI-specific at all. It exposes:
- **Commands** (plain method calls): `start_listening`,
  `stop_listening`, `connect_to_peer`, `send_file`,
  `respond_to_offer`, `close_session`
- **Events** (a thread-safe `queue.Queue`): `session_started`,
  `handshake_result`, `file_offer_received`, `file_offer_answered`,
  `chunk_progress`, `file_complete`, `session_closed`, `log`

Whatever eventually presents this (a terminal loop, a GUI, a web
frontend) just calls commands and reacts to events pulled off the
queue - it never touches a socket, a thread, or the wire protocol
directly. This separation means the actual UI can be built, or even
swapped out later, without ever touching the networking/crypto code
again.

**Threading model, per session:**
- Each session (one connection to one peer) gets exactly ONE dedicated
  "reader" thread, whose only job is to loop on `recv_message()` and
  react to whatever arrives.
- Sending happens from WHATEVER thread calls `send_file()` or responds
  to an offer - never from the reader thread. This split is what makes
  bidirectional sending possible: nothing is ever stuck only-listening.
- A per-session `send_lock` (a `threading.Lock`) guards every write to
  that session's socket, since two threads could otherwise interleave
  writes and corrupt the byte stream.
- The listener's accept loop spawns one new thread per accepted
  connection and immediately goes back to `accept()`-ing more - this
  is what makes multiple simultaneous sessions possible on one port.

**The offer hand-off (`PendingDecision`):** a small wait/signal
primitive (`threading.Event` + a place to store the answer). When a
`FILE_OFFER` arrives, the receiving session's reader thread creates
one, emits `file_offer_received`, and BLOCKS waiting on it -
*only that session's own thread blocks*, so multiple pending offers
from different peers can coexist without one blocking another. The
same primitive is used in reverse for outgoing offers: the thread
that called `send_file()` waits for the reader thread to receive and
record a `FILE_ACCEPT`/`FILE_REJECT`.

Verified via `tests/test_engine.py` (scripted, auto-accepting, not
requiring a human) for both bidirectional sending and multiple
simultaneous sessions.

## Wire protocol — message-based framing
Rather than hardcoding "filename, then size, then bytes" as fixed byte
offsets, every message on the wire has a generic envelope:

```
[ 1 byte  : message type ]
[ 8 bytes : payload length (unsigned, big-endian) ]
[ N bytes : payload ]
```

Message types (implemented, unless marked planned):
- `HELLO`          — listener's X25519 public key (32 bytes), sent
                     first on every connection
- `HELLO_RESPONSE` — sender's X25519 public key + confirmation tag
                     (HMAC-SHA256, keyed by the shared passphrase, over
                     both public keys in a specific order) — proves
                     passphrase knowledge AND binds the exchange to
                     these exact keys, closing a man-in-the-middle gap
                     a raw DH exchange alone wouldn't have
- `HELLO_OK`       — listener's own confirmation tag (reversed key
                     order, preventing a reflection attack), letting
                     the sender verify the listener too
- `HELLO_REJECT`   — handshake failed; connection closes right after
- `FILE_OFFER`     — transfer_id, filesize, filename
- `FILE_ACCEPT` / `FILE_REJECT` — receiver's per-file decision. The
                     engine surfaces the decision request as a
                     `file_offer_received` event and blocks that
                     session's thread on a `PendingDecision` until
                     `respond_to_offer()` is called - by a CLI prompt
                     today, a GUI popup later, with no engine changes
                     needed either way.
- `FILE_CHUNK`     — chunk index + SHA-256 hash of the PLAINTEXT chunk
                     + the chunk data, ENCRYPTED. Currently Fernet
                     (base64-wrapped AES, ~33% size overhead per
                     chunk); **planned near-term change** to raw
                     AES-GCM (same `cryptography` library, same HKDF-
                     derived key) to remove that overhead for large
                     files - the engine's chunk pack/unpack is
                     structured so this only touches the
                     encrypt/decrypt calls, not the surrounding logic.
- `DONE`           — sender signals no more chunks; whole-file hash
                     (of the plaintext) included, for a final
                     end-to-end integrity check
- `BYE`            — either side signals the session is closing
- (planned) `CANCEL`, `RESEND_REQUEST` — additive later, once resume
  is built; the generic envelope means these don't require a protocol
  redesign

All of the above are implemented and tested, including the full
authenticated key exchange (not just a placeholder challenge/response -
see Security section above, which reflects the actual implementation).

Using a generic envelope means adding new behavior later (cancel,
pause, resume) is "add a new message type" rather than a protocol
redesign.

## Per-chunk integrity
- Files are split into fixed-size chunks (e.g. 1 MB).
- Each chunk is hashed (SHA-256) *before* encryption, on the sender
  side. The hash travels alongside the chunk.
- Receiver decrypts, hashes the decrypted bytes, and compares to the
  hash it was sent. Mismatch = that chunk failed, can be flagged for
  retry without needing to redo the whole file (resume-friendly, even
  though full resume logic is a later phase).

## Staging, transfer IDs, and finalization (implemented)
- Each transfer gets a random 16-byte `transfer_id`, generated by the
  sender, included in `FILE_OFFER`. This is a stable identity for the
  transfer itself, independent of filename (filenames can collide or
  need renaming — see below — so they're unreliable as an identity key
  for future resume matching).
- Incoming files are written to a staging area
  (`received/.partial/<transfer_id>.part`) at their correct byte
  offset (`chunk_index * chunk_size`), not appended sequentially — this
  makes chunk order irrelevant to correctness and means a future
  resumed/resent chunk can simply be written to its correct spot.
- The destination file is **pre-allocated** to its full final size
  before any chunks arrive (seek to last byte, write one zero byte).
  This makes an out-of-disk-space condition fail immediately and
  clearly, rather than partway through a large transfer.
- A JSON manifest (`<transfer_id>.json`) sits alongside the staged
  file, recording filename, filesize, chunk size, and which chunk
  indices are verified good so far. Updated after every chunk, so the
  on-disk record survives a crash or dropped connection.
- The receiver keeps receiving even if a chunk fails its hash check
  (rather than aborting) — it writes what arrived, records the index
  as failed, and continues. This maximizes how much of a file lands
  correctly even under a flaky connection, and produces a precise
  "here's what's still needed" list.
- Finalization (moving the file from staging into `received/` under
  its real name) only happens when a transfer completes with **zero**
  failed chunks AND the whole-file hash matches. Otherwise, the staged
  file and manifest are left in place — ready for a future resume
  feature, not silently discarded.
- Collision-safe naming (`photo (1).jpg`, incrementing) is applied
  only at finalization time, matching common OS/browser download
  behavior — never on the in-progress staged file, since staged files
  are identified by transfer_id, not name.

## Failure handling
- Per-chunk hash mismatches are recorded, not fatal — see Staging
  above. The transfer completes with a clear list of which chunks
  need to be resent.
- **Not yet implemented:** the actual resend/reconnect handshake
  (sender and receiver negotiating "here's what's missing, send just
  that"). This requires the receiver to communicate back to the
  sender mid-transfer — a genuine two-way exchange, which the new
  persistent session model (see Transfer model above) is what makes
  this practical to add later without a redesign.

## Robustness: logging, error handling, and filename safety (implemented)
- All status output goes through a single `log()` function
  (`logger.py`) rather than scattered `print()` calls, AND through the
  engine's `event_queue` ("log" events) — a deliberate seam for the
  GUI phase: a future "Details" panel can consume these events without
  touching any calling code elsewhere.
- Each session's thread (`engine.py`'s `_run_session`) wraps its
  handshake and reader loop in broad try/except (`ConnectionError`,
  `OSError`, and a final catch-all). This is intentional and
  important: a single bad/dropped connection must never be able to
  crash the whole listener or any OTHER session's thread - especially
  once a GUI is relying on this running unattended in the background.
  Verified experimentally: an abrupt mid-handshake disconnect is
  caught and logged, and the listener keeps accepting new connections
  normally afterward.
- `connect_to_peer()` wraps its `connect()` call specifically, since
  "peer's app isn't running" / "port not forwarded" / "wrong IP" are
  common, expected failure modes that deserve a clear message rather
  than a raw traceback.
- **Filename sanitization** (`storage.py`'s `sanitize_filename`):
  beyond stripping path-traversal attempts (`os.path.basename`), also
  replaces characters Windows forbids entirely in filenames
  (`: * ? " < > |` and control characters) and strips trailing dots/
  spaces (also Windows-illegal). Prevents a peer sending an unusual or
  malformed filename from causing a confusing `OSError` when creating
  the file, rather than rejecting the transfer outright.
- **Platform lesson learned:** on Windows, a file cannot be renamed or
  moved while any process still holds it open (`WinError 32`) - unlike
  Linux/Mac, which generally allow this. `_receive_file()` explicitly
  closes the output file handle before calling `finalize_transfer()`
  (which renames the file into its final location), rather than
  relying on a `finally` block that would only run too late. Worth
  remembering for any future code that renames/moves files that were
  just written to.

## Contacts
- Saved locally in a JSON file next to the app.
- Each contact stores: name, ip, port, passphrase (since passphrases
  are treated as semi-permanent per relationship, not re-typed each
  time — user's call, may regenerate per session if preferred).

## GUI (later phase)
- Top: passphrase + port fields (port remembers last used)
- "Copy my info" button → builds connection string, copies to
  clipboard
- Paste box → parse connection string → save as named contact
- Contact list → click to connect
- "Start listening" button
- Accept/reject prompt per incoming file (filename, size, sender IP)
- Live stats: transfer rate, bytes done/total, ETA

## Build phases (updated to reflect actual progress)
1. ✅ Core transfer engine (CLI only): message-based framing, chunked
   send/receive, per-chunk hashing, whole-file verification via DONE
2. ✅ Staged storage: transfer IDs, pre-allocation, manifests,
   collision-safe finalization — groundwork for future resume
3. ✅ Persistent session connection + handshake: FILE_ACCEPT/REJECT
   loop, BYE, listener loops to accept multiple sessions over time
4. ✅ Real encryption: authenticated X25519 Diffie-Hellman key
   exchange (HMAC confirmation tags derived from a shared passphrase,
   closing the MITM gap), HKDF session key derivation, Fernet chunk
   encryption. Verified: matching shared secrets, tampering detection,
   wrong-key decryption failure, corrupted-ciphertext detection.
5. ✅ Robustness: centralized logging, broad error handling, filename
   sanitization
6. ✅ **Engine rework**: replaced the original `listener.py`/`sender.py`
   scripts (which assumed one fixed sender and one fixed receiver per
   run) with `engine.py` — a presentation-agnostic core supporting
   true bidirectional sending and multiple simultaneous sessions on
   one port. See Architecture section above. Verified via
   `tests/test_engine.py`.
7. **Next:** switch chunk encryption from Fernet to raw AES-GCM
   (removes ~33% base64 overhead per chunk - see Wire protocol above)
8. **Then:** GUI — planned as a local web UI (HTML/CSS/JS frontend,
   rendered via `pywebview` for a native-feeling window) rather than a
   native Python toolkit, specifically to support modern, animated
   visuals (e.g. a chunk-by-chunk progress grid) - browser engines
   GPU-accelerate this kind of rendering by default. The engine's
   event queue maps naturally onto this: push events to the frontend
   over a local websocket as they occur, rather than polling.
9. Contacts persistence, clipboard, encrypted connection-string
   generation/parsing, live transfer stats (rate, ETA)
10. (Future) Real resume: reconnect + manifest-based "here's what's
    missing" exchange, using groundwork already in place
11. (Future, exploratory) Browsing/requesting files from a peer's
    shared folder — bigger feature, needs its own permission model,
    deliberately deferred

Note: the shared passphrase (`auth.py`'s `SHARED_PASSPHRASE`) is still
hardcoded, standing in for the real per-session pairing-code flow.
Wiring that up is part of the GUI phase.

`listener.py` and `sender.py` (the original CLI scripts) are
superseded by `engine.py` and are being retired - `engine.py` covers
everything they did plus bidirectional sending and multi-session
support. `tests/simulate_corruption.py` still references the older
single-direction flow and will need a small update to use `engine.py`
if kept going forward.
