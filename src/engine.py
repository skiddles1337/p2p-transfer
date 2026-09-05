"""
engine.py

The core P2P transfer engine, deliberately built with NO knowledge of
how it's being presented (CLI, GUI, web frontend - anything). It
exposes a small set of COMMANDS (things you tell it to do) and reports
everything that happens via a stream of EVENTS (a thread-safe queue).
Whatever is presenting this to a person - a terminal, a desktop
window, a webpage - just calls commands and reacts to events; it never
needs to know about sockets, threads, or the wire protocol at all.

COMMANDS (methods on Engine):
    start_listening(port)
    stop_listening()
    connect_to_peer(ip, port, passphrase, peer_name=None) -> session_id
    send_file(session_id, filepath)
    cancel_transfer(session_id, transfer_id_hex)
    respond_to_offer(offer_id, accept: bool)
    close_session(session_id)
    get_state_snapshot() -> dict   (full current state - see below)
    has_active_transfers() -> bool

EVENTS (dicts pulled from engine.event_queue, each with a "type"):
    log                 {message}
    session_started     {session_id, peer_addr, direction}
    handshake_result    {session_id, success, reason}
    file_offer_received {session_id, offer_id, filename, filesize}
    file_offer_answered {session_id, filename, accepted}
    chunk_progress      {session_id, transfer_id, chunk_index,
                          total_chunks, status, bytes_transferred,
                          total_bytes, bytes_per_second, eta_seconds}
                          status: "sent" | "ok" | "failed". Rate/ETA
                          fields are None until enough time has
                          elapsed to estimate meaningfully.
    file_complete       {session_id, transfer_id, success, detail}
    session_closed      {session_id, reason}

ARCHITECTURE NOTES:
- Each session (one connection to one peer) gets exactly one dedicated
  READER thread, whose only job is to loop on recv_message() and react.
  Sending happens from WHATEVER thread calls send_file() or responds
  to an offer - never from the reader thread itself. This split is
  what makes bidirectional sending possible: nothing is ever stuck
  only-listening or only-sending.
- Since two threads could otherwise try to write to the same socket at
  once (corrupting the byte stream), every session has a send_lock
  that must be held during any sendall() call on that socket.
- Incoming file offers use a "PendingDecision" (a threading.Event +
  a place to store the answer) so the reader thread can BLOCK waiting
  for a decision without blocking any OTHER session's reader thread -
  each session's wait is independent.
- Outgoing offers use the same PendingDecision pattern, but from the
  opposite direction: the SENDING thread waits for the reader thread
  to receive and record a FILE_ACCEPT/FILE_REJECT.
"""

import socket
import threading
import queue
import itertools
import os
import math
import time
import hashlib
from chunk_crypto import ChunkCipher
from cryptography.exceptions import InvalidTag

from protocol import (
    recv_message,
    pack_message,
    pack_hello_response,
    unpack_hello_response,
    pack_file_offer,
    unpack_file_offer,
    pack_file_chunk,
    unpack_file_chunk,
    MSG_HELLO,
    MSG_HELLO_RESPONSE,
    MSG_HELLO_OK,
    MSG_HELLO_REJECT,
    MSG_FILE_OFFER,
    MSG_FILE_ACCEPT,
    MSG_FILE_REJECT,
    MSG_FILE_CHUNK,
    MSG_DONE,
    MSG_BYE,
    MSG_CANCEL,
    CHUNK_SIZE,
    TRANSFER_ID_LEN,
)
from auth import compute_confirmation_tag, verify_confirmation_tag, find_matching_passphrase
from keyexchange import (
    generate_keypair,
    public_key_to_bytes,
    public_key_from_bytes,
    compute_shared_secret,
    derive_session_key,
)
from storage import (
    staging_paths,
    write_manifest,
    finalize_transfer,
    preallocate_file,
    sanitize_filename,
    get_save_dir,
)


def _compute_rate_and_eta(start_time, bytes_done, total_bytes):
    """
    Given when a transfer started, how many bytes have been processed
    so far, and the total expected, return (bytes_per_second,
    eta_seconds). Shared by both the sending and receiving paths so
    the math (and its rough edges - e.g. near-zero elapsed time right
    at the start) only needs to be handled correctly once.
    """
    elapsed = time.monotonic() - start_time
    if elapsed <= 0 or bytes_done <= 0:
        return None, None

    bytes_per_second = bytes_done / elapsed
    bytes_remaining = max(total_bytes - bytes_done, 0)
    eta_seconds = bytes_remaining / bytes_per_second if bytes_per_second > 0 else None
    return bytes_per_second, eta_seconds


class PendingDecision:
    """
    A simple wait/signal primitive: one thread calls .wait() and
    blocks; another thread later sets .result and calls .event.set(),
    waking the first thread up. Used for both "waiting on a human's
    accept/reject" and "waiting on the peer's accept/reject response."
    """
    def __init__(self):
        self.event = threading.Event()
        self.result = None


class IncomingTransfer:
    """
    State for a file currently being received on a session. Held as
    session.active_incoming rather than as local variables inside a
    nested loop, so the flat reader loop can update this across
    multiple iterations while ALSO handling unrelated messages
    (like a FILE_ACCEPT for an outgoing offer) in between.
    """
    def __init__(self, filename, safe_filename, filesize, transfer_id):
        self.filename = filename
        self.safe_filename = safe_filename
        self.filesize = filesize
        self.transfer_id = transfer_id
        self.total_chunks = math.ceil(filesize / CHUNK_SIZE) if filesize > 0 else 0
        self.whole_file_hasher = hashlib.sha256()
        self.verified_chunks = []
        self.failed_chunk_indices = []
        self.start_time = time.monotonic()
        self.bytes_processed = 0

        data_path, manifest_path = staging_paths(transfer_id, safe_filename)
        self.data_path = data_path
        self.manifest_path = manifest_path
        preallocate_file(data_path, filesize)  # may raise OSError - caller handles
        self.output_file = open(data_path, "r+b")


class OutgoingTransfer:
    """
    State for a file currently being sent on a session. Held as
    session.active_outgoing - previously this was just a bare dict
    with "transfer_id"/"cancel_event" and nothing else, meaning
    progress (bytes sent so far) only ever existed as a local variable
    inside _send_one_file's loop, with no way for anything outside
    that function to ask "how far along is this?" Promoted to a real
    object, mirroring IncomingTransfer, specifically so
    get_state_snapshot() can describe outgoing progress just as
    completely as incoming progress - an asymmetry that would
    otherwise be a real, confusing gap for a GUI to work around.
    """
    def __init__(self, filename, filesize, transfer_id):
        self.filename = filename
        self.filesize = filesize
        self.transfer_id = transfer_id
        self.total_chunks = math.ceil(filesize / CHUNK_SIZE) if filesize > 0 else 0
        self.start_time = time.monotonic()
        self.bytes_sent = 0
        self.cancel_event = threading.Event()


class Session:
    """
    State for one connection to one peer. Each Session has its own
    reader thread and its own dedicated SENDER thread (draining
    send_queue one file at a time), plus its own send_lock guarding
    raw socket writes.
    """
    def __init__(self, session_id, sock, addr, direction):
        self.session_id = session_id
        self.sock = sock
        self.addr = addr
        self.direction = direction  # "inbound" or "outbound"
        self.cipher = None
        self.send_lock = threading.Lock()
        # Set while THIS side has an offer out, awaiting the peer's answer.
        self.pending_outgoing_offer = None
        # Files queued to send on this session - a DEDICATED sender
        # thread (started once the handshake succeeds) drains this
        # one at a time. This is what prevents multiple send_file()
        # calls from racing each other over pending_outgoing_offer -
        # only ever ONE outgoing offer is in flight per session at a
        # time, by construction, not by hoping callers behave.
        self.send_queue = queue.Queue()
        # The file CURRENTLY being received on this session, if any -
        # tracked as state here (not as "which nested loop we're
        # inside") so the single flat reader loop can handle
        # FILE_CHUNK/DONE for this transfer AND unrelated messages
        # (like a FILE_ACCEPT for our own outgoing offer) arriving
        # interleaved with it, without either one blocking the other.
        self.active_incoming = None
        # The file CURRENTLY being sent on this session, if any - an
        # OutgoingTransfer instance (see above), or None. The send
        # loop checks its cancel_event between chunks; setting that
        # event (via cancel_transfer()) is how we ask an in-progress
        # send to stop early.
        self.active_outgoing = None
        # Set once the handshake succeeds. For an INBOUND session,
        # this is whichever known_passphrases entry matched (i.e. who
        # we now know is connecting). For an OUTBOUND session, this
        # stays None here - the caller already knows who they called;
        # see connect_to_peer's peer_name parameter if you want that
        # tracked too.
        self.peer_name = None


class Engine:
    def __init__(self):
        self.event_queue = queue.Queue()
        self.sessions = {}
        self.pending_incoming_offers = {}
        # Guards mutations AND reads of the two dicts above -
        # sessions come and go from multiple session threads
        # concurrently, and get_state_snapshot() needs to iterate them
        # safely without risking a "dictionary changed size during
        # iteration" error if a session starts or ends mid-snapshot.
        # Cheap insurance at this scale (a handful of sessions, never
        # thousands) - not worth leaving to chance.
        self._state_lock = threading.Lock()

        # {name: passphrase} - the set of passphrases this engine will
        # accept from an INCOMING connection. During a listener-side
        # handshake, each is tried in turn (see auth.find_matching_
        # passphrase) - whichever matches both authenticates the
        # connection and identifies who it is. Managed via
        # set_known_passphrase()/remove_known_passphrase() below;
        # typically populated from saved contacts.
        self.known_passphrases = {}

        self._session_id_counter = itertools.count(1)
        self._offer_id_counter = itertools.count(1)

        self._listen_socket = None
        self._listen_thread = None
        self._stop_listening_flag = threading.Event()
        self._listening_port = None

    def set_known_passphrase(self, name, passphrase):
        """Register (or update) a passphrase this engine should accept
        from an incoming connection, under the given name."""
        self.known_passphrases[name] = passphrase

    def remove_known_passphrase(self, name):
        """Stop accepting a previously-registered passphrase."""
        self.known_passphrases.pop(name, None)

    # ---------- internal helpers ----------

    def _emit(self, event_type, **data):
        self.event_queue.put({"type": event_type, **data})

    def _send(self, session, msg_type, payload):
        """All writes to a session's socket go through here, holding
        the lock - this is what makes it safe for multiple threads
        (the reader thread's responses, and any send_file() caller) to
        write to the same socket without corrupting each other."""
        with session.send_lock:
            session.sock.sendall(pack_message(msg_type, payload))

    # ---------- handshake ----------

    def _do_handshake_as_listener(self, session):
        """
        Returns (ChunkCipher, matched_name) on success, or (None, None)
        on failure. matched_name is whichever entry in
        self.known_passphrases produced a matching tag - this is how
        the listener learns WHO just connected, without them having
        stated it anywhere.
        """
        conn = session.sock
        listener_private, listener_public = generate_keypair()
        listener_public_bytes = public_key_to_bytes(listener_public)
        self._send(session, MSG_HELLO, listener_public_bytes)

        msg_type, payload = recv_message(conn)
        if msg_type != MSG_HELLO_RESPONSE:
            self._send(session, MSG_HELLO_REJECT, b"")
            return None, None

        sender_public_bytes, sender_tag = unpack_hello_response(payload)

        matched_name = find_matching_passphrase(
            sender_tag, listener_public_bytes, sender_public_bytes,
            self.known_passphrases,
        )
        if matched_name is None:
            self._send(session, MSG_HELLO_REJECT, b"")
            return None, None

        matched_passphrase = self.known_passphrases[matched_name]

        sender_public = public_key_from_bytes(sender_public_bytes)
        shared_secret = compute_shared_secret(listener_private, sender_public)
        session_key = derive_session_key(shared_secret)

        our_tag = compute_confirmation_tag(matched_passphrase, sender_public_bytes, listener_public_bytes)
        self._send(session, MSG_HELLO_OK, our_tag)

        return ChunkCipher(session_key), matched_name

    def _do_handshake_as_sender(self, session, passphrase):
        sock = session.sock
        msg_type, listener_public_bytes = recv_message(sock)
        if msg_type != MSG_HELLO:
            return None

        sender_private, sender_public = generate_keypair()
        sender_public_bytes = public_key_to_bytes(sender_public)

        tag = compute_confirmation_tag(passphrase, listener_public_bytes, sender_public_bytes)
        self._send(session, MSG_HELLO_RESPONSE, pack_hello_response(sender_public_bytes, tag))

        msg_type, listener_tag = recv_message(sock)
        if msg_type != MSG_HELLO_OK:
            return None
        if not verify_confirmation_tag(listener_tag, passphrase, sender_public_bytes, listener_public_bytes):
            return None

        listener_public = public_key_from_bytes(listener_public_bytes)
        shared_secret = compute_shared_secret(sender_private, listener_public)
        return ChunkCipher(derive_session_key(shared_secret))

    # ---------- session lifecycle ----------

    # How long to wait for a peer to complete the handshake before
    # giving up. This ONLY applies during the handshake - once
    # established, a session should be able to sit idle indefinitely
    # (waiting for the next file offer, possibly minutes or hours
    # later) without being killed. A silent/hung peer during the
    # handshake itself, though, would otherwise tie up a thread
    # forever with no way to notice or recover.
    HANDSHAKE_TIMEOUT_SECONDS = 15

    def _run_session(self, session, as_listener, passphrase=None):
        with self._state_lock:
            self.sessions[session.session_id] = session
        self._emit("session_started", session_id=session.session_id,
                   peer_addr=session.addr, direction=session.direction)

        session.sock.settimeout(self.HANDSHAKE_TIMEOUT_SECONDS)
        matched_name = None
        try:
            if as_listener:
                cipher, matched_name = self._do_handshake_as_listener(session)
            else:
                cipher = self._do_handshake_as_sender(session, passphrase)
        except socket.timeout:
            self._emit("handshake_result", session_id=session.session_id,
                       success=False, reason="Handshake timed out - peer sent nothing")
            self._cleanup_session(session, reason="handshake timeout")
            return
        except (ConnectionError, OSError) as e:
            self._emit("handshake_result", session_id=session.session_id,
                       success=False, reason=str(e))
            self._cleanup_session(session, reason="handshake error")
            return
        finally:
            # Whether it succeeded, failed, or timed out, remove the
            # handshake-specific timeout before anything else uses
            # this socket - an established session's reader loop must
            # be able to wait indefinitely for the next message. The
            # socket may already be closed at this point (if a
            # timeout/error path above already called
            # _cleanup_session) - that's fine, nothing left to do.
            try:
                session.sock.settimeout(None)
            except OSError:
                pass

        if cipher is None:
            self._emit("handshake_result", session_id=session.session_id,
                       success=False, reason="Authentication failed")
            self._cleanup_session(session, reason="handshake failed")
            return

        session.cipher = cipher
        if matched_name is not None:
            session.peer_name = matched_name  # inbound: identified via passphrase match
        # outbound sessions already have peer_name set (if provided) by connect_to_peer
        self._emit("handshake_result", session_id=session.session_id, success=True,
                   reason=None, peer_name=session.peer_name)

        # One dedicated thread per session, draining send_queue one
        # file at a time - this is what guarantees only one outgoing
        # offer is ever in flight on this session at once.
        threading.Thread(
            target=self._sender_loop, args=(session,), daemon=True
        ).start()

        try:
            self._reader_loop(session)
        except (ConnectionError, OSError) as e:
            self._emit("log", message=f"Connection error on session {session.session_id}: {e}")
        except Exception as e:
            self._emit("log", message=f"Unexpected error on session {session.session_id}: {e}")
        finally:
            self._cleanup_session(session, reason="closed")

    def _cleanup_session(self, session, reason):
        try:
            session.sock.close()
        except OSError:
            pass
        session.send_queue.put(None)  # sentinel: stop this session's sender thread
        with self._state_lock:
            self.sessions.pop(session.session_id, None)
        self._emit("session_closed", session_id=session.session_id, reason=reason)

    # ---------- the reader loop: one per session, fully FLAT dispatch ----------
    #
    # Every message type is handled directly in this one loop, based
    # purely on session state (session.active_incoming,
    # session.pending_outgoing_offer) - NOT on which nested function
    # call we happen to be inside. This matters once bidirectional
    # sending is possible: a single TCP connection genuinely carries
    # two independent logical conversations (my outgoing offer's
    # accept/reject, and chunks for whatever I'm currently receiving),
    # and either can arrive at any moment relative to the other. An
    # earlier version of this loop called into a nested function
    # (_receive_file) with its own narrow inner loop that only
    # recognized FILE_CHUNK/DONE - a FILE_ACCEPT for our own outgoing
    # offer arriving while that nested loop was running got treated as
    # "unexpected" and crashed the whole session. Flattening into one
    # dispatcher, with in-progress-transfer state tracked externally,
    # fixes this: any message type can be handled correctly regardless
    # of what else is going on at the same time.

    def _reader_loop(self, session):
        while True:
            msg_type, payload = recv_message(session.sock)

            if msg_type == MSG_FILE_OFFER:
                self._handle_incoming_offer(session, payload)

            elif msg_type == MSG_FILE_CHUNK:
                self._handle_incoming_chunk(session, payload)

            elif msg_type == MSG_DONE:
                self._handle_incoming_done(session, payload)

            elif msg_type in (MSG_FILE_ACCEPT, MSG_FILE_REJECT):
                pending = session.pending_outgoing_offer
                if pending is not None:
                    pending.result = (msg_type == MSG_FILE_ACCEPT)
                    pending.event.set()

            elif msg_type == MSG_CANCEL:
                self._handle_cancel(session, payload)

            elif msg_type == MSG_BYE:
                self._emit("log", message=f"Peer sent BYE on session {session.session_id}")
                return

            else:
                self._emit("log", message=f"Unexpected message type {msg_type} "
                                          f"on session {session.session_id}")
                return

    def _handle_incoming_offer(self, session, payload):
        filename, filesize, transfer_id = unpack_file_offer(payload)
        offer_id = next(self._offer_id_counter)
        decision = PendingDecision()

        # Storing filename/filesize here (not just the decision +
        # session) is what lets get_state_snapshot() fully describe a
        # pending offer later - "someone wants to send you
        # vacation.jpg, 2MB" - rather than only knowing an offer_id
        # exists with no detail about what it actually is.
        with self._state_lock:
            self.pending_incoming_offers[offer_id] = {
                "decision": decision,
                "session": session,
                "filename": filename,
                "filesize": filesize,
            }

        self._emit("file_offer_received", session_id=session.session_id,
                   offer_id=offer_id, filename=filename, filesize=filesize)

        # This wait is a DIFFERENT kind of blocking than the old nested
        # receive loop was: we're waiting on a human decision, not
        # narrowly refusing to recognize other message types. Chunks
        # for some OTHER already-active incoming transfer would simply
        # sit in the OS socket buffer until we get back to recv_message()
        # - not lost, just delayed, which is fine.
        decision.event.wait()
        accept = decision.result
        with self._state_lock:
            self.pending_incoming_offers.pop(offer_id, None)

        self._send(session, MSG_FILE_ACCEPT if accept else MSG_FILE_REJECT, b"")
        self._emit("file_offer_answered", session_id=session.session_id,
                   filename=filename, accepted=accept)

        if accept:
            safe_filename = sanitize_filename(filename)
            try:
                session.active_incoming = IncomingTransfer(
                    filename, safe_filename, filesize, transfer_id
                )
            except OSError as e:
                self._emit("log", message=f"Could not allocate space for incoming file: {e}")

    def _handle_cancel(self, session, transfer_id_bytes):
        """
        The peer sent MSG_CANCEL for a specific transfer_id. Check
        both directions - it might be cancelling something WE are
        sending (stop our send loop), or telling us THEY won't send us
        any more of something we're receiving (abort our incoming
        transfer). At most one of these will actually match, since
        transfer_id is randomly unique per transfer.
        """
        if session.active_outgoing and session.active_outgoing.transfer_id == transfer_id_bytes:
            session.active_outgoing.cancel_event.set()

        if session.active_incoming and session.active_incoming.transfer_id == transfer_id_bytes:
            self._abort_incoming(session, reason="cancelled by peer")

    def _abort_incoming(self, session, reason):
        """
        Stop receiving the currently-active incoming transfer early
        (peer cancelled it, or we're cancelling it ourselves). The
        partial data stays in staging - same "leave it resumable"
        philosophy as a chunk failure or dropped connection, just
        triggered intentionally instead of by an error.
        """
        transfer = session.active_incoming
        if transfer is None:
            return
        if not transfer.output_file.closed:
            transfer.output_file.close()
        self._emit("file_complete", session_id=session.session_id,
                   transfer_id=transfer.transfer_id.hex(), success=False, detail=reason)
        session.active_incoming = None

    def _handle_incoming_chunk(self, session, payload):
        transfer = session.active_incoming
        if transfer is None:
            # Expected and harmless in one common case: we (or the
            # peer) just cancelled this transfer, but a few chunks
            # that were already in flight before the cancel took
            # effect are still arriving. Not a protocol violation -
            # just network timing - so this stays quiet rather than
            # looking like something's wrong.
            return

        chunk_index, expected_hash, encrypted_data = unpack_file_chunk(payload)

        try:
            chunk_data = session.cipher.decrypt(encrypted_data)
            chunk_ok = hashlib.sha256(chunk_data).digest() == expected_hash
        except InvalidTag:
            chunk_data = b""
            chunk_ok = False

        if chunk_ok:
            transfer.verified_chunks.append(chunk_index)
        else:
            transfer.failed_chunk_indices.append(chunk_index)

        if chunk_data:
            transfer.output_file.seek(chunk_index * CHUNK_SIZE)
            transfer.output_file.write(chunk_data)
            transfer.whole_file_hasher.update(chunk_data)

        # Count bytes toward the rate/ETA estimate regardless of
        # whether the chunk passed its hash check - what matters for
        # "how fast is data arriving" is wire throughput, not validity.
        transfer.bytes_processed += len(encrypted_data)
        bytes_per_second, eta_seconds = _compute_rate_and_eta(
            transfer.start_time, transfer.bytes_processed, transfer.filesize
        )

        write_manifest(transfer.manifest_path, transfer.filename, transfer.filesize,
                       CHUNK_SIZE, transfer.verified_chunks)
        self._emit("chunk_progress", session_id=session.session_id,
                   transfer_id=transfer.transfer_id.hex(), chunk_index=chunk_index,
                   total_chunks=transfer.total_chunks,
                   status="ok" if chunk_ok else "failed",
                   bytes_transferred=transfer.bytes_processed, total_bytes=transfer.filesize,
                   bytes_per_second=bytes_per_second, eta_seconds=eta_seconds)

    def _handle_incoming_done(self, session, payload):
        transfer = session.active_incoming
        if transfer is None:
            self._emit("log", message=f"Received DONE with no active incoming "
                                      f"transfer on session {session.session_id} - ignoring")
            return

        sender_whole_hash = payload
        our_whole_hash = transfer.whole_file_hasher.digest()
        transfer.output_file.close()  # MUST close before any rename (Windows)

        if transfer.failed_chunk_indices:
            detail = f"{len(transfer.failed_chunk_indices)} chunk(s) failed: {transfer.failed_chunk_indices}"
            success = False
        elif our_whole_hash != sender_whole_hash:
            detail = "whole-file hash mismatch despite no flagged chunk failures"
            success = False
        else:
            final_path = finalize_transfer(transfer.data_path, transfer.manifest_path,
                                           get_save_dir(), transfer.safe_filename)
            detail = final_path
            success = True

        self._emit("file_complete", session_id=session.session_id,
                   transfer_id=transfer.transfer_id.hex(), success=success, detail=detail)

        # Clear the active transfer - this session can now accept a
        # new incoming FILE_OFFER (or already has, if one arrived
        # while this one was finishing - it'll be queued behind this
        # call in the reader loop's next iteration).
        session.active_incoming = None

    # ---------- COMMANDS ----------

    def start_listening(self, port):
        self._stop_listening_flag.clear()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        srv.settimeout(1.0)
        self._listen_socket = srv
        self._listening_port = port
        self._emit("log", message=f"Listening on port {port}...")

        def accept_loop():
            while not self._stop_listening_flag.is_set():
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break

                conn.settimeout(None)
                session_id = next(self._session_id_counter)
                session = Session(session_id, conn, addr, direction="inbound")

                threading.Thread(
                    target=self._run_session, args=(session, True), daemon=True
                ).start()

            # Whatever caused this loop to exit - an explicit
            # stop_listening() call, or the socket erroring out for
            # some other reason - we're no longer actually listening,
            # so this must be cleared here rather than only in
            # stop_listening(). Otherwise listening_port could keep
            # reporting a port we've silently stopped listening on.
            self._listening_port = None
            try:
                srv.close()
            except OSError:
                pass

        self._listen_thread = threading.Thread(target=accept_loop, daemon=True)
        self._listen_thread.start()

    def stop_listening(self):
        self._stop_listening_flag.set()
        # Clear this immediately - we KNOW right now that we're
        # intentionally stopping, so there's no reason to wait for the
        # background accept thread to notice on its own timeout cycle
        # (which could take up to a second, during which listening_port
        # would misleadingly still report the old port as active).
        self._listening_port = None
        if self._listen_socket is not None:
            try:
                self._listen_socket.close()
            except OSError:
                pass

        # Closing the socket above does NOT guarantee the OS has
        # actually released the port yet - the accept_loop background
        # thread could still be blocked inside srv.accept() at this
        # exact moment (up to its 1-second timeout window). On Linux,
        # closing a socket while another thread is still blocked
        # inside a syscall on it doesn't necessarily release the
        # underlying port binding until that thread's own call
        # actually unblocks and returns. Without waiting here, an
        # immediate subsequent start_listening() on the SAME port
        # (e.g. quick_share() re-checking listening_port right away)
        # can race against this and fail with "Address already in
        # use" - reproduced reliably in testing. Joining here
        # (bounded, so a genuinely stuck thread can't hang this call
        # forever) ensures the thread has actually stopped touching
        # the socket before we report "stopped" to the caller.
        if self._listen_thread is not None:
            self._listen_thread.join(timeout=2.0)
        self._emit("log", message="Stopped listening.")

    @property
    def listening_port(self):
        """
        The port ACTUALLY currently being listened on, or None if not
        listening. This is the source of truth the GUI should check
        before trusting its own port input field - editing that field
        does NOT retroactively change what's actually listening; only
        calling start_listening() again does. A GUI showing "Listening
        on 5001" should read this property, not just echo back
        whatever the text field currently contains.
        """
        return self._listening_port

    def connect_to_peer(self, ip, port, passphrase, peer_name=None):
        """
        peer_name is optional and purely informational (e.g. "connecting
        to Alex") - unlike the listener side, an outbound connection
        already knows who it's calling; peer_name just lets that be
        reflected back in session.peer_name / events, for a nicer UI.
        """
        session_id = next(self._session_id_counter)

        def worker():
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.connect((ip, port))
            except OSError as e:
                self._emit("session_closed", session_id=session_id,
                           reason=f"Could not connect: {e}")
                return

            session = Session(session_id, sock, (ip, port), direction="outbound")
            session.peer_name = peer_name
            self._run_session(session, as_listener=False, passphrase=passphrase)

        threading.Thread(target=worker, daemon=True).start()
        return session_id

    def _sender_loop(self, session):
        """
        Runs for the lifetime of the session: pulls filepaths off
        send_queue one at a time and sends each fully (offer, wait for
        accept/reject, chunks if accepted) before moving to the next.
        A None sentinel signals this loop to stop (used on cleanup).
        """
        while True:
            filepath = session.send_queue.get()
            if filepath is None:
                return
            try:
                self._send_one_file(session, filepath)
            except (ConnectionError, OSError) as e:
                self._emit("log", message=f"Send failed on session {session.session_id}: {e}")
                return

    def send_file(self, session_id, filepath):
        session = self.sessions.get(session_id)
        if session is None or session.cipher is None:
            self._emit("log", message=f"Cannot send: session {session_id} not ready")
            return

        # Just enqueue - the session's dedicated sender thread (started
        # once, when the session's handshake succeeded) will process
        # this in order, one file at a time. Calling send_file()
        # several times quickly is safe and simply queues them up,
        # rather than racing multiple sends against each other.
        session.send_queue.put(filepath)

    def _send_one_file(self, session, filepath):
        # This is a LOCAL file access problem (bad path, permissions,
        # file deleted before we got to it) - fundamentally different
        # from a network/connection problem, and must be handled
        # differently: report it clearly and return, so the sender
        # loop (see _sender_loop) moves on to the NEXT queued file.
        # Previously this wasn't guarded at all, so an OSError here
        # (e.g. a bad path) propagated all the way up to _sender_loop's
        # except clause - which is designed to end the loop for a
        # genuinely dead CONNECTION, but was doing the same thing for
        # a simple typo in a file path, silently killing the session's
        # ability to send anything else for the rest of its lifetime.
        try:
            filename = sanitize_filename(os.path.basename(filepath))
            filesize = os.path.getsize(filepath)
        except OSError as e:
            self._emit("log", message=f"Could not read '{filepath}': {e}")
            return

        transfer_id = os.urandom(TRANSFER_ID_LEN)

        transfer = OutgoingTransfer(filename, filesize, transfer_id)
        session.active_outgoing = transfer

        decision = PendingDecision()
        session.pending_outgoing_offer = decision

        offer_payload = pack_file_offer(filename, filesize, transfer_id)
        self._send(session, MSG_FILE_OFFER, offer_payload)
        self._emit("log", message=f"Offered '{filename}' on session {session.session_id}")

        decision.event.wait()
        session.pending_outgoing_offer = None

        if not decision.result:
            self._emit("file_offer_answered", session_id=session.session_id,
                       filename=filename, accepted=False)
            session.active_outgoing = None
            return

        self._emit("file_offer_answered", session_id=session.session_id,
                   filename=filename, accepted=True)

        whole_file_hasher = hashlib.sha256()
        chunk_index = 0
        cancelled = False

        # This is guarded the same way as the initial filename/filesize
        # lookup above, for the same reason: the file could become
        # unreadable AFTER the offer was already accepted (deleted,
        # a USB drive pulled mid-read, etc.), and without this guard
        # that failure would propagate all the way up to
        # _sender_loop's except clause, silently killing the
        # session's ability to send anything else for the rest of its
        # lifetime - exactly the same bug as the initial lookup, just
        # triggered partway through instead of at the very start.
        #
        # We don't need to distinguish "local file problem" from "the
        # connection itself died" here - the READER thread independently
        # and reliably detects a genuinely dead connection on its own
        # (via its own recv_message() call failing) and triggers proper
        # session cleanup regardless of what happens on the sending
        # side. So it's safe to handle any failure here the same way:
        # tell the peer we're giving up on this specific transfer (best
        # effort - this send might also fail if the connection really
        # is dead, which is fine, the reader thread will still notice
        # separately), report it, and let this session keep running.
        try:
            with open(filepath, "rb") as f:
                while True:
                    if transfer.cancel_event.is_set():
                        cancelled = True
                        break

                    chunk = f.read(CHUNK_SIZE)
                    if chunk == b"":
                        break

                    whole_file_hasher.update(chunk)
                    chunk_hash = hashlib.sha256(chunk).digest()
                    encrypted_chunk = session.cipher.encrypt(chunk)

                    chunk_payload = pack_file_chunk(chunk_index, chunk_hash, encrypted_chunk)
                    self._send(session, MSG_FILE_CHUNK, chunk_payload)

                    transfer.bytes_sent += len(chunk)
                    bytes_per_second, eta_seconds = _compute_rate_and_eta(
                        transfer.start_time, transfer.bytes_sent, filesize
                    )
                    self._emit("chunk_progress", session_id=session.session_id,
                               transfer_id=transfer_id.hex(), chunk_index=chunk_index,
                               total_chunks=transfer.total_chunks, status="sent",
                               bytes_transferred=transfer.bytes_sent, total_bytes=filesize,
                               bytes_per_second=bytes_per_second, eta_seconds=eta_seconds)
                    chunk_index += 1

        except OSError as e:
            self._emit("log", message=f"Send failed partway through '{filename}': {e}")
            try:
                self._send(session, MSG_CANCEL, transfer_id)
            except OSError:
                pass  # connection's probably dead too - the reader thread will notice
            self._emit("file_complete", session_id=session.session_id,
                       transfer_id=transfer_id.hex(), success=False, detail=str(e))
            session.active_outgoing = None
            return

        if cancelled:
            self._send(session, MSG_CANCEL, transfer_id)
            self._emit("file_complete", session_id=session.session_id,
                       transfer_id=transfer_id.hex(), success=False, detail="cancelled")
        else:
            final_hash = whole_file_hasher.digest()
            self._send(session, MSG_DONE, final_hash)
            self._emit("file_complete", session_id=session.session_id,
                       transfer_id=transfer_id.hex(), success=True, detail="sent")

        session.active_outgoing = None

    def cancel_transfer(self, session_id, transfer_id_hex):
        """
        Cancel a specific in-progress transfer, identified by its
        transfer_id (as a hex string, matching how it's already
        exposed in chunk_progress/file_complete events). Works for
        either direction: if we're SENDING it, signals our own send
        loop to stop early and tells the peer via MSG_CANCEL; if we're
        RECEIVING it, tells the peer to stop sending (via MSG_CANCEL)
        and aborts our own reception immediately.
        """
        session = self.sessions.get(session_id)
        if session is None:
            self._emit("log", message=f"No such session {session_id} to cancel on")
            return

        try:
            transfer_id = bytes.fromhex(transfer_id_hex)
        except ValueError:
            self._emit("log", message=f"Invalid transfer_id for cancel: {transfer_id_hex}")
            return

        if session.active_outgoing and session.active_outgoing.transfer_id == transfer_id:
            session.active_outgoing.cancel_event.set()
            return

        if session.active_incoming and session.active_incoming.transfer_id == transfer_id:
            try:
                self._send(session, MSG_CANCEL, transfer_id)
            except OSError:
                pass
            self._abort_incoming(session, reason="cancelled locally")
            return

        self._emit("log", message=f"No active transfer {transfer_id_hex} "
                                  f"to cancel on session {session_id}")

    def respond_to_offer(self, offer_id, accept):
        with self._state_lock:
            entry = self.pending_incoming_offers.get(offer_id)
        if entry is None:
            self._emit("log", message=f"No pending offer with id {offer_id}")
            return
        decision = entry["decision"]
        decision.result = accept
        decision.event.set()

    def _transfer_progress_dict(self, transfer, start_time, bytes_done, status_field_name, status_value):
        """
        Shared shape-builder for describing an in-progress transfer,
        used by BOTH the incoming and outgoing branches of a session
        snapshot. Deliberately uses the SAME field names as the
        chunk_progress EVENT (bytes_transferred, total_bytes,
        bytes_per_second, eta_seconds) - so a frontend can render a
        transfer row identically whether it was first drawn from a
        snapshot or updated by a live event, without needing two
        different code paths for what is conceptually the same data.
        """
        bytes_per_second, eta_seconds = _compute_rate_and_eta(
            start_time, bytes_done, transfer.filesize
        )
        return {
            "transfer_id": transfer.transfer_id.hex(),
            "filename": transfer.filename,
            "total_chunks": transfer.total_chunks,
            "bytes_transferred": bytes_done,
            "total_bytes": transfer.filesize,
            "bytes_per_second": bytes_per_second,
            "eta_seconds": eta_seconds,
            status_field_name: status_value,
        }

    def _session_snapshot(self, session) -> dict:
        """
        Build a full, JSON-serializable description of ONE session -
        the per-session piece of get_state_snapshot() below.
        """
        info = {
            "session_id": session.session_id,
            "peer_addr": session.addr,
            "direction": session.direction,
            "peer_name": session.peer_name,
            "handshake_complete": session.cipher is not None,
            "active_incoming": None,
            "active_outgoing": None,
        }

        incoming = session.active_incoming
        if incoming is not None:
            info["active_incoming"] = self._transfer_progress_dict(
                incoming, incoming.start_time, incoming.bytes_processed,
                "chunks_failed", len(incoming.failed_chunk_indices),
            )

        outgoing = session.active_outgoing
        if outgoing is not None:
            info["active_outgoing"] = self._transfer_progress_dict(
                outgoing, outgoing.start_time, outgoing.bytes_sent,
                "cancelled", outgoing.cancel_event.is_set(),
            )

        return info

    def get_state_snapshot(self) -> dict:
        """
        Returns a full, JSON-serializable description of everything
        the engine currently knows: whether listening (and on what
        port), every active session (identity, handshake state, and
        any in-progress transfer's live progress), and every pending
        incoming offer still awaiting a decision.

        WHY THIS EXISTS: every other way of learning what the engine
        is doing is the one-shot event queue - fine for a frontend
        that's been listening continuously since the engine started,
        but insufficient the moment a frontend needs to REBUILD its
        display from a blank slate (a page reload, a websocket
        reconnecting after a hiccup, a GUI window reopening) without
        having seen every event that led to the current state. Events
        remain the mechanism for "something just happened, update
        incrementally"; this is the mechanism for "tell me everything,
        right now."

        Thread-safe: takes a stable, lock-protected copy of sessions
        and pending offers before building the description, so this
        can't race with a session starting or ending concurrently.
        """
        with self._state_lock:
            session_list = list(self.sessions.values())
            offer_items = list(self.pending_incoming_offers.items())

        sessions_snapshot = [self._session_snapshot(s) for s in session_list]

        offers_snapshot = [
            {
                "offer_id": offer_id,
                "session_id": entry["session"].session_id,
                "filename": entry["filename"],
                "filesize": entry["filesize"],
            }
            for offer_id, entry in offer_items
        ]

        return {
            "listening": self.listening_port is not None,
            "listening_port": self.listening_port,
            "sessions": sessions_snapshot,
            "pending_offers": offers_snapshot,
            "known_contact_names": sorted(self.known_passphrases.keys()),
        }

    def has_active_transfers(self) -> bool:
        """
        True if ANY session currently has a transfer in progress
        (sending or receiving) - meant for the GUI to check before
        letting the person close the app, so it can warn "you have an
        active transfer" rather than silently killing it mid-flight.

        Note: closing mid-transfer isn't actually DESTRUCTIVE (staging
        preserves partial data, resumable later) - this check is about
        giving the person a heads-up and a chance to reconsider, not
        preventing data loss that wouldn't otherwise happen.
        """
        return any(
            session.active_incoming is not None or session.active_outgoing is not None
            for session in self.sessions.values()
        )

    def close_session(self, session_id):
        session = self.sessions.get(session_id)
        if session is None:
            return

        # Anything still QUEUED (not yet started) gets explicitly
        # reported as cancelled, rather than silently vanishing when
        # the session actually closes. Note: this does NOT cover a
        # send that's already IN PROGRESS (pulled off the queue,
        # offer already sent) - that one will still race with BYE and
        # typically surface as a connection error, same as closing a
        # connection mid-transfer always would.
        while True:
            try:
                dropped_filepath = session.send_queue.get_nowait()
            except queue.Empty:
                break
            if dropped_filepath is None:
                continue  # the stop-sentinel, not a real queued file
            self._emit("log", message=f"Cancelled queued send (session "
                                      f"{session.session_id} closing): {dropped_filepath}")

        try:
            self._send(session, MSG_BYE, b"")
        except OSError:
            pass
