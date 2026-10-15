"""Shared protocol helpers for the virtual worker agent and backend.

Messages are newline-delimited JSON over a Unix domain socket.
Every message has a "type". Requests carry "req_id"; replies carry
"type":"result" or "type":"error" with the same "req_id".
"""
import json
import os
import socket
import threading


def default_socket_path():
    """Socket path resolution order: $VW_SOCKET, system /run path, ~/run."""
    env = os.environ.get("VW_SOCKET")
    if env:
        return env
    candidates = ["/run/virtual-worker/agent.sock",
                  os.path.expanduser("~/run/agent.sock")]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[-1]


def send_msg(sock, obj):
    sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))


def recv_msg(sock, buf):
    """buf is a bytearray reused across calls. Returns dict or None on EOF."""
    while True:
        nl = buf.find(b"\n")
        if nl != -1:
            line = bytes(buf[:nl])
            del buf[: nl + 1]
            return json.loads(line.decode("utf-8"))
        chunk = sock.recv(65536)
        if not chunk:
            return None
        buf.extend(chunk)


def recv_msg_timeout(sock, buf, timeout):
    sock.settimeout(timeout)
    try:
        return recv_msg(sock, buf)
    finally:
        sock.settimeout(None)


class Requester:
    """One client connection to the backend socket."""

    def __init__(self, sock_path):
        self.sock_path = sock_path
        self.sock = None
        self.buf = bytearray()
        self.lock = threading.Lock()
        self._req_counter = 0

    def connect(self, role):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.sock_path)
        send_msg(self.sock, {"type": "hello", "role": role})

    def reconnect(self):
        self.close()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.sock_path)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.buf.clear()

    def next_req_id(self):
        with self.lock:
            self._req_counter += 1
            return "r%d" % self._req_counter

    def request(self, msg, timeout=60.0):
        """Send a request and wait for its result (blocking, exclusive use)."""
        with self.lock:
            if self.sock is None:
                self.reconnect()
            req_id = self.next_req_id()
            msg = dict(msg)
            msg["req_id"] = req_id
            send_msg(self.sock, msg)
            return self._wait_result(req_id, timeout)

    def _wait_result(self, req_id, timeout):
        self.sock.settimeout(timeout)
        try:
            while True:
                msg = recv_msg(self.sock, self.buf)
                if msg is None:
                    raise ConnectionError("backend closed connection")
                if msg.get("type") == "result" and msg.get("req_id") == req_id:
                    return msg
                if msg.get("type") == "error" and msg.get("req_id") == req_id:
                    raise RuntimeError(msg.get("error", "unknown error"))
                # push messages for other requests are ignored here
        finally:
            self.sock.settimeout(None)