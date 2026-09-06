"""
cli.py

A minimal interactive command-line interface for the P2P transfer
engine. This is a real, permanent way to use the app - not a
throwaway test script - now with full command parity to the GUI's
pairing/contacts capabilities (quickshare/pasteconnect/contacts/
cancel/snapshot), not just the original manual trust/connect flow.

This was also our first real test of the engine under UNPREDICTABLE
human timing (as opposed to tests/test_engine.py's scripted,
auto-accepting scenarios) - a good sanity check before the GUI was
built on the same engine.

Commands:
  listen <port>                       start listening for connections
  stoplisten                          stop listening for new connections
  whoami <name> <port>                set your own name/default port (for quickshare)
  quickshare <pairing_code>           generate + register an invite string, using
                                       your identity from 'whoami'
  pasteconnect <code> <string>        parse a received connection string and
                                       connect immediately (no separate accept step)
  contacts                            list saved contacts
  connectto <name>                    connect directly to an already-saved contact
  alias <name> <alias>                set a cosmetic nickname for a saved contact
  forget <name>                       remove a saved contact
  trust <name> <passphrase>           accept incoming connections using this passphrase,
                                       identified as <name> (manual/ephemeral - prefer
                                       quickshare/pasteconnect for anything persistent)
  connect <ip> <port> <passphrase> [name]   connect to a peer directly (manual)
  send <session_id> <path>            offer a file on an existing session
  accept <offer_id>                   accept a pending incoming file offer
  reject <offer_id>                   reject a pending incoming file offer
  cancel <session_id> <transfer_id>   cancel an in-progress transfer
  sessions                            list active sessions
  snapshot                            print the full current engine state
  help                                show this help
  quit                                exit
"""

import json
import threading
from engine import Engine
import pairing
import contacts as contacts_module
import my_identity


def print_events(engine: Engine) -> None:
    """
    Runs on its own thread for the lifetime of the program: pulls
    events off the engine's queue and prints a human-readable line for
    each one. Runs independently of the command-input loop below, so
    events can appear at any time, interleaved with whatever the
    person is typing - same as how a GUI would react to events
    whenever they arrive, not just when someone happens to be looking.
    """
    while True:
        event = engine.event_queue.get()
        etype = event["type"]

        if etype == "log":
            print(f"[log] {event['message']}")

        elif etype == "session_started":
            print(f"[session {event['session_id']}] started with "
                  f"{event['peer_addr']} ({event['direction']})")

        elif etype == "handshake_result":
            if event["success"]:
                name_info = f" (identified as '{event['peer_name']}')" if event.get("peer_name") else ""
                print(f"[session {event['session_id']}] handshake OK{name_info}")
            else:
                print(f"[session {event['session_id']}] handshake FAILED: {event['reason']}")

        elif etype == "file_offer_received":
            print(f"[session {event['session_id']}] Incoming offer #{event['offer_id']}: "
                  f"'{event['filename']}' ({event['filesize']} bytes) -- "
                  f"type 'accept {event['offer_id']}' or 'reject {event['offer_id']}'")

        elif etype == "file_offer_answered":
            verdict = "ACCEPTED" if event["accepted"] else "REJECTED"
            print(f"[session {event['session_id']}] Offer for '{event['filename']}' was {verdict}")

        elif etype == "chunk_progress":
            shown_index = event["chunk_index"] + 1  # 1-based for display

            rate = event.get("bytes_per_second")
            eta = event.get("eta_seconds")

            if rate is not None:
                rate_str = f"{rate / 1_000_000:.2f} MB/s"
            else:
                rate_str = "calculating..."

            if eta is not None:
                eta_str = f"{eta:.0f}s remaining"
            else:
                eta_str = ""

            done = event.get("bytes_transferred", 0)
            total = event.get("total_bytes", 0)
            percent = (done / total * 100) if total else 0

            print(f"[session {event['session_id']}] chunk {shown_index}/"
                  f"{event['total_chunks']} - {event['status']} "
                  f"({percent:.0f}%, {rate_str}"
                  f"{', ' + eta_str if eta_str else ''})")

        elif etype == "file_complete":
            verdict = "SUCCESS" if event["success"] else "FAILED"
            print(f"[session {event['session_id']}] Transfer {verdict}: {event['detail']}")

        elif etype == "session_closed":
            print(f"[session {event['session_id']}] closed ({event['reason']})")

        else:
            print(f"[event] {event}")


def print_help() -> None:
    print("""
Commands:
  listen <port>                       start listening for connections
  stoplisten                          stop listening for new connections
  whoami <name> <port>                set your own name/default port (for quickshare)
  quickshare <pairing_code>           generate + register an invite string, using
                                       your identity from 'whoami'
  pasteconnect <code> <string>        parse a received connection string and
                                       connect immediately (no separate accept step)
  contacts                            list saved contacts
  connectto <name>                    connect directly to an already-saved contact
  alias <name> <alias>                set a cosmetic nickname for a saved contact
  forget <name>                       remove a saved contact
  trust <name> <passphrase>           accept incoming connections using this passphrase,
                                       identified as <name> (manual/ephemeral - prefer
                                       quickshare/pasteconnect for anything persistent)
  connect <ip> <port> <passphrase> [name]   connect to a peer directly (manual)
  send <session_id> <path>            offer a file on an existing session
  accept <offer_id>                   accept a pending incoming file offer
  reject <offer_id>                   reject a pending incoming file offer
  cancel <session_id> <transfer_id>   cancel an in-progress transfer
  sessions                            list active sessions
  snapshot                            print the full current engine state
  help                                show this help
  quit                                exit
""")


def main() -> None:
    engine = Engine()

    loaded_count = pairing.load_contacts_into_engine(engine)
    if loaded_count:
        print(f"Loaded {loaded_count} saved contact(s).")

    threading.Thread(target=print_events, args=(engine,), daemon=True).start()

    print("P2P Transfer - interactive CLI. Type 'help' for commands.")

    while True:
        try:
            line = input("> ").strip()
        except EOFError:
            break

        if not line:
            continue

        parts = line.split()
        cmd = parts[0].lower()

        try:
            if cmd == "listen":
                engine.start_listening(int(parts[1]))

            elif cmd == "stoplisten":
                engine.stop_listening()

            elif cmd == "whoami":
                name, port = parts[1], int(parts[2])
                my_identity.set_identity(name=name, port=port)
                print(f"Identity set: {name}, default port {port}")

            elif cmd == "quickshare":
                pairing_code = parts[1]
                try:
                    invite = pairing.quick_share(engine, pairing_code)
                    print(f"Invite string (share this):\n{invite}")
                except RuntimeError as e:
                    print(f"Quick share failed: {e}")

            elif cmd == "pasteconnect":
                pairing_code = parts[1]
                connection_string = parts[2]
                session_id = pairing.paste_and_connect(engine, connection_string, pairing_code)
                if session_id is None:
                    print("Couldn't connect - check the pairing code and the connection string.")
                else:
                    print(f"Connected. Assigned session id {session_id}")

            elif cmd == "contacts":
                all_contacts = contacts_module.load_contacts()
                if not all_contacts:
                    print("No saved contacts.")
                for name, info in all_contacts.items():
                    display = info.get("alias") or name
                    freshness = pairing.get_contact_freshness(name)
                    stale_note = " (stale - consider re-pairing)" if freshness["is_stale"] else ""
                    print(f"  {display} ({name}): {info['ip']}:{info['port']}{stale_note}")

            elif cmd == "connectto":
                name = parts[1]
                contact = contacts_module.get_contact(name)
                if contact is None:
                    print(f"No saved contact named '{name}'.")
                else:
                    session_id = engine.connect_to_peer(
                        contact["ip"], contact["port"], contact["passphrase"], peer_name=name
                    )
                    print(f"Connecting... assigned session id {session_id}")

            elif cmd == "alias":
                name = parts[1]
                alias = " ".join(parts[2:])
                if contacts_module.set_alias(name, alias):
                    print(f"Alias set: '{name}' will show as '{alias}'.")
                else:
                    print(f"No saved contact named '{name}'.")

            elif cmd == "forget":
                name = parts[1]
                if pairing.forget_contact(engine, name):
                    print(f"Forgot contact '{name}'.")
                else:
                    print(f"No saved contact named '{name}'.")

            elif cmd == "trust":
                name, passphrase = parts[1], parts[2]
                engine.set_known_passphrase(name, passphrase)
                print(f"Now accepting incoming connections using passphrase "
                      f"for '{name}'.")

            elif cmd == "connect":
                ip, port, passphrase = parts[1], int(parts[2]), parts[3]
                peer_name = parts[4] if len(parts) > 4 else None
                session_id = engine.connect_to_peer(ip, port, passphrase, peer_name)
                print(f"Connecting... assigned session id {session_id}")

            elif cmd == "send":
                session_id = int(parts[1])
                path = " ".join(parts[2:])
                engine.send_file(session_id, path)

            elif cmd == "accept":
                engine.respond_to_offer(int(parts[1]), True)

            elif cmd == "reject":
                engine.respond_to_offer(int(parts[1]), False)

            elif cmd == "cancel":
                session_id = int(parts[1])
                transfer_id_hex = parts[2]
                engine.cancel_transfer(session_id, transfer_id_hex)

            elif cmd == "sessions":
                if not engine.sessions:
                    print("No active sessions.")
                for sid, session in engine.sessions.items():
                    print(f"  session {sid}: {session.addr} ({session.direction}) "
                          f"peer_name={session.peer_name}")

            elif cmd == "snapshot":
                print(json.dumps(engine.get_state_snapshot(), indent=2))

            elif cmd in ("help", "?"):
                print_help()

            elif cmd in ("quit", "exit"):
                break

            else:
                print(f"Unknown command: '{cmd}'. Type 'help' for a list.")

        except (IndexError, ValueError):
            print("Invalid arguments for that command. Type 'help' for usage.")

    print("Exiting.")


if __name__ == "__main__":
    main()
