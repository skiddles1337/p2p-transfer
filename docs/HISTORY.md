# Project History & Decisions Log

This is the narrative companion to `docs/DESIGN.md` — the story of how
the app got to its current state: bugs found and fixed, alternatives
considered and rejected, testing that surfaced real problems. None of
this should be required reading to understand what the app currently
does (that's `DESIGN.md`'s job) — this is here for context, and because
a lot of the actual learning happened in these bugs and decisions.

## Phase 1 — Core protocol & chunked transfer

Started with the basic message envelope (`[type][length][payload]`),
then built chunked send/receive with per-chunk SHA-256 hashing and a
final whole-file hash check. Files are pre-allocated to their full
size before any chunks arrive specifically so an out-of-disk-space
condition fails immediately and clearly, rather than partway through a
large transfer — and so random-offset chunk writes are safe regardless
of arrival order.

**Windows lesson learned early:** a file cannot be renamed or moved
while any process still holds it open (`WinError 32`) — unlike
Linux/Mac, which generally allow this. The fix (explicitly closing the
output file handle before renaming, rather than relying on a `finally`
block that ran too late) is now just how `_receive_file`/
`_handle_incoming_done` work, but it was a real bug the first time,
caught by testing on the actual target platform rather than only in
this Linux sandbox.

## Phase 2 — Staging & finalization design

Deliberately designed staging (`Partial Downloads`) to be genuinely
**visible and browsable**, not hidden app-data — the reasoning: seeing
in-progress/incomplete transfers is interesting on its own, and it's
exactly the state a future resume feature would read from. Staged
files are named `<original filename>.<transfer_id hex>.part` rather
than just the raw ID, specifically so someone browsing the folder can
tell what a partial file *is* at a glance.

## Phase 3 — Session handshake & the original passphrase model

The very first handshake used a single global `SHARED_PASSPHRASE`
constant — a stand-in, known from the start to need replacing with a
real per-contact model eventually (which it later was — see Phase 9).

## Phase 4 — Real encryption: X25519, HKDF, and (originally) Fernet

Built the authenticated X25519 Diffie-Hellman key exchange with HMAC
confirmation tags, closing the man-in-the-middle gap a raw DH exchange
alone would have. Originally used `cryptography`'s `Fernet` for chunk
encryption — simple, but Fernet base64-encodes its output, inflating
every chunk by roughly 33%. This got swapped for raw AES-256-GCM
directly (Phase 7) once the overhead was actually measured and judged
worth fixing.

**A genuinely important discovery during this phase:** an early design
considered deriving the file-encryption key directly from the short,
spoken pairing code. This was rejected specifically because a code
weak enough to say aloud comfortably is also weak enough to brute-force
offline against captured traffic — and worse, doing so would have
broken forward secrecy entirely (a compromised code could retroactively
decrypt every past session). The fix that stuck: two separate secrets
with two separate strength requirements (see `DESIGN.md` §4) — the
weak code only ever protects the connection string's metadata and
authenticates the key exchange; the actual encryption key is
independently *computed* by both sides through the DH math and never
transmitted in any form, giving genuine forward secrecy.

## Phase 5 — The engine rework: bidirectional sending & multi-session

The original `listener.py`/`sender.py` scripts assumed one fixed sender
and one fixed receiver per run. Replacing them with `engine.py` (a
presentation-agnostic core) to support true bidirectional sending and
multiple simultaneous sessions surfaced two real, serious bugs:

**Bug 1 — the send-queue race.** The first version of `send_file()`
spawned a new thread per call, each setting `session.pending_outgoing_
offer` independently. Rapid-fire testing via `cli.py` (typing `send`
several times before the first got a response — exactly the kind of
unpredictable human timing `test_engine.py`'s scripted delays never
exercised) exposed a real race: concurrent sender threads overwrote
each other's `pending_outgoing_offer`, so an incoming `FILE_ACCEPT`
could resolve the *wrong* thread's wait, causing offers and chunk data
to interleave out of order and crash the session. Fixed with a single
dedicated sender thread per session, draining a queue — this
eliminates the race by construction (only one thread could ever start
an outgoing offer) while *improving* the UX: multiple quick
`send_file()` calls now queue naturally instead of racing.

**Bug 2 — nested dispatch loops can't handle interleaved bidirectional
traffic.** An earlier reader loop called into a nested function
(`_receive_file`) with its own narrow inner loop recognizing only
`FILE_CHUNK`/`DONE`. Testing two peers sending files to each other
*simultaneously* on the same connection (zero coordination delay)
exposed the problem: while one side's reader thread was inside that
nested loop receiving a file, the *other* direction's `FILE_ACCEPT`
(for that same side's own, unrelated outgoing offer) arrived
interleaved on the same connection — and the nested loop, expecting
only file-chunk-related messages, treated it as "unexpected" and
crashed the whole session. Fixed by flattening the reader loop into a
single dispatcher that can handle any message type at any point,
tracking "am I currently receiving a file" as state on the session
object rather than as which nested function call is currently
executing. Verified: 5 consecutive runs of simultaneous bidirectional
sends, zero crashes.

**The general lesson from both bugs:** once two-way, asynchronous
traffic is possible on one connection, a message dispatcher must never
assume "the next message will be one of these few types" based on what
it's currently doing — only based on the actual state of the
conversation, which can have multiple independent threads of context
active at once. And: testing with genuinely unpredictable timing (a
human clicking at their own pace) catches bugs that thorough scripted/
automated tests can still miss.

## Phase 6 — `cli.py` as a real, permanent interface

Originally conceived as a throwaway test harness to stress-test the
engine under real human timing before building a GUI — but given the
clean engine/presentation separation, a real interactive CLI cost very
little extra to build properly, and was kept as a permanent, first-
class way to use the app (useful on low-spec machines; a natural base
for a possible future automation-oriented mode).

## Phase 7 — Switching Fernet to raw AES-GCM

Measured, not assumed: Fernet's base64 encoding added ~349,624 bytes of
overhead to a single 1MB chunk. Raw AES-256-GCM, used directly via
`chunk_crypto.py`, reduces this to a fixed 28 bytes per chunk (12-byte
nonce + 16-byte auth tag) regardless of chunk size, with identical
security properties (tampering still detected via `InvalidTag`).
Verified via a real 3MB multi-chunk transfer after the swap.

## Phase 8 — Distribution & installation planning

A dedicated planning session (before any packaging code existed) to
avoid designing the app in ways that would fight real Windows
distribution constraints later. Key realizations: relative file paths
(the original `received/` folder) would break for a packaged,
double-clicked executable that might run from a read-only location —
fixed by switching to `platformdirs`-based paths. Also decided:
`--onedir` over `--onefile` (packed single-file executables trigger
more antivirus false positives), and that the P2P listening port and a
future local GUI-communication channel are two genuinely different
things that shouldn't be conflated in either code or documentation.

## Phase 9 — Pre-GUI engine features round

A deliberate pass to close gaps *before* any frontend code existed,
specifically to avoid retrofitting the wire protocol or engine
internals around already-written frontend assumptions:
1. Multi-passphrase, identity-revealing handshake (replacing the single
   global `SHARED_PASSPHRASE` from Phase 3)
2. Cancel-in-progress transfers (`MSG_CANCEL`)
3. Rate/ETA in progress events
4. `contacts.py` (standalone persistence)
5. `connection_string.py` (the actual pairing-string encode/decode)

## Phase 10 — Adversarial testing round

A deliberate round of "break it on purpose" scenarios, beyond the two
big bugs in Phase 5:
- **Closing a session with files still queued to send:** originally,
  `close_session()` sent `BYE` immediately and any not-yet-started
  queued files silently vanished with no event at all. Fixed:
  `close_session()` now drains the queue first and emits a `log` event
  per cancelled file.
- **Double-responding to the same offer** (e.g. a UI double-click):
  tested both same-value and conflicting rapid double-calls. Both
  proved safe — conflicting rapid calls deterministically let the
  second call win (last-write-wins), and a stale response to an
  already-resolved offer is correctly rejected.
- **Multiple peers connecting at the literal same instant:** tested
  three peers connecting and sending simultaneously to one listener —
  all completed correctly and independently.
- **A peer that connects and sends nothing:** originally tied up one
  thread forever, waiting on a `HELLO_RESPONSE` that would never come —
  not a crash, but an unbounded resource leak. Fixed with a
  handshake-only timeout (15s), deliberately *not* a general per-
  session timeout, since an established session must be able to sit
  idle indefinitely.

## Phase 11 — Field confirmation & a real timing bug

While designing how a GUI should show "what's actually being used" for
the Name/IP/Port fields, realized the port field had a real gap: the
engine had no way to answer "am I actually listening, and on what
port" independent of whatever a UI text field currently displayed.
Added `Engine.listening_port` as the source of truth — and while
testing it, caught a genuine timing bug: `stop_listening()` didn't
clear this property immediately, relying instead on the background
accept thread noticing on its own ~1-second timeout cycle, meaning the
property could misleadingly report the old port for up to a second
after an explicit stop. Fixed by clearing it immediately in
`stop_listening()` itself.

## Phase 12 — Real-world multi-machine testing, and two production bugs

Testing across two actual machines (not just this sandbox) surfaced
two real bugs in the send path, both following the same underlying
pattern:

**Bug A:** `_send_one_file()`'s initial `os.path.getsize(filepath)`
call was completely unguarded. A bad/mistyped local path (e.g. a
relative path resolved from the wrong working directory) raised an
`OSError` that propagated all the way up to the sender loop's
exception handler — which is designed to end the loop for a genuinely
dead *connection*, but was doing the same thing for a simple local
file error, silently killing that session's ability to send anything
else for the rest of its lifetime. A subsequent `send_file()` call
would queue normally but never actually go anywhere, since nothing was
left running to process the queue.

**Bug B:** the same failure mode existed a second time, later in the
same function — if a file became unreadable *mid-transfer* (after the
offer was already accepted), the exact same fate awaited it.

**The fix, both times:** wrap the local file operations specifically,
report the failure clearly (and, for the mid-transfer case, send
`MSG_CANCEL` to the peer so it doesn't sit waiting forever for chunks
that will never come), and let the session's sender loop continue
normally. Verified with a genuinely forced read failure (a monkey-
patched file object that raises on a specific chunk) — deleting a file
mid-read on this OS didn't actually reproduce the failure, since POSIX
allows continued reads against an already-open, since-unlinked file
handle; the real test had to force the failure directly.

## Phase 13 — Small cheap fixes, found by deliberately re-scanning for gaps

1. **Crash-safe JSON persistence** (`json_store.py`): every JSON file
   the app writes previously wrote directly to the real file — a crash
   or power loss mid-write would leave a truncated, invalid file
   behind, and a naive `json.load()` on next startup could crash the
   whole app, not just lose one piece of data. Fixed with atomic
   writes (temp file + rename) and graceful load-failure recovery
   (fall back to a default, preserve the corrupted file under a
   `.corrupted` suffix). While wiring this in, a self-inflicted bug
   (removing the `json` import from `storage.py` while
   `write_manifest()` still used it directly) was caught immediately
   by rerunning the *full* test suite rather than grepping for an
   expected success line — a good reminder that partial verification
   can mask real breakage.
2. **`pairing.forget_contact()`** — the removal-side counterpart to
   earlier pairing gaps: removing a contact from disk alone left a
   running engine still willing to accept connections from them.
3. **Filename length cap** — an unusually long filename could push a
   staged file's name past typical filesystem limits.
4. **Zero-byte file transfer** — verified end-to-end rather than just
   reasoned about (it works: zero chunks needed, straight to `DONE`).

## Phase 14 — State snapshot design, and two more gaps

Designing `get_state_snapshot()` (so a frontend can rebuild its display
from a blank slate) surfaced two more instances of the same underlying
pattern as Phase 12/13's bugs — state that events describe once but
was never actually persisted anywhere a later snapshot could read:
- Outgoing transfer progress didn't exist as queryable state at all —
  it was a local variable inside `_send_one_file`'s loop. Fixed by
  promoting it to a real `OutgoingTransfer` object, mirroring the
  existing `IncomingTransfer`.
- Pending incoming offers didn't remember their own filename/filesize —
  a snapshot taken while an offer awaited a decision could say "an
  offer exists" but not what it actually was. Fixed by expanding each
  entry to include that detail.

Also added `Engine._state_lock`, since `sessions` and
`pending_incoming_offers` are mutated from multiple threads
concurrently — verified under real concurrent load (500 rapid snapshot
calls while 20 sessions connected simultaneously in a separate thread,
zero errors).

## Phase 15 — Rethinking contacts: staleness, identity, one-click flows

A deliberate step back to reconsider the whole contacts/pairing model,
prompted by a direct question: is the passphrase actually doing
anything useful? The honest answer: yes (access control + MITM
protection during the key exchange), but with a real weakness — it's a
static, shared bearer secret with no expiration, so a leaked
`contacts.json` means indefinite impersonation until manually caught. A
full public-key identity model (each device holds one long-term
keypair, pairing exchanges public keys rather than a shared secret)
was considered and judged more architecturally correct, but deliberately
not built — too large a change for what's currently a friends-only
tool. Contact staleness tracking was built instead, as a hygiene layer
on top of the existing model rather than a replacement for it: it
doesn't fix the underlying weakness, but nudges people toward
periodically refreshing trust.

Also built in this phase: `my_identity.py` (so your own name/port don't
need retyping), and the one-click `quick_share`/`paste_and_connect`
workflows, replacing what had been a multi-step manual flow.

## Phase 16 — Naming: alias vs. rename

Prompted by noticing the receiving side of a pairing has no say in
what name they're identified by — it's whatever the inviter called
themselves. Built two deliberately separate capabilities with very
different risk profiles: a purely cosmetic alias (zero risk, never
touches the actual security matching) and a true rename (which must
keep `contacts.json` and the running engine's `known_passphrases` in
sync atomically, or you get exactly the kind of silent data/engine-
state mismatch bug this whole contacts/pairing subsystem has run into
repeatedly). The first test of the rename feature was actually a false
pass — a test script called `engine.set_known_passphrase()` directly
without going through the real `contacts.add_contact()` flow first,
so `rename_contact()` correctly found nothing to rename and silently
returned `False`, which the test never checked. Retesting through the
real workflow caught this and confirmed the rename genuinely works,
including through a real live connection afterward.

## Phase 17 — Housekeeping & documentation audit

A deliberate pass to check for drift between what the code actually
does and what comments/docs claim, prompted by the sheer number of
files accumulated (17 `.py` files by this point) and rounds of change.
Found and fixed:
- `protocol.py` had genuinely dead code (`MSG_FILE_DATA`, a
  whole-file-in-one-message design fully replaced by chunking back in
  an early phase, but never removed) and a stale opening line ("knows
  nothing about sockets or files yet" — no longer an accurate
  description of a module that had grown into the full message
  envelope system).
- `keyexchange.py`'s docstring still described itself as "an isolated
  proof... before we wire this into sender.py/listener.py" — those
  files were retired back in Phase 5; this module has been core,
  actively-used production code (imported by `engine.py`) ever since.
- `DESIGN.md` itself had grown to over 1000 lines through pure
  chronological appending, with real factual drift as a result: it
  still claimed the passphrase was "still hardcoded" (wrong — fixed in
  Phase 9), still told a future reader that `tests/simulate_corruption.py`
  "needs updating" (wrong — it had already been kept current), listed
  `CANCEL` as merely "planned" (wrong — implemented in Phase 9), and
  described `SAVE_DIR`/`STAGING_DIR` as plain constants rather than the
  override-aware functions they'd become. Two significant, verified bug
  fixes (Phase 12's pair of "local file error kills the whole session"
  bugs) had never been documented in the design doc at all, despite
  being real, tested work. **Fixed by splitting the document in two:**
  `DESIGN.md` rewritten as a clean, current-state-only reference
  organized by topic, and this file (`HISTORY.md`) created to hold the
  narrative/rationale content that had been cluttering it — so the
  design doc can always be trusted as accurate without needing to
  mentally filter out superseded chronological cruft.
- `cli.py` was found to be genuinely *behind* the engine's real
  capabilities, not just out of date in its comments — no `cancel`
  command, no persistence for `trust` (doesn't save to `contacts.json`
  at all), and none of `quick_share`/`paste_and_connect`/
  `rename_contact`/`set_alias`/`get_state_snapshot` exposed anywhere.
  Flagged as a known gap in `DESIGN.md` §12, worth closing before or
  alongside GUI work.
