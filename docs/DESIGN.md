# P2P File Transfer — Design Doc

This document describes the app as it currently is. For the story of
how it got this way — bugs found, decisions weighed, dead ends —
see `docs/HISTORY.md`. Nothing here should require reading that file
to understand; this doc should always be readable top-to-bottom as an
accurate, current spec.

## 1. Overview

A small app, run by two people (friends), that lets them send files to
each other directly over the internet, without a middleman server.
Target audience: a normal Windows (priority) or Linux user with no
Python installed and no interest in a terminal — not a developer.

## 2. Functional capabilities (current state)

**Implemented and tested:**
- Direct P2P file transfer over the internet (with manual port
  forwarding — see §3)
- Persistent session connections: connect once, send/receive multiple
  files without reconnecting
- **Bidirectional sending**: either side of an established connection
  can send files, not just whoever initiated it
- **Multiple simultaneous sessions**: one listener can handle several
  different peers connected at once, on a single port
- Chunked transfer with per-chunk SHA-256 integrity checking, plus a
  final whole-file hash check
- Full authenticated encryption (X25519 key exchange + AES-256-GCM
  chunk encryption) — see §4
- Multi-contact identity: a listener recognizes different contacts by
  which passphrase they present, no manual "who is this" step
- Cancel an in-progress transfer, from either the sending or
  receiving side
- Live transfer rate/ETA reporting
- Resumable-transfer groundwork: incomplete transfers are preserved
  (not deleted) with enough state to support a future resume feature
- Contact management: add via connection string, remove, alias
  (cosmetic nickname), true rename (changes the actual identity key)
- Contact staleness tracking: a "hasn't been re-paired in N days"
  nudge (not a hard expiration)
- One-click sharing workflow (`quick_share`) and one-click connecting
  workflow (`paste_and_connect`)
- Transfer history log
- Configurable save location
- Crash-safe local data storage (contacts, history, settings survive
  a mid-write crash without corrupting)
- A real, permanent CLI (`cli.py`) — not just a dev tool

**Not yet implemented:**
- GUI (planned — see §11)
- Actual resume (reconnect + "here's what's missing" exchange) — the
  groundwork exists, the negotiation itself doesn't yet
- Public-key identity model (see §4's honest limitation)
- Any automated/scripted CLI mode (JSON output, exit codes) — deferred
  until a real automation need exists
- Peer folder browsing/requesting — deliberately deferred, needs its
  own permission model

## 3. Networking model

- Each instance of the app can act as **both** a listener and a
  connector — roles are per-action, not fixed for the app's lifetime.
- Runs over the public internet, not LAN — NAT means each person must
  manually forward a port on their router for incoming connections to
  reach them. The app cannot do this automatically (see §10 for why
  UPnP isn't a full solution).
- Public IP is fetched live via an external lookup service
  (`network_info.get_public_ip()`) when needed — never cached, since a
  stale IP silently breaks for whoever receives an invite built from it.
- A small "test my connectivity" helper (`network_info.
  open_port_check_tool()`) opens a reputable third-party port-checking
  site in the browser — deliberately manual (not scraped/automated),
  since these sites don't offer stable APIs to build against safely.

## 4. Security design

Two different secrets, two different strength requirements,
deliberately kept separate:

**1. The short pairing code** (e.g. a 4-digit number, said aloud over
Discord). Narrow, short-lived job:
- Encrypts the connection string itself (name/IP/port/passphrase), so
  it isn't plaintext-readable if it leaks (e.g. sitting in Discord
  chat history). Uses PBKDF2 (200,000 iterations) to derive a key from
  it, then AES-256-GCM (via `chunk_crypto.ChunkCipher`, reused rather
  than inventing a second encryption mechanism) to encrypt the payload.
- Deliberately weak/guessable is an accepted tradeoff — the exposure
  window and blast radius are both small. This is what keeps the UX
  simple enough for "say a number out loud."

**2. The actual session passphrase** (long, random, auto-generated —
never typed or spoken by a human). This is what the connection string
actually carries and what authenticates each handshake:
- Established per-contact (`engine.known_passphrases`), not a single
  global value — a listener doesn't know in advance who's connecting,
  so it tries each known passphrase against the incoming confirmation
  tag; whichever matches both authenticates the connection AND
  identifies who it is, before they've said anything else.
- Authenticates an X25519 Diffie-Hellman key exchange via HMAC
  confirmation tags computed over the two exchanged public keys, in a
  specific order each direction (preventing a reflection attack) — this
  is what closes the man-in-the-middle gap a raw DH exchange alone
  would have. The passphrase itself is never transmitted on the wire,
  in any form.
- The actual file-encryption key is never derived from or transmitted
  using either secret above — both sides independently *compute* the
  same shared secret through the DH exchange, then derive a session key
  via HKDF. This gives **forward secrecy**: since the real key is never
  put on the wire in any form, someone who recorded traffic and later
  compromises a passphrase gains nothing retroactively.
- Each chunk is encrypted with this session key via AES-256-GCM
  directly (`chunk_crypto.py`) — not a higher-level wrapper like
  Fernet, which would inflate every chunk by ~33% via base64 encoding.
  Raw AES-GCM adds a small fixed overhead (28 bytes: 12-byte nonce +
  16-byte auth tag) regardless of chunk size.

**Contact staleness** (`contacts.py` + `pairing.get_contact_freshness`):
every contact stores a `paired_at` timestamp, refreshed whenever
`add_contact()` runs (including re-pairing). A contact not re-paired
within a configurable window (default 30 days) is flagged "stale" — a
UI nudge, not a lockout; the passphrase still works exactly as before.
A contact with no timestamp at all (predates this feature) is treated
as stale by default.

**Naming: alias vs. rename.** The name attached to a contact is the
*inviter's own self-asserted* display name — the receiving side had no
say in it originally. Two ways to change it, different risk profiles:
- **Alias** (`contacts.set_alias` / `contacts.display_name`) — purely
  cosmetic, never touches `engine.known_passphrases`. Survives
  re-pairing.
- **True rename** (`pairing.rename_contact`) — actually changes the
  identity key everywhere, keeping `contacts.json` and the running
  engine's `known_passphrases` in sync atomically. Refuses to overwrite
  an existing different contact.

Both are always local-only, never communicated back to the other person.

**Honest limitation:** the passphrase is a static, shared bearer
secret with no hard expiration — if a contact's saved data ever leaks,
whoever has it can impersonate that contact until manually revoked
(`pairing.forget_contact`). A full public-key identity model (each
device holds one long-term keypair; pairing exchanges public keys, not
a shared secret) would eliminate this class of risk entirely, and is
the more architecturally correct long-term design — deliberately not
built yet, given the added complexity, for a currently friends-only tool.

Also out of scope, same as basically all end-to-end encrypted tools:
defending against a compromised endpoint (e.g. malware reading files
after decryption). This design assumes protocol/source-code details
being public doesn't help an attacker (Kerckhoffs's principle) —
deliberately verified via testing, not just assumed.

## 5. Transfer model — persistent session connection

- **One connection per session**, not one connection per file — this
  single connection doubles as both control channel (handshake,
  accept/reject, cancel) and data channel (file chunks), since the
  generic message envelope makes interleaving both kinds of messages
  on one socket straightforward.
- A session: connect → authenticated handshake → loop of file offers
  (each accepted/rejected, transferred if accepted, either direction) →
  `BYE` to close.
- Rejecting one file does not close the session. Only an explicit
  `BYE`, or a connection failure, ends it.
- Failure isolation comes from per-file, per-chunk state (transfer_id +
  manifest — see §7), not from separate connections — this state
  survives independently of any particular connection's lifetime,
  which is what will make real resume possible later.

## 6. Architecture: engine / presentation separation

`engine.py` is the core of the application, built with no knowledge of
how it's presented (CLI, GUI, anything). It exposes:

**Commands** (plain method calls): `start_listening`, `stop_listening`,
`connect_to_peer(ip, port, passphrase, peer_name=None)`, `send_file`,
`cancel_transfer(session_id, transfer_id_hex)`, `respond_to_offer`,
`close_session`, `get_state_snapshot()`, `has_active_transfers()`,
`set_known_passphrase` / `remove_known_passphrase` (only ever called
indirectly, via `pairing.py` — see §9).

**Events** (a thread-safe `queue.Queue`): `session_started`,
`handshake_result` (includes `peer_name`), `file_offer_received`,
`file_offer_answered`, `chunk_progress` (includes `bytes_transferred`,
`total_bytes`, `bytes_per_second`, `eta_seconds`), `file_complete`,
`session_closed`, `log`.

Whatever presents this — a terminal loop, a GUI, a web frontend — just
calls commands and reacts to events; it never touches a socket, a
thread, or the wire protocol directly.

**Threading model, per session:**
- Each session gets exactly one dedicated **reader** thread, whose only
  job is to loop on `recv_message()` and dispatch based on message
  type. The dispatcher is **flat** — any message type can be handled at
  any point, with state (e.g. "am I currently receiving a file")
  tracked on the session object (`session.active_incoming`, an
  `IncomingTransfer` instance) rather than via nested function calls
  with narrow expectations. This is what allows two independent logical
  conversations (my own outgoing offer's response; an unrelated
  incoming file's chunks) to share one connection safely.
- Each session also gets exactly one dedicated **sender** thread,
  draining a `send_queue` of files to send, one at a time, start to
  finish. `send_file()` just enqueues — it never spawns a thread per
  call. This is what guarantees only one outgoing offer is ever in
  flight per session, and lets multiple quick `send_file()` calls queue
  naturally rather than racing.
- A per-session `send_lock` guards every write to that session's
  socket, since the reader thread's responses and the sender thread's
  writes could otherwise interleave and corrupt the byte stream.
- The listener's accept loop spawns one new thread per accepted
  connection and immediately returns to `accept()`-ing — this is what
  makes multiple simultaneous sessions possible on a single port (one
  port is always enough; `accept()` hands back a new socket per
  connection without consuming the listening socket).
- A `HANDSHAKE_TIMEOUT_SECONDS` (15s) applies **only** during the
  handshake phase (`socket.settimeout()`, cleared immediately after,
  success or failure) — an established session can sit idle
  indefinitely waiting for the next file offer, but a peer that
  connects and sends nothing during the handshake itself is bounded,
  preventing an unbounded thread leak.

**The offer hand-off (`PendingDecision`):** a `threading.Event` + a
place to store the answer. An incoming `FILE_OFFER` creates one, emits
`file_offer_received`, and blocks *only that session's own thread* —
multiple pending offers from different peers coexist independently.
The same primitive is used for outgoing offers, in reverse (the
sender thread waits for the reader thread to record a response).

**State snapshot (`get_state_snapshot()`):** returns a full,
JSON-serializable description of everything the engine currently
knows — listening status, every active session (identity, handshake
state, live transfer progress for both `active_incoming` and
`active_outgoing`), and every pending incoming offer (including its
filename/filesize, not just its existence). Exists for a frontend that
needs to rebuild its display from a blank slate (a page reload, a
reconnecting websocket) without having seen every event that led to
the current state — events remain the mechanism for incremental
updates; this is the mechanism for "tell me everything, right now."
Thread-safe (`Engine._state_lock` guards the sessions and
pending-offers dicts against concurrent mutation during iteration).
Both `active_incoming`/`active_outgoing` in the snapshot and the live
`chunk_progress` event use identical field names, via a shared
`_transfer_progress_dict()` helper, so a frontend can render a
transfer row the same way regardless of which one it came from.
`known_contact_names` in the snapshot exposes only names, never
passphrase values.

**Robustness:** every session thread wraps its handshake and reader
loop in broad exception handling — a single bad/dropped connection, or
a local file-access error while sending (bad path, or a file that
becomes unreadable mid-transfer), cannot crash the whole listener or
kill a session's ability to send/receive anything else for the rest of
its lifetime. A failed local file read mid-transfer sends `MSG_CANCEL`
to the peer (best-effort) so it doesn't sit waiting forever, reports
`file_complete(success=False)`, and lets the session continue normally.
`connect_to_peer()` wraps its initial `connect()` call specifically, so
"peer's app isn't running" / "port not forwarded" / "wrong IP" produce
a clear message rather than a raw traceback.

## 7. Wire protocol — message-based framing

Every message on the wire:
```
[ 1 byte  : message type ]
[ 8 bytes : payload length (unsigned, big-endian) ]
[ N bytes : payload ]
```

Message types (all implemented and tested):
- `HELLO` (1) — listener's X25519 public key
- `FILE_OFFER` (2) — transfer_id (16 bytes), filesize, filename
- *(3 is a deliberate gap — an early whole-file-in-one-message design,
  fully replaced by chunking, removed as dead code)*
- `FILE_CHUNK` (4) — chunk index + SHA-256 hash of the plaintext chunk
  + the chunk data, encrypted (`[12-byte nonce][ciphertext+16-byte tag]`)
- `DONE` (5) — whole-file hash of the plaintext, sent after all chunks
- `HELLO_RESPONSE` (6) — sender's X25519 public key + confirmation tag
- `HELLO_OK` (7) — listener's own confirmation tag (reversed key order
  from `HELLO_RESPONSE`'s, preventing a reflection attack)
- `HELLO_REJECT` (8) — handshake failed; connection closes right after
- `FILE_ACCEPT` (9) / `FILE_REJECT` (10) — receiver's per-file decision
- `BYE` (11) — either side signals the session is closing
- `MSG_CANCEL` (12) — transfer_id; either side can abort a specific
  in-progress transfer; the other side stops sending/receiving that
  transfer's chunks, treated as an intentional stop, not an error

Using a generic envelope means adding new behavior (e.g. a future
resend request) is "add a new message type," not a protocol redesign.

**Per-chunk integrity:** each chunk is hashed (SHA-256) *before*
encryption on the sender side; the receiver decrypts, hashes the
decrypted bytes, and compares. A mismatch marks that chunk failed
without aborting the whole transfer — the receiver keeps going,
maximizing how much of a file lands correctly even under a flaky
connection, and produces a precise "here's what's still needed" list.

## 8. File storage & data model

All storage locations use `platformdirs` for OS-appropriate paths —
never relative paths (which depend on the launch directory and break
for a packaged, double-clicked app that might run from a read-only
location).

**Save location** (`storage.py`):
- `DEFAULT_SAVE_DIR` → `Downloads/P2P Transfer/` — finished files,
  same idea as where a browser puts its downloads.
- `get_save_dir()` / `set_save_dir(path)` / `reset_save_dir()` — the
  effective save location can be overridden (e.g. a future Settings
  screen); checked at the point of use, not cached, so a change takes
  effect immediately without restarting. `set_save_dir()` attempts to
  create the directory immediately, raising `OSError` on failure, so a
  bad choice is caught right away rather than discovered mid-transfer.
- `get_staging_dir()` → always computed from the *current* effective
  save dir, as a `Partial Downloads` subfolder — an override moves
  staging along with it automatically.

**Staging & finalization:**
- Each transfer gets a random 16-byte `transfer_id`, generated by the
  sender, included in `FILE_OFFER` — a stable identity independent of
  filename (filenames can collide or need renaming).
- Incoming files are written to staging at their correct byte offset
  (`chunk_index * chunk_size`), not appended sequentially — chunk
  order becomes irrelevant to correctness, and a future resumed/resent
  chunk can simply be written to its correct spot.
- The destination file is pre-allocated to its full final size before
  any chunks arrive — an out-of-disk-space condition fails immediately
  and clearly, rather than partway through a large transfer.
- Staged files are named `<original filename>.<transfer_id hex>.part`
  (+ matching `.json` manifest), not just the raw transfer_id — this is
  deliberate: staging is meant to be genuinely browsable (not hidden
  app-data), and a curious person should be able to tell what a partial
  file *is* at a glance. The manifest is plain, readable JSON
  (`{"filename": ..., "verified_chunks": [0, 2]}`) — exactly what a
  future resume feature needs to know "what's still missing."
- The receiver keeps receiving even if a chunk fails its hash check
  (or fails to decrypt) — writes what arrived, records the index as
  failed, continues.
- Finalization (moving the file into the real save location, under
  its real name) only happens when a transfer completes with zero
  failed chunks and a matching whole-file hash. Otherwise the staged
  file and manifest are left in place, not discarded.
- Collision-safe naming (`photo (1).jpg`, incrementing) applies only
  at finalization time.

**Crash-safe JSON persistence** (`json_store.py`), used by every JSON
file the app writes (contacts, history, settings, manifests):
- `atomic_write_json()` — writes to a temp file, then atomically
  renames over the real one, so a crash or power loss mid-write can
  never leave a corrupted real file behind.
- `safe_load_json()` — falls back to a sensible default if a file is
  missing or corrupted, preserving a corrupted file under a
  `.corrupted` suffix rather than silently losing it or crashing the
  whole app on startup.

**Contacts** (`contacts.py`) — `{name: {ip, port, passphrase,
paired_at, alias}}`, in a config directory (not "a JSON file next to
the app").

**Transfer history** (`history.py`) — a capped (500 entries, oldest
trimmed) local log of past transfers (direction, peer, filename, size,
success, detail, timestamp). Recording is the presentation layer's
responsibility (call `record_transfer()` on a `file_complete` event) —
this module doesn't listen to the engine directly, staying decoupled
and independently testable.

**Own identity** (`my_identity.py`) — persists display name and
default listening port so the one-click sharing workflow (§9) doesn't
require retyping them. Deliberately does *not* persist the public IP
(always fetched live) or the pairing code (meant to stay short-lived).

**Filename safety** (`storage.py`'s `sanitize_filename`): strips path
traversal (`os.path.basename`), replaces characters Windows forbids
entirely in filenames, strips illegal trailing dots/spaces, and caps
length at 200 characters (preserving the extension) — an unusually
long filename from a peer could otherwise push a staged filename past
typical filesystem limits.

## 9. Pairing & connection-string workflow

**`connection_string.py`** — builds/parses the shareable pairing
string. Encrypts `{name, ip, port, passphrase}` using a PBKDF2-derived
key from the short pairing code, base64-encodes, wraps in `----`
markers. Any failure (wrong code, corrupted string, garbage input)
collapses to the same generic `None` — deliberately not distinguishing
why, so a failed attempt doesn't hand an attacker a calibration signal.

**`pairing.py`** — coordinates `connection_string.py`, `contacts.py`,
and `engine.py`, which are deliberately decoupled from each other (none
of them know the others exist). This coordination exists specifically
because skipping or misordering a step across these three produces a
real, confusing bug rather than an obvious crash:
- `create_invite(engine, name, ip, port, pairing_code)` — generates a
  fresh random passphrase, builds the string, and immediately registers
  that passphrase with the engine and saves the contact — otherwise
  the very friend you just invited would be rejected, since your
  listener was never told to expect them.
- `accept_invite(engine, received_string, pairing_code)` — parses and
  saves the contact, also registering the passphrase with your own
  engine (since it's symmetric — that same contact can connect back to
  you later too).
- `load_contacts_into_engine(engine)` — call once at app startup;
  without it, a fresh `Engine()` instance (e.g. after restarting the
  app) starts with no memory of previously-paired contacts.
- `forget_contact(engine, name)` — removes a contact AND makes a
  *running* engine forget their passphrase immediately; removing from
  disk alone would leave a "removed" contact still able to connect
  until the app happened to restart.
- `rename_contact(engine, old_name, new_name)` — see §4's alias/rename
  section.
- `get_contact_freshness(name, stale_after_days=30)` — see §4.
- `quick_share(engine, pairing_code)` — the one-click "copy my info"
  action: uses saved identity (name, port), fetches a fresh public IP,
  starts listening automatically if not already, generates+registers
  the invite, all in one call. Returns the string; clipboard writing
  happens at the presentation layer.
- `paste_and_connect(engine, clipboard_text, pairing_code)` — parses a
  received string and connects immediately (not just saves a contact
  for later), since pasting a fresh invite implies intent to connect
  right now.

**Decided: no one-off/"guest" connections.** Every connection goes
through real pairing first — no temporary/anonymous trust option. A
successful handshake always corresponds to a saved contact, with no
exception to design around.

## 10. Distribution & installation plan

**Packaging (deferred until the GUI is stable — decisions made now):**
target is Windows first, via PyInstaller + Inno Setup.
- **`--onedir`, not `--onefile`.** A single-file build is more
  convenient to send a friend, but self-extracting packed executables
  are meaningfully more likely to trigger antivirus/Windows Defender
  false-positive flags, purely from looking suspicious to heuristic
  scanners. `--onedir` (a folder, zipped for distribution) triggers
  this far less.
- **Two genuinely different "ports," not to be conflated:** (1) the
  actual P2P listening port, which needs router forwarding and
  triggers a normal Windows Firewall prompt; (2) a local-only channel
  between the Python engine and a future GUI frontend (websocket or
  `pywebview`'s JS bridge), which never leaves 127.0.0.1 and never
  triggers any firewall prompt at all.
- **WebView2 dependency check:** most modern Windows installs already
  have it, but not guaranteed (older/locked-down/LTSC installs might
  lack it) — `pywebview` can silently fall back to a legacy IE-based
  renderer if missing, undermining the "modern, animated" GUI goal.
  Plan: installer checks and silently runs Microsoft's small Evergreen
  Bootstrapper if absent.
- **Pre-configure the firewall exception during install** (via `netsh
  advfirewall` in the Inno Setup script) rather than relying on the
  reactive runtime prompt.
- Linux: explicitly deprioritized — `pywebview` needs system-level
  GTK/WebKit (or Qt/WebEngine) libraries PyInstaller can't fully bundle
  away. Revisit if Linux support becomes a real priority.

**Expected friction, documented rather than "fixed" (not really
fixable):**
- Windows SmartScreen warning on an unsigned `.exe` — normal for a
  personal/indie app without a paid code-signing certificate.
- Port forwarding remains manual — no fully reliable way to automate
  this across arbitrary home routers. UPnP could offer a "try this
  first, fall back to manual" experience for routers that support it,
  but manual instructions will always need to remain the real fallback.

## 11. GUI plan

**What already exists to build on** (all engine-side, tested,
presentation-agnostic): the full command/event set on `Engine`,
`pairing.py`'s coordinating functions (the *only* correct way to touch
`engine.known_passphrases` — never call `engine.set_known_passphrase()`
directly from GUI code), `contacts.py`, `history.py`, `my_identity.py`,
`network_info.py`.

**Planned toolkit:** a local web UI (HTML/CSS/JS frontend, rendered
via `pywebview` for a native-feeling window) rather than a native
Python GUI toolkit, specifically to support modern, animated visuals
(e.g. a chunk-by-chunk progress grid) — browser engines GPU-accelerate
this kind of rendering by default. The engine's event queue maps
naturally onto this: push events to the frontend over a local
websocket as they occur, rather than polling.

**Decided UI behaviors:**
- **Unverified incoming connection state:** `session_started` fires
  before the handshake resolves — show `Unknown (<ip>) — verifying...`
  immediately, update in place to the real name on success, or show
  `Unknown (<ip>) — rejected` briefly then auto-dismiss on failure
  (chosen over either permanent visibility or total silence).
- **Connection failure messaging** — map specific event reasons to
  specific copy, not one generic "connection failed":

  | Event | Likely cause | Message |
  |---|---|---|
  | `session_closed`, "Could not connect" | Stale IP | "Couldn't reach `<name>` — have they changed networks?" |
  | `handshake_result`, timeout reason | App not running | "Connected, but `<name>`'s app didn't respond." |
  | `handshake_result`, "Authentication failed" | Corrupted/stale contact | "Security check failed — this contact may need re-pairing." |

- **System notifications:** browser Notification API (not a Python
  library) — fits the existing architecture with zero new
  dependencies; the frontend reacts to `file_offer_received` itself.
- **Drag-and-drop / multi-file:** N files dropped = N separate
  sequential offers, reusing `send_file()`'s existing queueing as-is.
- **Close-app-mid-transfer:** warn first (`Engine.
  has_active_transfers()`) — not because closing is destructive
  (staging already makes it safely resumable), but to give the person
  a chance to reconsider.
- **Field confirmation (Name/IP/Port):** each has a different
  relationship to live state (see §6's `listening_port` and §3's IP
  freshness) — Port shows a separate status badge sourced from
  `engine.listening_port`, never the live text field; IP is tagged
  "auto-detected" vs. "manually entered" with a refresh option; Name
  just needs standard auto-save-with-confirmation.

**Settings screen (planned; save-location override already built —
see §8):** other good-fit candidates, not yet built: auto-accept from
trusted contacts (opt-in per-contact), notification on/off, theme,
auto-listen-on-launch, clear all data, export/import contacts.
Deprioritized: bandwidth throttling (needs real rate-limiting logic),
minimize-to-tray. Explicitly excluded from ever exposing: chunk size,
language/locale, log verbosity.

**Not yet designed:** concrete layout, the exact websocket/JS-bridge
wiring mechanism, and the connection-string UI flow's fine details.

## 12. Known limitations & deliberately deferred features

- No real resume yet (reconnect + "here's what's missing" negotiation)
  — groundwork (transfer IDs, manifests, offset-based writes) is in
  place, the actual two-way negotiation isn't built.
- No public-key identity model — see §4's honest limitation.
- Linux packaging deprioritized.
- No automation-oriented CLI mode (JSON output, exit codes,
  non-interactive accept policies) — deferred until a real automation
  use case exists; the engine's command/event interface already
  supports this without changes.
- No peer folder browsing/requesting — a bigger feature needing its
  own permission model.
- `cli.py` does not yet expose several engine/pairing capabilities
  that exist and are tested: `cancel_transfer`, `get_state_snapshot`,
  `quick_share`/`paste_and_connect`, `rename_contact`/`set_alias`, and
  persistent `trust` (currently in-memory only for a CLI session, not
  backed by `contacts.json`). Worth closing this gap before or
  alongside GUI work, since `pairing.py` already provides everything
  needed.
