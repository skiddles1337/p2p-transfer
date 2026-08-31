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
"ready to receive" action generates a single string containing:
- host's name
- public IP
- port
- a freshly auto-generated passphrase (via `secrets.token_urlsafe`)

Format (human-readable, simple to parse):
```
name=Alex;ip=82.14.55.10;port=5001;pass=xk9-4jd2-plm1
```

The receiving friend pastes this whole string into their app to
"knock" / initiate a connection attempt.

## Security
- The passphrase gates the connection: mismatched passphrase = instant
  drop, before any file metadata is even exchanged.
- Since some files may contain PII, the connection is also
  **encrypted** (not just passphrase-gated). We derive an encryption
  key from the shared passphrase (via PBKDF2) and use it with
  `cryptography`'s `Fernet` to encrypt each chunk before sending.
- This is symmetric encryption based on a shared secret (the
  passphrase) — good enough for two friends exchanging files, not a
  full PKI/certificate system.

## Transfer model
- **One TCP connection per file** (not one long-lived pipe per
  session). This isolates failures — if file 3 of 5 fails, files 1-2
  are unaffected and file 3 can just be retried independently.
- A "session" is a UI-level concept: an established, ready peer
  relationship (validated passphrase) that you can send multiple files
  through, one connection at a time.

## Wire protocol — message-based framing
Rather than hardcoding "filename, then size, then bytes" as fixed byte
offsets, every message on the wire has a generic envelope:

```
[ 1 byte  : message type ]
[ 8 bytes : payload length (unsigned, big-endian) ]
[ N bytes : payload ]
```

Message types (v1):
- `HELLO`      — passphrase check, sent first on every connection
- `FILE_OFFER` — filename, filesize, chunk size, total chunk count
- `FILE_CHUNK` — chunk index + encrypted chunk bytes + chunk hash
- `ACK`        — generic acknowledgement (e.g. "chunk received OK")
- `ERROR`      — human-readable error message, connection will close
- `DONE`       — sender signals no more chunks; whole-file hash included
- `BYE`        — either side signals it's closing the session

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

## Failure handling (v1)
- If a chunk fails its hash check or the connection drops mid-file:
  retry the whole file from scratch (simple, correct).
- Chunk-level hashing/framing is designed so that a future "resume
  from last good chunk" feature is an additive change, not a rewrite.

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

## Build phases
1. Core transfer engine (CLI only): message-based framing, chunked
   send/receive, per-chunk hashing, encryption from the start
2. Passphrase handshake + accept/reject (still CLI)
3. GUI shell wrapping the working CLI logic
4. Contacts persistence, clipboard, connection-string parsing, stats
   display
5. (Future) Resume support, using the chunk boundaries already in
   place
