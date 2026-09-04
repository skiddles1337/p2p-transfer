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
    connect_to_peer(ip, port) -> session_id
    send_file(session_id, filepath)
    respond_to_offer(offer_id, accept: bool)
    close_session(session_id)

EVENTS (dicts pulled from engine.event_queue, each with a "type"):
    log                 {message}
    session_started     {session_id, peer_addr, direction}
    handshake_result    {session_id, success, reason}
    file_offer_received {session_id, offer_id, filename, filesize}
    file_offer_answered {session_id, filename, accepted}
    chunk_progress      {session_id, transfer_id, chunk_index,
                          total_chunks, status}   status: "sent" | "ok" | "failed"
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
    CHUNK_SIZE,
    TRANSFER_ID_LEN,
)
from auth import compute_confirmation_tag, verify_confirmation_tag
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
    STAGING_DIR,
    SAVE_DIR,
)


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

        data_path, manifest_path = staging_paths(transfer_id, safe_filename)
        self.data_path = data_path
        self.manifest_path = manifest_path
        preallocate_file(data_path, filesize)  # may raise OSError - caller handles
        self.output_file = open(data_path, "r+b")


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


class Engine:
    def __init__(self):
        self.event_queue = queue.Queue()
        self.sessions = {}
        self.pending_incoming_offers = {}

        self._session_id_counter = itertools.count(1)
        self._offer_id_counter = itertools.count(1)

        self._listen_socket = None
        self._listen_thread = None
        self._stop_listening_flag = threading.Event()

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
        conn = session.sock
        listener_private, listener_public = generate_keypair()
        listener_public_bytes = public_key_to_bytes(listener_public)
        self._send(session, MSG_HELLO, listener_public_bytes)

        msg_type, payload = recv_message(conn)
        if msg_type != MSG_HELLO_RESPONSE:
            self._send(session, MSG_HELLO_REJECT, b"")
            return None

        sender_public_bytes, sender_tag = unpack_hello_response(payload)
        if not verify_confirmation_tag(sender_tag, listener_public_bytes, sender_public_bytes):
            self._send(session, MSG_HELLO_REJECT, b"")
            return None

        sender_public = public_key_from_bytes(sender_public_bytes)
        shared_secret = compute_shared_secret(listener_private, sender_public)
        session_key = derive_session_key(shared_secret)

        our_tag = compute_confirmation_tag(sender_public_bytes, listener_public_bytes)
        self._send(session, MSG_HELLO_OK, our_tag)

        return ChunkCipher(session_key)

    def _do_handshake_as_sender(self, session):
        sock = session.sock
        msg_type, listener_public_bytes = recv_message(sock)
        if msg_type != MSG_HELLO:
            return None

        sender_private, sender_public = generate_keypair()
        sender_public_bytes = public_key_to_bytes(sender_public)

        tag = compute_confirmation_tag(listener_public_bytes, sender_public_bytes)
        self._send(session, MSG_HELLO_RESPONSE, pack_hello_response(sender_public_bytes, tag))

        msg_type, listener_tag = recv_message(sock)
        if msg_type != MSG_HELLO_OK:
            return None
        if not verify_confirmation_tag(listener_tag, sender_public_bytes, listener_public_bytes):
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

    def _run_session(self, session, as_listener):
        self.sessions[session.session_id] = session
        self._emit("session_started", session_id=session.session_id,
                   peer_addr=session.addr, direction=session.direction)

        session.sock.settimeout(self.HANDSHAKE_TIMEOUT_SECONDS)
        try:
            if as_listener:
                cipher = self._do_handshake_as_listener(session)
            else:
                cipher = self._do_handshake_as_sender(session)
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
        self._emit("handshake_result", session_id=session.session_id, success=True, reason=None)

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
        self.pending_incoming_offers[offer_id] = (decision, session)

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

    def _handle_incoming_chunk(self, session, payload):
        transfer = session.active_incoming
        if transfer is None:
            self._emit("log", message=f"Received a chunk with no active incoming "
                                      f"transfer on session {session.session_id} - ignoring")
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

        write_manifest(transfer.manifest_path, transfer.filename, transfer.filesize,
                       CHUNK_SIZE, transfer.verified_chunks)
        self._emit("chunk_progress", session_id=session.session_id,
                   transfer_id=transfer.transfer_id.hex(), chunk_index=chunk_index,
                   total_chunks=transfer.total_chunks,
                   status="ok" if chunk_ok else "failed")

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
                                           SAVE_DIR, transfer.safe_filename)
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

            try:
                srv.close()
            except OSError:
                pass

        self._listen_thread = threading.Thread(target=accept_loop, daemon=True)
        self._listen_thread.start()

    def stop_listening(self):
        self._stop_listening_flag.set()
        if self._listen_socket is not None:
            try:
                self._listen_socket.close()
            except OSError:
                pass
        self._emit("log", message="Stopped listening.")

    def connect_to_peer(self, ip, port):
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
            self._run_session(session, as_listener=False)

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
        filename = sanitize_filename(os.path.basename(filepath))
        filesize = os.path.getsize(filepath)
        transfer_id = os.urandom(TRANSFER_ID_LEN)
        total_chunks = math.ceil(filesize / CHUNK_SIZE) if filesize > 0 else 0

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
            return

        self._emit("file_offer_answered", session_id=session.session_id,
                   filename=filename, accepted=True)

        whole_file_hasher = hashlib.sha256()
        chunk_index = 0
        bytes_sent = 0

        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if chunk == b"":
                    break

                whole_file_hasher.update(chunk)
                chunk_hash = hashlib.sha256(chunk).digest()
                encrypted_chunk = session.cipher.encrypt(chunk)

                chunk_payload = pack_file_chunk(chunk_index, chunk_hash, encrypted_chunk)
                self._send(session, MSG_FILE_CHUNK, chunk_payload)

                bytes_sent += len(chunk)
                self._emit("chunk_progress", session_id=session.session_id,
                           transfer_id=transfer_id.hex(), chunk_index=chunk_index,
                           total_chunks=total_chunks, status="sent")
                chunk_index += 1

        final_hash = whole_file_hasher.digest()
        self._send(session, MSG_DONE, final_hash)
        self._emit("file_complete", session_id=session.session_id,
                   transfer_id=transfer_id.hex(), success=True, detail="sent")

    def respond_to_offer(self, offer_id, accept):
        entry = self.pending_incoming_offers.get(offer_id)
        if entry is None:
            self._emit("log", message=f"No pending offer with id {offer_id}")
            return
        decision, _session = entry
        decision.result = accept
        decision.event.set()

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
