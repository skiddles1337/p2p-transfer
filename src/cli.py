"""
cli.py

A minimal interactive command-line interface for the P2P transfer
engine. This is a real, permanent way to use the app - not a
throwaway test script - but deliberately small in scope for now:
type commands, see events printed as they happen.

This is also our first real test of the engine under UNPREDICTABLE
human timing (as opposed to tests/test_engine.py's scripted,
auto-accepting scenarios) - a good sanity check before building
anything more elaborate (like a GUI) on top of the same engine.

Commands:
  listen <port>                       start listening for connections
  stoplisten                          stop listening for new connections
  trust <name> <passphrase>           accept incoming connections using this passphrase, identified as <name>
  connect <ip> <port> <passphrase> [name]   connect to a peer
  send <session_id> <path>            offer a file on an existing session
  accept <offer_id>                   accept a pending incoming file offer
  reject <offer_id>                   reject a pending incoming file offer
  sessions                            list active sessions
  help                                show this help
  quit                                exit
"""

import threading
from engine import Engine


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
            print(f"[session {event['session_id']}] chunk {shown_index}/"
                  f"{event['total_chunks']} - {event['status']}")

        elif etype == "file_complete":
            verdict = "SUCCESS" if event["success"] else "FAILED"
            print(f"[session {event['session_id']}] Transfer {verdict}: {event['detail']}")

        elif etype == "session_closed":
            print(f"[session {event['session_id']}] closed ({event['reason']})")

        else:
            # Fallback for any event type we haven't special-cased -
            # ensures nothing silently disappears if the event set
            # grows later.
            print(f"[event] {event}")


def print_help() -> None:
    print("""
Commands:
  listen <port>                       start listening for connections
  stoplisten                          stop listening for new connections
  trust <name> <passphrase>           accept incoming connections using this passphrase, identified as <name>
  connect <ip> <port> <passphrase> [name]   connect to a peer
  send <session_id> <path>            offer a file on an existing session
  accept <offer_id>                   accept a pending incoming file offer
  reject <offer_id>                   reject a pending incoming file offer
  sessions                            list active sessions
  help                                show this help
  quit                                exit
""")


def main() -> None:
    engine = Engine()
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

            elif cmd == "trust":
                # trust <name> <passphrase> - register a passphrase
                # this engine will accept from an INCOMING connection,
                # identified afterward by this name.
                name, passphrase = parts[1], parts[2]
                engine.set_known_passphrase(name, passphrase)
                print(f"Now accepting incoming connections using passphrase "
                      f"for '{name}'.")

            elif cmd == "connect":
                # connect <ip> <port> <passphrase> [name]
                ip, port, passphrase = parts[1], int(parts[2]), parts[3]
                peer_name = parts[4] if len(parts) > 4 else None
                session_id = engine.connect_to_peer(ip, port, passphrase, peer_name)
                print(f"Connecting... assigned session id {session_id}")

            elif cmd == "send":
                session_id = int(parts[1])
                path = " ".join(parts[2:])  # allow spaces in file paths
                engine.send_file(session_id, path)

            elif cmd == "accept":
                engine.respond_to_offer(int(parts[1]), True)

            elif cmd == "reject":
                engine.respond_to_offer(int(parts[1]), False)

            elif cmd == "sessions":
                if not engine.sessions:
                    print("No active sessions.")
                for sid, session in engine.sessions.items():
                    print(f"  session {sid}: {session.addr} ({session.direction}) "
                          f"peer_name={session.peer_name}")

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
