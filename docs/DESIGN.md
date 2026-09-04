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
  to actually knows the code"), via a challenge/response (e.g. HMAC of
  a random challenge value, keyed by the code) — **the code itself is
  never transmitted on the wire, in any form**, so there's nothing for
  an eavesdropper to directly capture and crack.
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
  close. Either side can initiate a file offer once the session is
  authenticated - not just the original connector.
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

## Wire protocol — message-based framing
Rather than hardcoding "filename, then size, then bytes" as fixed byte
offsets, every message on the wire has a generic envelope:

```
[ 1 byte  : message type ]
[ 8 bytes : payload length (unsigned, big-endian) ]
[ N bytes : payload ]
```

Message types (current + planned):
- `HELLO`          — sent first on every connection; challenge/response
                     handshake proves both sides know the short pairing
                     code, without transmitting the code itself
- `HELLO_OK` / `HELLO_REJECT` — handshake result; reject closes the
                     connection immediately (no retry on same connection)
- `FILE_OFFER`     — transfer_id, filesize, filename
- `FILE_ACCEPT` / `FILE_REJECT` — receiver's per-file decision, shown
                     via CLI prompt for now, GUI dialog later
- `FILE_CHUNK`     — chunk index + chunk hash + encrypted chunk bytes
- `DONE`           — sender signals no more chunks; whole-file hash
                     included, for a final end-to-end integrity check
- `BYE`            — either side signals the session is closing
- (planned) `CANCEL`, `RESEND_REQUEST` — additive later, once resume
  is built; the generic envelope means these don't require a protocol
  redesign

Implemented so far: HELLO (defined, not yet wired up), FILE_OFFER,
FILE_CHUNK, DONE. HELLO_OK/REJECT, FILE_ACCEPT/REJECT, and BYE are the
next step (session loop + handshake).

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
3. **Next:** persistent session connection + handshake — HELLO
   challenge/response, FILE_ACCEPT/REJECT loop, BYE (still CLI; a
   hardcoded shared code stands in for the real pairing-code flow
   until the GUI exists)
4. Real encryption — Diffie-Hellman (X25519) session key exchange,
   authenticated by the short pairing code; encrypt chunks with the
   resulting session key (Fernet or direct AEAD)
5. GUI shell wrapping the working CLI/session logic
6. Contacts persistence, clipboard, encrypted connection-string
   generation/parsing, live transfer stats (rate, ETA)
7. (Future) Real resume: reconnect + manifest-based "here's what's
   missing" exchange, using groundwork already in place
8. (Future, exploratory) Browsing/requesting files from a peer's
   shared folder — bigger feature, needs its own permission model,
   deliberately deferred
