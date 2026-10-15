"""vw-admin — operator CLI for the virtual worker backend.

Usage examples:
  vw-admin ping
  vw-admin team-add Product
  vw-admin user-add alice --team Product
  vw-admin meeting-start --platform meet --url https://meet.google.com/abc --team Product
  vw-admin ask --user alice "What did we agree about the launch?"
  vw-admin ask --user outsider "What did we agree about the launch?"   # -> no access
  vw-admin put "We agreed to launch on October 20." --meeting
  vw-admin reminders
  vw-admin meeting-end
"""
import json
import os
import socket
import sys
import threading

from vw_common import default_socket_path

SOCKET_PATH = default_socket_path()


def request(msg, timeout=120):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(SOCKET_PATH)
    buf = bytearray()

    def reader():
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        buf.extend(data)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    sock.sendall((json.dumps(msg) + "\n").encode())
    t.join(timeout)
    sock.close()
    if not buf:
        raise RuntimeError("no answer from backend")
    resp = json.loads(bytes(buf).split(b"\n", 1)[0].decode())
    if not resp.get("ok"):
        raise RuntimeError("backend error: %s" % resp.get("error"))
    return resp.get("data", {})


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "ping":
            data = request({"type": "ping", "req_id": "cli"})
            print(json.dumps(data, indent=2))
        elif cmd == "team-add":
            print(request({"type": "team.ensure", "req_id": "cli",
                           "name": args[0]}))
        elif cmd == "user-add":
            name, teams = args[0], []
            if "--team" in args:
                teams = args[args.index("--team") + 1:]
            print(request({"type": "user.ensure", "req_id": "cli",
                           "name": name, "teams": teams}))
        elif cmd == "meeting-start":
            team = args[args.index("--team") + 1]
            url = args[args.index("--url") + 1] if "--url" in args else ""
            platform = args[args.index("--platform") + 1] if "--platform" in args else "meet"
            print(request({"type": "meeting.start", "req_id": "cli",
                           "platform": platform, "url": url, "team": team}))
        elif cmd == "meeting-end":
            print(request({"type": "meeting.end", "req_id": "cli"}))
        elif cmd == "join":
            url = args[0]
            print("joining %s ..." % url)
            print(request({"type": "meeting.join", "req_id": "cli", "url": url},
                          timeout=200))
        elif cmd == "ask":
            user = args[args.index("--user") + 1]
            q = " ".join(a for a in args[args.index("--user") + 2:] if a != "--user")
            data = request({"type": "question", "req_id": "cli",
                            "requester": user, "text": q})
            print("Alex: %s" % data["answer"])
            if not data.get("allowed"):
                print("(access denied — no authorized data was used)")
        elif cmd == "put":
            text = args[0]
            print(request({"type": "kb.put", "req_id": "cli", "text": text}))
        elif cmd == "search":
            user = args[args.index("--user") + 1]
            q = " ".join(a for a in args[args.index("--user") + 2:] if a != "--user")
            print(request({"type": "kb.search", "req_id": "cli",
                           "requester": user, "query": q}))
        elif cmd == "reminders":
            data = request({"type": "reminders.list", "req_id": "cli"})
            for r in data["reminders"]:
                print(r)
            if not data["reminders"]:
                print("(no pending reminders)")
        elif cmd == "agent-status":
            print(request({"type": "agent.status", "req_id": "cli"}))
        elif cmd == "say":
            print(request({"type": "agent.say", "req_id": "cli",
                           "text": " ".join(args)}))
        elif cmd == "selftest":
            print(request({"type": "agent.selftest", "req_id": "cli"},
                          timeout=320))
        elif cmd == "demo":
            print(request({"type": "agent.demo", "req_id": "cli"}, timeout=70))
        else:
            print("unknown command: %s" % cmd)
            return 1
        return 0
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))