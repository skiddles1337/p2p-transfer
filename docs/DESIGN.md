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
- Each chunk is encrypted using this session key, via AES-256-GCM
  directly (`chunk_crypto.py`) — see Wire protocol below for why raw
  AES-GCM rather than a higher-level wrapper like Fernet.

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
same primitive is used for outgoing offers: the session's dedicated
SENDER thread (see below) waits for the reader thread to receive and
record a `FILE_ACCEPT`/`FILE_REJECT`, before sending any chunk data.

**Per-session send queue (bug found and fixed via CLI testing):**
Each session has exactly ONE dedicated sender thread, draining a
`send_queue` of files to send, one at a time, start to finish, before
moving to the next. `send_file()` just enqueues a filepath - it does
NOT spawn a new thread per call.

This wasn't the original design - the first version spawned a new
thread per `send_file()` call, each setting `session.pending_outgoing_
offer` independently. Rapid-fire testing via `cli.py` (typing `send`
several times before the first got a response - exactly the kind of
unpredictable human timing `test_engine.py`'s scripted delays never
exercised) exposed a real race: concurrent sender threads overwrote
each other's `pending_outgoing_offer`, so an incoming `FILE_ACCEPT`
could resolve the WRONG thread's wait, causing offers and chunk data
to interleave out of order and crash the session. The fix - a single
dedicated sender thread per session, processing a queue - eliminates
the race by construction (only one outgoing offer can ever be in
flight per session, not by convention, but because there's only one
thread that could start another) while *improving* the UX: multiple
quick `send_file()` calls now queue naturally instead of needing the
caller to wait for each to finish.

This is a good example of why testing with genuinely unpredictable
timing (a human clicking at their own pace) matters even after
thorough scripted/automated tests pass - some bugs only exist at
exactly the boundary of "two things happening close together but not
in the order the code implicitly assumed."

Verified via `tests/test_engine.py` (scripted, auto-accepting) for
bidirectional sending and multiple simultaneous sessions, and via
manual `cli.py` testing (including deliberately reproducing the race
above, both before and after the fix) for realistic human timing.

**Second bug found via adversarial testing: nested dispatch loops
can't handle interleaved bidirectional traffic.** An earlier version
of the reader loop called into a nested function (`_receive_file`)
with its own narrow inner loop recognizing only `FILE_CHUNK`/`DONE`.
Testing two peers sending files to each other SIMULTANEOUSLY on the
same connection (zero coordination delay) exposed the problem: while
one side's reader thread was inside that nested loop receiving a file,
the OTHER direction's `FILE_ACCEPT` (for that same side's own,
unrelated outgoing offer) arrived interleaved on the same connection -
and the nested loop, only expecting file-chunk-related messages,
treated it as "unexpected" and crashed the whole session.

**The fix:** flatten the reader loop into a single dispatcher that can
handle ANY message type at any point, tracking "am I currently
receiving a file" as state on the session object
(`session.active_incoming`, an `IncomingTransfer` instance) rather
than as which nested function call we happen to be inside. A
`FILE_CHUNK`/`DONE` updates whatever transfer is currently active;
a `FILE_ACCEPT`/`FILE_REJECT` resolves the pending outgoing offer;
either can arrive in any order relative to the other, since they're
genuinely independent logical conversations sharing one TCP stream.
Verified: 5 consecutive runs of simultaneous bidirectional sends, zero
crashes, both files arriving byte-correct on both sides.

This is worth remembering as a general lesson: once two-way,
asynchronous traffic is possible on one connection, a message
dispatcher must never assume "the next message will be one of these
few types" based on what it's currently doing - only based on the
actual state of the conversation, which can have multiple independent
threads of context active at once.

## Adversarial testing round (before GUI work)
Beyond the two bugs above, a deliberate round of testing specific
"break it on purpose" scenarios turned up smaller findings:

- **Closing a session with files still queued to send:** originally,
  `close_session()` just sent `BYE` immediately - any files still
  sitting in `send_queue` (not yet started) silently vanished with no
  event at all. Fixed: `close_session()` now drains the queue first
  and emits a `log` event for each cancelled file, so a future UI can
  show clearly what got cut off rather than files just disappearing.
  (A send already IN PROGRESS when close happens still isn't
  gracefully aborted - it typically surfaces as a connection error,
  same as closing any connection mid-transfer always would; considered
  acceptable for now.)
- **Double-responding to the same offer** (e.g. a UI double-click):
  tested both same-value (accept, accept) and conflicting
  (accept, reject) rapid double-calls. Both are safe - no crash, no
  duplicate transfer. Conflicting rapid calls deterministically let
  the second call win (last-write-wins), which is a reasonable
  semantic; a stale response to an already-resolved offer_id is
  correctly rejected with a clear log message rather than doing
  anything surprising.
- **Multiple peers connecting at the literal same instant** (not just
  staggered by a few hundred ms): tested three peers connecting and
  sending simultaneously to one listener. All three sessions and
  transfers completed correctly and independently - confirms the
  per-connection threading model holds up under genuine, not just
  staggered, concurrency.
- **A peer that connects and sends nothing** (hung, flaky, or
  malicious): originally, this tied up one thread FOREVER, blocked
  inside `recv_message()` waiting for a `HELLO_RESPONSE` that would
  never come - not a crash, and it didn't block OTHER sessions
  (each has its own thread), but an unbounded resource leak (connect
  many silent sockets, leak many threads). Fixed: a
  `HANDSHAKE_TIMEOUT_SECONDS` (15s) applies ONLY during the handshake
  phase - `socket.settimeout()` is set before the handshake and
  explicitly cleared (back to indefinite) immediately after, success
  or failure. This is deliberately NOT a general per-session timeout:
  an established session must be able to sit idle indefinitely
  (waiting for the next file offer, possibly minutes or hours later)
  without being killed - only the bounded, short handshake phase
  benefits from a deadline. (Fixing this surfaced a small secondary
  bug: the cleanup path could close the socket before a `finally`
  block tried to reset its timeout, raising `OSError: Bad file
  descriptor` - fixed by guarding that call.)

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
                     + the chunk data, ENCRYPTED with AES-256-GCM
                     directly (`chunk_crypto.py`), keyed by the HKDF-
                     derived session key. Ciphertext is
                     `[12-byte nonce][ciphertext+16-byte tag]` - only
                     28 bytes of fixed overhead per chunk, regardless
                     of chunk size (switched from Fernet, which
                     base64-encoded its output and inflated every
                     chunk by ~33%; verified via `chunk_crypto.py`'s
                     self-test: 28 bytes overhead on a 1MB chunk vs.
                     Fernet's ~349,624 bytes for the same size).
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

## Distribution & installation planning
The target audience is a normal Windows (priority) or Linux user with
no Python installed and no interest in a terminal - not a developer.
A few decisions made now, and a few deliberately deferred:

**Storage locations (implemented):** `storage.py` no longer uses
relative paths ("received/", "received/.partial/") - those depended
entirely on whatever folder the script happened to be launched from,
which breaks for a packaged, double-clicked app (it might run from
Program Files, often not writable by a normal user, or an
unpredictable temp extraction folder). Now uses `platformdirs` to
find the OS-appropriate Downloads location, with both finished and
in-progress transfers deliberately kept VISIBLE and browsable, rather
than tucking bookkeeping away in a hidden app-data folder:
- `SAVE_DIR` → `Downloads/P2P Transfer/` - finished files, same idea
  as where a browser puts its downloads.
- `STAGING_DIR` → `Downloads/P2P Transfer/Partial Downloads/` - a
  deliberate design choice, not just "where the temp files happen to
  live": in-progress/incomplete transfers are genuinely interesting to
  see, and this is exactly the state a future "resume" feature will
  read from. Staged files are named `<original filename>.<full
  transfer_id hex>.part` (+ matching `.json` manifest) rather than
  just the raw transfer_id, so someone browsing this folder can
  immediately tell what a given file IS ("vacation_photo.jpg.5b0673...
  .part") instead of seeing an opaque hash. The manifest is
  plain, readable JSON (`{"filename": ..., "verified_chunks": [0, 2]}`)
  - genuinely legible if someone opens it out of curiosity, and
  exactly what a resume feature needs to know "what's still missing."
This also means contacts/config (see below) should follow the same
pattern once implemented, rather than "a JSON file next to the app."

**Packaging (deferred until after GUI is stable, but decisions made
now):** target is Windows first, via PyInstaller + Inno Setup.
- **PyInstaller build mode: `--onedir`, not `--onefile`.** A
  single-file build is more convenient to send a friend, but
  self-extracting packed executables are meaningfully more likely to
  trigger antivirus/Windows Defender false-positive flags, purely from
  looking suspicious to heuristic scanners - a bad first impression
  for something a friend is about to run. `--onedir` (a folder,
  zipped for distribution) triggers this far less.
- **Two genuinely different "ports" - don't conflate them:** (1) the
  actual P2P listening port (5001), which needs router forwarding and
  triggers a normal Windows Firewall prompt; (2) a LOCAL-ONLY channel
  between the Python engine and the GUI frontend (websocket or
  `pywebview`'s JS bridge), which never leaves 127.0.0.1 and never
  triggers any firewall prompt at all, since loopback connections
  aren't visible to Windows' network security layer.
- **WebView2 dependency check:** most modern Windows installs already
  have WebView2 (built into Windows 11; delivered via Windows Update
  on Windows 10 since 2021) - but it's not guaranteed (older/locked-
  down/LTSC installs might lack it), and pywebview can silently fall
  back to a legacy IE-based renderer if it's missing, badly
  undermining the "modern, animated" GUI goal. Plan: the installer
  checks for WebView2 and silently runs Microsoft's small (~2MB)
  "Evergreen Bootstrapper" if it's absent, before first launch.
- **Pre-configure the firewall exception during install**, via a
  `netsh advfirewall` command in the Inno Setup installer script,
  rather than relying on the reactive runtime prompt - ideally
  eliminates that friction point entirely, since permission is
  requested once, upfront, as a normal part of installing the app.
- Linux: explicitly deprioritized per current plan - `pywebview` needs
  system-level GTK/WebKit (or Qt/WebEngine) libraries that PyInstaller
  can't fully bundle away, since they're tied to the OS's own shared
  libraries. Revisit if Linux support becomes a real priority later.
- Packaging a moving target wastes effort - deliberately building this
  only once the GUI itself is done, not alongside it.

**Expected friction, to document rather than "fix" (not really
fixable):**
- Windows SmartScreen warning on an unsigned .exe ("Windows protected
  your PC") - normal for a personal/indie app without a paid
  code-signing certificate; document the "More info → Run anyway"
  click-through.
- Port forwarding remains manual - no fully reliable way to automate
  this across arbitrary home routers. UPnP could offer a "try this
  first, fall back to manual" experience later for routers that
  support it, but manual instructions will always need to remain the
  real fallback (many routers disable UPnP by default).

## Contacts
- Will be saved via the same `platformdirs`-based approach as other
  storage (see Distribution & installation planning above), NOT "a
  JSON file next to the app" as originally sketched - that assumption
  predates the storage-location fix and has the same portability
  problem relative paths did.
- Each contact stores: name, ip, port, passphrase (since passphrases
  are treated as semi-permanent per relationship, not re-typed each
  time — user's call, may regenerate per session if preferred).

## Pre-GUI engine features (implemented)
Five pieces of groundwork built before frontend code exists, so the
GUI is built against a stable command/event shape rather than
retrofitting the wire protocol or engine internals afterward:

1. **Multi-passphrase, identity-revealing handshake.** `auth.py`'s
   confirmation tag functions take an explicit passphrase (no more
   single global `SHARED_PASSPHRASE`). `Engine.set_known_passphrase(
   name, passphrase)` / `remove_known_passphrase(name)` register what
   a LISTENER will accept; during handshake, `auth.
   find_matching_passphrase()` tries each known passphrase against the
   incoming tag - whichever matches both authenticates the connection
   AND identifies who it is (`session.peer_name`), surfaced in the
   `handshake_result` event. `connect_to_peer(ip, port, passphrase,
   peer_name=None)` takes the specific passphrase for whoever you're
   calling. Verified: two different peers, two different passphrases,
   one listener - both correctly identified.
2. **Cancel-in-progress transfers.** New `MSG_CANCEL` message
   (transfer_id payload) and `cancel_transfer(session_id,
   transfer_id_hex)` command. Works symmetrically: cancelling a
   transfer you're SENDING signals your own send loop to stop (via a
   per-transfer `threading.Event`) and tells the peer via `MSG_CANCEL`;
   cancelling one you're RECEIVING tells the peer to stop sending and
   aborts your own reception immediately. Partial data stays in
   staging either way - same "leave it resumable" philosophy as a
   chunk failure or dropped connection, just triggered intentionally.
   Verified from both directions with a large (300MB) file, cancelling
   after only ~4 of 300 chunks.
3. **Rate/ETA in progress events.** `chunk_progress` now includes
   `bytes_transferred`, `total_bytes`, `bytes_per_second`,
   `eta_seconds` (computed engine-side via a shared
   `_compute_rate_and_eta` helper, used by both the send and receive
   paths) - deliberately NOT left for the frontend to infer from
   websocket message timing, which would be noisy.
4. **`contacts.py`** - standalone, engine-independent persistence for
   saved contacts (name/ip/port/passphrase), using the same
   `platformdirs` approach as other storage (a config directory, not
   "a JSON file next to the app").
5. **`connection_string.py`** - the actual encode/decode logic for the
   shareable pairing string, implementing the layered security design
   from earlier: a short, human-typed PAIRING CODE (PBKDF2-derived key,
   200,000 iterations) encrypts a blob containing name/ip/port/PLUS an
   auto-generated session PASSPHRASE - the latter being what actually
   gets used for handshake confirmation tags (item 1 above), never the
   weak pairing code directly. Reuses `chunk_crypto.ChunkCipher`
   rather than inventing a second encryption mechanism. Any failure
   (wrong code, corrupted string, garbage input) collapses to the same
   generic `None` - deliberately not distinguishing why, so a failed
   attempt doesn't hand an attacker a calibration signal. Verified:
   round-trip, wrong code, garbage input, and tampered ciphertext all
   behave correctly.

## GUI planning (pre-implementation - engine/support modules done, frontend not started)

**What already exists to build on** (all engine-side, tested, presentation-agnostic):
- `pairing.py`: `create_invite()` / `accept_invite()` / `load_contacts_into_engine()`
  - the ONLY correct way to touch `engine.known_passphrases` - never call
  `engine.set_known_passphrase()` directly from GUI code, or a successful
  handshake could correspond to no saved contact, a state the UI below
  isn't designed to handle.
- `network_info.py`: `get_public_ip()` for the "copy my info" flow -
  returns `None` on failure (no internet, all lookup services down);
  GUI must handle that by letting the person type their IP manually,
  not by crashing or leaving the field blank with no explanation.
  Also `open_port_check_tool()` - a small "test my connectivity"
  button next to the port field, opens canyouseeme.org in the
  person's browser. Deliberately manual (person types their own port
  in) rather than automated/scraped - reputable port-check sites don't
  offer stable, documented APIs, so pre-filling or parsing a result
  programmatically would be fragile and could silently break.
- `contacts.py`: saved contact list, persisted via `platformdirs`.
- Full command/event set on `Engine` (see Architecture section above),
  including `cancel_transfer` and rate/ETA-bearing `chunk_progress`.

**Unverified incoming connection - UI state (decided, not yet built):**
`session_started` fires immediately on accept, BEFORE the handshake
resolves - `handshake_result` (with `peer_name`) comes later and can
take a moment. This means there's a real window where a connection
exists but isn't yet identified - the UI needs an explicit state for
it, not silence:
- On `session_started`: show `Unknown (<ip>) - verifying...`
  immediately.
- On `handshake_result` success: update that same row in place to the
  real name.
- On `handshake_result` failure: show `Unknown (<ip>) - rejected`
  BRIEFLY, then auto-dismiss (decided: failed/unauthorized attempts
  ARE shown, briefly, rather than either staying visible indefinitely
  or being hidden entirely - a middle ground between "no visibility
  into who's probing your open port" and "alarming permanent clutter
  from routine internet background noise"). Worth considering later:
  collapsing repeated rapid failures from the same IP into one row
  rather than spamming the list.

**Decided: no one-off/"guest" connections.** Every connection must go
through real pairing (`pairing.py`) first - no temporary/anonymous
trust option. This reinforces rather than complicates the invariant
above: a successful handshake always corresponds to a saved contact,
with no exception to design around.

**Connection failure messaging - the engine already distinguishes
enough to show specific, helpful messages, not a generic "failed":**

| Event | Likely cause | Suggested message |
|---|---|---|
| `session_closed` with `reason: "Could not connect: ..."` | Stale IP - contact's home IP changed since you saved them | "Couldn't reach `<name>` — have they changed networks? Ask for an updated invite." |
| `handshake_result` with `reason` mentioning timeout | Reached them, but their app didn't respond in time | "Connected, but `<name>`'s app didn't respond — is it running?" |
| `handshake_result` with `reason: "Authentication failed"` | Reached them, but passphrase mismatch - given the no-guest-connections rule, should be rare (corrupted/stale saved contact) | "Security check failed — this contact may need to be re-paired." |

No engine changes needed for this - purely a GUI-layer mapping from
existing event reasons to specific copy, instead of one generic
"connection failed" message for every case.

**System notifications - decided: browser Notification API, not a
Python library.** Fits the existing `pywebview` + web frontend
architecture with zero new dependencies - the frontend reacts to a
`file_offer_received` event and shows the notification itself,
consistent with "engine emits, frontend reacts." WebView2 (Windows'
`pywebview` backend) supports this, including real Windows toast
notifications. Honest tradeoff: requires a one-time browser-style
permission prompt, which can feel slightly unusual in a desktop app
context - acceptable given the dependency savings and architectural
fit; revisit if it feels wrong once actually built (a presentation-
layer-only change, doesn't touch the engine).

**Drag-and-drop / multi-file sending - decided: N files dropped = N
separate sequential offers**, not one offer containing multiple files.
Reuses `send_file()`'s existing queueing behavior exactly as-is - no
protocol change needed. Drag-and-drop itself is a frontend concern
(native browser APIs), not an engine concern.

**Transfer history (implemented):** `history.py` - standalone,
`contacts.py`-style persistence (`platformdirs`, JSON) for a local
record of past transfers (direction, peer, filename, size, success,
detail, timestamp). Capped at 500 entries (oldest trimmed) - a
personal tool doesn't need unbounded history, and an ever-growing file
would slowly cost more to load/save. The GUI (or a future automation-
CLI) is responsible for calling `record_transfer()` when a
`file_complete` event arrives - this module doesn't listen to the
engine directly, keeping it decoupled and independently testable, same
as `contacts.py`.

**Close-app-mid-transfer warning - decided: yes, warn first.**
`Engine.has_active_transfers()` (implemented) - True if ANY session
has a transfer in progress (checks `active_incoming`/`active_outgoing`
across all sessions), for the GUI to check before allowing the app to
close. Worth being clear about WHY this is a warning and not a hard
block: closing mid-transfer isn't actually destructive - staging
preserves partial data, resumable later - so this is about giving the
person a heads-up and a chance to reconsider, not preventing data loss
that wouldn't otherwise happen. Verified: correctly False before/after
a transfer, True while one is genuinely in progress, on both sides of
a real connection.

**Field "what's actually being used" confirmation - Name/IP/Port
treated differently, since each has a different relationship to live
state:**
- **Port:** genuinely tricky - editing the text field does NOT
  retroactively change what's actually listening; only calling
  `start_listening()` again does. Fixed at the engine level:
  `Engine.listening_port` (implemented) is the source of truth the
  GUI should display via a separate status badge (e.g. "● Listening
  on 5001"), NOT by echoing the live text field - so an edited-but-
  not-yet-applied port is visibly distinct from what's actually bound.
  Returns `None` when not listening. Verified: correctly stays at the
  old port while the field is edited without restarting, and clears
  to `None` immediately (not after a delay) on `stop_listening()` -
  an actual timing bug was caught and fixed here, since the property
  was originally only cleared by the background accept thread noticing
  on its own ~1-second timeout cycle.
- **IP:** a stale value doesn't break YOUR listening (binding is
  always all-interfaces) - it silently breaks things for whoever you
  hand a connection string to, a worse failure mode since you're not
  the one who feels the symptom. Plan: tag the field as "Auto-detected
  just now" vs. "Manually entered," with a refresh icon to re-run
  `network_info.get_public_ip()` on demand.
- **Name:** no live-state concern (not tied to anything currently
  running) - just needs the standard auto-save-with-brief-confirmation
  pattern (save on blur/pause, a momentary checkmark/flash), same as
  any other persistent preference field.

## GUI next steps (not yet designed)
Layout, the concrete event→frontend wiring mechanism (websocket vs.
pywebview's JS bridge), and the connection-string UI flow itself still
need to be designed before any frontend code is written.

**Still-undecided GUI-shape questions** (layout, wiring mechanism,
connection-string UI flow) - see "Next steps" further down; not yet
designed in detail.

## Old sketch (superseded by the planning above, kept for history)
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
   closing the MITM gap), HKDF session key derivation, chunk
   encryption. Verified: matching shared secrets, tampering detection,
   wrong-key decryption failure, corrupted-ciphertext detection.
5. ✅ Robustness: centralized logging, broad error handling, filename
   sanitization
6. ✅ **Engine rework**: replaced the original `listener.py`/`sender.py`
   scripts (which assumed one fixed sender and one fixed receiver per
   run) with `engine.py` — a presentation-agnostic core supporting
   true bidirectional sending and multiple simultaneous sessions on
   one port. See Architecture section above. Verified via
   `tests/test_engine.py`, and via real human-timing testing with
   `cli.py` (which caught and led to fixing the send-queue race
   described above).
7. ✅ **`cli.py`**: a permanent, lightweight interactive CLI on top of
   `engine.py` - not a throwaway test harness. Intended to remain a
   real, first-class way to use the app going forward (useful on
   low-spec machines, and a natural base for a future automation-
   oriented CLI mode), not just a stepping stone to the GUI.
8. ✅ Switched chunk encryption from Fernet to raw AES-256-GCM
   (`chunk_crypto.py`) - removes ~33% base64 overhead per chunk,
   replaced with a fixed 28 bytes (12-byte nonce + 16-byte auth tag)
   regardless of chunk size. Same security properties (authenticated
   encryption, tampering still detected via `InvalidTag`). Verified:
   round-trip correctness, tampering detection, and a real 3MB
   multi-chunk transfer end-to-end after the swap.
9. **Then:** GUI — planned as a local web UI (HTML/CSS/JS frontend,
   rendered via `pywebview` for a native-feeling window) rather than a
   native Python toolkit, specifically to support modern, animated
   visuals (e.g. a chunk-by-chunk progress grid) - browser engines
   GPU-accelerate this kind of rendering by default. The engine's
   event queue maps naturally onto this: push events to the frontend
   over a local websocket as they occur, rather than polling.
10. Contacts persistence, clipboard, encrypted connection-string
    generation/parsing, live transfer stats (rate, ETA)
11. (Future) Real resume: reconnect + manifest-based "here's what's
    missing" exchange, using groundwork already in place
12. (Future) A more automation-oriented CLI mode (JSON event output,
    one-shot subcommands with proper exit codes, non-interactive
    accept policies) - deliberately deferred until a real automation
    use case exists, rather than speculatively designed now. `cli.py`
    already proves the engine's command/event interface supports this
    without changes to `engine.py` itself.
13. (Future, exploratory) Browsing/requesting files from a peer's
    shared folder — bigger feature, needs its own permission model,
    deliberately deferred

Note: the shared passphrase (`auth.py`'s `SHARED_PASSPHRASE`) is still
hardcoded, standing in for the real per-session pairing-code flow.
Wiring that up is part of the GUI phase.

`listener.py` and `sender.py` (the original single-direction CLI
scripts) have been removed - fully superseded by `engine.py` +
`cli.py`. `tests/simulate_corruption.py` still references the older
pre-engine flow; either update it to use `engine.py` or retire it in
favor of `tests/test_engine.py`, which already covers similar ground
(including corruption/failure paths) against the current architecture.
