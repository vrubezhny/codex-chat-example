#!/usr/bin/env python3
"""
Chat client for a local codex app-server (stdlib only - no dependencies).

Talks RFC6455 WebSocket frames over the app-server's unix control socket
(same transport as cli-watcher-tester's codex-app-server-client.py, framing
code included here so this script is standalone). It automates the session
lifecycle so you can just type text:

    initialize -> thread/resume (or thread/start) -> turn/start per line

Codex's reply streams back live via item/agentMessage/delta; each turn ends
with turn/completed. One process = one session; run several instances with
different --session names to emulate several users at once (each gets its
own thread on the same server).

Usage:
    codex-chat.py [SOCKET-PATH] [--session NAME] [--auto-approve] [--fresh] [-v]

    SOCKET-PATH   defaults to $CODEX_HOME/app-server-control/
                  app-server-control.sock (or /tmp/codex-app-server/... if
                  CODEX_HOME is unset) - same default as codex-chat's
                  codex-app-server-ctl.sh
    --session     session name, default "default"; one word or quoted
                  multiword ("multi word"), used as prompt prefix and as the
                  key in $CODEX_HOME/chat-sessions.json (sessionId -> thread)
    --auto-approve  answer codex's approval requests with "accept"
                  (default: prompt y/n in this terminal)
    --fresh       ignore a stored thread for this session, start a new one
    -v            log raw notifications/server requests to stderr

The chat itself needs no login - only the server process does (see README).
Ctrl-C interrupts a running turn, Ctrl-D quits.

Examples:
    codex-chat.py
    codex-chat.py --session alice
    codex-chat.py /run/codex.sock --session "multi word user" --auto-approve
"""
import argparse
import base64
import fcntl
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_CODEX_HOME = "/tmp/codex-app-server"


class ChatError(Exception):
    pass


def default_codex_home():
    return os.environ.get("CODEX_HOME", DEFAULT_CODEX_HOME)


def default_sock_path():
    return os.path.join(
        default_codex_home(), "app-server-control", "app-server-control.sock"
    )


def sessions_path():
    return os.path.join(default_codex_home(), "chat-sessions.json")


def ws_handshake(sock):
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        "GET / HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise ChatError("connection closed during handshake")
        resp += chunk
    header, _, rest = resp.partition(b"\r\n\r\n")
    header_text = header.decode(errors="replace")
    if "101" not in header_text.splitlines()[0]:
        raise ChatError(f"handshake failed: {header_text}")
    expected_accept = base64.b64encode(
        hashlib.sha1((key + GUID).encode()).digest()
    ).decode()
    if expected_accept not in header_text:
        raise ChatError(f"Sec-WebSocket-Accept mismatch, expected {expected_accept}")
    return rest


def send_text_frame(sock, text):
    payload = text.encode()
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    length = len(payload)
    header = bytearray()
    header.append(0x80 | 0x1)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    header += mask
    sock.sendall(bytes(header) + masked)


def ts():
    return time.strftime("%H:%M:%S")


def format_usage(usage):
    if not usage:
        return None
    parts = []
    if usage.get("inputTokens") is not None:
        parts.append(f"in={usage['inputTokens']}")
    if usage.get("cachedInputTokens"):
        parts.append(f"cached={usage['cachedInputTokens']}")
    if usage.get("outputTokens") is not None:
        parts.append(f"out={usage['outputTokens']}")
    return " ".join(parts) or None


def read_store():
    path = sessions_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+b") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        f.seek(0)
        data = f.read()
        fcntl.flock(f, fcntl.LOCK_UN)
    if not data:
        return {}
    try:
        parsed = json.loads(data.decode())
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        print(f"warning: ignoring corrupt {path}", file=sys.stderr)
        return {}


def store_thread(session, thread_id):
    path = sessions_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+b") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        data = f.read()
        try:
            store = json.loads(data.decode()) if data else {}
            if not isinstance(store, dict):
                store = {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            store = {}
        store[session] = {"threadId": thread_id, "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        f.seek(0)
        f.truncate()
        f.write(json.dumps(store, indent=2).encode())
        f.flush()
        fcntl.flock(f, fcntl.LOCK_UN)


def normalize_session(raw):
    name = raw.strip()
    if len(name) >= 2 and name[0] == name[-1] and name[0] in ("'", '"'):
        name = name[1:-1].strip()
    if not name:
        print("error: --session must not be empty", file=sys.stderr)
        sys.exit(2)
    if "\n" in name or "\r" in name:
        print("error: --session must not contain newlines", file=sys.stderr)
        sys.exit(2)
    return name


class CodexChat:
    APPROVAL_METHODS = {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "execCommandApproval",
        "applyPatchApproval",
    }

    def __init__(self, session, auto_approve, verbose):
        self.session = session
        self.auto_approve = auto_approve
        self.verbose = verbose
        self.thread_id = None
        self.turn_id = None
        self.turn_done = threading.Event()
        self.turn_done.set()
        self.turn_started_mono = None
        self.turn_usage = None
        self.turn_usage_counted = False
        self.tok_in = 0
        self.tok_cached = 0
        self.tok_out = 0
        self.turn_stats = {}
        self.turn_seconds = 0.0
        self.streaming = False
        self.thread_status = None
        self.sock = None
        self.send_lock = threading.Lock()
        self.id_lock = threading.Lock()
        self.next_id = 1
        self.pending = {}

    def connect(self, sock_path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(sock_path)
        leftover = ws_handshake(self.sock)
        self.receiver = threading.Thread(
            target=self._recv_loop, args=(leftover,), daemon=True
        )
        self.receiver.start()

    def send(self, obj):
        with self.send_lock:
            send_text_frame(self.sock, json.dumps(obj))

    def rpc(self, method, params, timeout=30):
        with self.id_lock:
            mid = self.next_id
            self.next_id += 1
        entry = {"event": threading.Event(), "result": None, "error": None}
        self.pending[mid] = entry
        self.send({"method": method, "id": mid, "params": params})
        if not entry["event"].wait(timeout):
            self.pending.pop(mid, None)
            raise ChatError(f"timeout waiting for {method} response")
        if entry["error"] is not None:
            err = entry["error"]
            message = err.get("message", "unknown error") if isinstance(err, dict) else str(err)
            code = err.get("code") if isinstance(err, dict) else None
            detail = f"{method} failed ({code}): {message}" if code is not None else f"{method} failed: {message}"
            raise ChatError(detail)
        return entry["result"]

    def _recv_loop(self, buf):
        sock = self.sock
        while True:
            try:
                data = buf + sock.recv(65536)
            except OSError:
                print("[connection closed by server]", file=sys.stderr)
                return
            if not data:
                print("[connection closed by server]", file=sys.stderr)
                return
            buf = b""
            while len(data) >= 2:
                b0, b1 = data[0], data[1]
                opcode = b0 & 0x0F
                masked = b1 & 0x80
                length = b1 & 0x7F
                idx = 2
                if length == 126:
                    if len(data) < idx + 2:
                        buf, data = data, b""
                        break
                    length = struct.unpack(">H", data[idx:idx + 2])[0]
                    idx += 2
                elif length == 127:
                    if len(data) < idx + 8:
                        buf, data = data, b""
                        break
                    length = struct.unpack(">Q", data[idx:idx + 8])[0]
                    idx += 8
                if masked:
                    if len(data) < idx + 4:
                        buf, data = data, b""
                        break
                    mask_key = data[idx:idx + 4]
                    idx += 4
                if len(data) < idx + length:
                    buf, data = data, b""
                    break
                payload = data[idx:idx + length]
                if masked:
                    payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
                data = data[idx + length:]
                if opcode == 0x1:
                    self._dispatch(payload)
                elif opcode == 0x8:
                    print("[server sent close frame]", file=sys.stderr)
                    return

    def _dispatch(self, payload):
        try:
            msg = json.loads(payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            print(f"< {payload.decode(errors='replace')}", file=sys.stderr)
            return
        if not isinstance(msg, dict):
            print(f"< {payload.decode(errors='replace')}", file=sys.stderr)
            return
        has_id = "id" in msg
        if has_id and ("result" in msg or "error" in msg):
            entry = self.pending.pop(msg["id"], None)
            if entry is None:
                if self.verbose:
                    print(f"< unmatched response: {json.dumps(msg)}", file=sys.stderr)
                return
            entry["result"] = msg.get("result")
            entry["error"] = msg.get("error")
            entry["event"].set()
        elif has_id and "method" in msg:
            self._handle_server_request(msg)
        elif "method" in msg:
            self._handle_notification(msg)
        elif self.verbose:
            print(f"< {json.dumps(msg)}", file=sys.stderr)

    def _accumulate_usage(self):
        if self.turn_usage and not self.turn_usage_counted:
            self.tok_in += self.turn_usage.get("inputTokens") or 0
            self.tok_cached += self.turn_usage.get("cachedInputTokens") or 0
            self.tok_out += self.turn_usage.get("outputTokens") or 0
            self.turn_usage_counted = True

    def _handle_notification(self, msg):
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "item/agentMessage/delta":
            delta = params.get("delta", "")
            if params.get("threadId") == self.thread_id or self.thread_id is None:
                if not self.streaming:
                    print(f"[{ts()}] ", end="", flush=True)
                    self.streaming = True
                print(delta, end="", flush=True)
            return
        if method == "turn/started":
            if params.get("threadId") == self.thread_id:
                turn = params.get("turn") or {}
                self.turn_id = turn.get("id") or self.turn_id
            if self.verbose:
                print(f"< {json.dumps(msg)}", file=sys.stderr)
            return
        if method == "turn/completed":
            if params.get("threadId") != self.thread_id:
                if self.verbose:
                    print(f"< {json.dumps(msg)}", file=sys.stderr)
                return
            turn = params.get("turn") or {}
            if self.streaming:
                print(flush=True)
                self.streaming = False
            status = turn.get("status", "unknown")
            stats = [f"[{ts()}] turn: {status}"]
            duration = turn.get("durationMs")
            if duration is None and self.turn_started_mono is not None:
                duration = int((time.monotonic() - self.turn_started_mono) * 1000)
            if duration is not None:
                stats.append(f"{duration / 1000:.1f}s")
                self.turn_seconds += duration / 1000
            self.turn_stats[status] = self.turn_stats.get(status, 0) + 1
            self._accumulate_usage()
            usage = format_usage(self.turn_usage)
            if usage:
                stats.append(usage)
            print(" | ".join(stats), file=sys.stderr)
            if status == "failed" and turn.get("error"):
                err = turn["error"]
                print(f"  {err.get('message', err)}", file=sys.stderr)
            self.turn_done.set()
            return
        if method == "thread/tokenUsage/updated":
            if params.get("threadId") == self.thread_id and (
                not params.get("turnId") or params.get("turnId") == self.turn_id
            ):
                usage = (params.get("tokenUsage") or {}).get("last")
                self.turn_usage = usage
                if self.turn_done.is_set():
                    self._accumulate_usage()
                    rendered = format_usage(usage)
                    if rendered:
                        print(f"[{ts()}] tokens: {rendered}", file=sys.stderr)
            elif self.verbose:
                print(f"< {json.dumps(msg)}", file=sys.stderr)
            return
        if method == "error":
            err = params.get("error") or {}
            message = err.get("message", "unknown error")
            retry = " (will retry)" if params.get("willRetry") else ""
            print(f"codex error: {message}{retry}", file=sys.stderr)
            return
        if method == "thread/status/changed":
            if params.get("threadId") == self.thread_id:
                status = params.get("status") or {}
                self.thread_status = status.get("type")
                if self.thread_status == "systemError":
                    print(
                        "[thread status: systemError - next prompt auto-recovers]",
                        file=sys.stderr,
                    )
                elif self.verbose:
                    print(f"< {json.dumps(msg)}", file=sys.stderr)
            elif self.verbose:
                print(f"< {json.dumps(msg)}", file=sys.stderr)
            return
        if self.verbose:
            print(f"< {json.dumps(msg)}", file=sys.stderr)

    def _handle_server_request(self, msg):
        method = msg.get("method")
        params = msg.get("params") or {}
        if self.verbose:
            print(f"< request {json.dumps(msg)}", file=sys.stderr)
        if method in self.APPROVAL_METHODS:
            decision = self._decide_approval(method, params)
            self.send({"id": msg["id"], "result": {"decision": decision}})
            return
        if method == "item/permissions/requestApproval":
            decision = self._decide_approval(method, params)
            requested = params.get("permissions") or {}
            granted = {}
            if decision == "accept":
                for key in ("network", "fileSystem"):
                    if requested.get(key) is not None:
                        granted[key] = requested[key]
            self.send(
                {
                    "id": msg["id"],
                    "result": {"permissions": granted, "scope": "turn"},
                }
            )
            return
        if method == "item/tool/requestUserInput":
            print(
                "[codex asks for user input - answering 'cancel' "
                "(not supported by codex-chat)]",
                file=sys.stderr,
            )
            self.send({"id": msg["id"], "result": {"response": "cancel"}})
            return
        print(f"[unhandled server request: {method} - replying method-not-found]", file=sys.stderr)
        self.send(
            {
                "id": msg["id"],
                "error": {"code": -32601, "message": "method not supported by codex-chat"},
            }
        )

    def _decide_approval(self, method, params):
        what = params.get("command") or params.get("reason") or method
        if self.auto_approve:
            print(f"[auto-approved] {what}", file=sys.stderr)
            return "accept"
        print(f"[approval needed] {what}", file=sys.stderr)
        try:
            answer = sys.stdin.readline()
        except (OSError, ValueError):
            return "decline"
        if not answer:
            return "decline"
        return "accept" if answer.strip().lower() in ("y", "yes") else "decline"

    def initialize(self):
        self.rpc(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_chat_cli",
                    "title": "Codex Chat",
                    "version": "0.1.0",
                }
            },
        )
        self.send({"method": "initialized"})

    def resolve_thread(self, fresh):
        stored = read_store().get(self.session)
        stored_id = stored.get("threadId") if isinstance(stored, dict) else None
        if stored_id and not fresh:
            try:
                self.rpc("thread/resume", {"threadId": stored_id})
                self.thread_id = stored_id
                return "resumed"
            except ChatError as e:
                print(f"[resume of {stored_id} failed: {e}; starting a new thread]", file=sys.stderr)
        result = self.rpc("thread/start", {})
        thread = (result or {}).get("thread") or {}
        self.thread_id = thread.get("id")
        if not self.thread_id:
            raise ChatError(f"thread/start returned no thread id: {result}")
        store_thread(self.session, self.thread_id)
        return "new"

    def start_turn(self, text, retried=False):
        self.turn_done.clear()
        self.streaming = False
        self.turn_usage = None
        self.turn_usage_counted = False
        self.turn_started_mono = time.monotonic()
        if not retried:
            print(f"[{ts()}] you: {text}", file=sys.stderr)
        try:
            result = self.rpc(
                "turn/start",
                {
                    "threadId": self.thread_id,
                    "input": [{"type": "text", "text": text}],
                },
                timeout=30,
            )
        except ChatError as e:
            self.turn_done.set()
            if not retried:
                print(f"[turn/start failed: {e}; re-resuming thread once]", file=sys.stderr)
                try:
                    self.rpc("thread/resume", {"threadId": self.thread_id})
                except ChatError as e2:
                    print(f"[re-resume failed: {e2}]", file=sys.stderr)
                    return False
                return self.start_turn(text, retried=True)
            print(f"[turn/start failed: {e}]", file=sys.stderr)
            return False
        turn = (result or {}).get("turn") or {}
        self.turn_id = turn.get("id") or self.turn_id
        try:
            while not self.turn_done.wait(0.2):
                pass
        except KeyboardInterrupt:
            print("\n[interrupting turn (Ctrl-C)]", file=sys.stderr)
            if self.turn_id:
                try:
                    self.rpc(
                        "turn/interrupt",
                        {"threadId": self.thread_id, "turnId": self.turn_id},
                        timeout=10,
                    )
                except ChatError as e:
                    print(f"[interrupt failed: {e}]", file=sys.stderr)
            try:
                while not self.turn_done.wait(0.2):
                    pass
            except KeyboardInterrupt:
                print("[giving up waiting for turn/completed]", file=sys.stderr)
                self.turn_done.set()
        return True

    def print_summary(self):
        total = sum(self.turn_stats.values())
        if not total:
            return
        counts = ", ".join(f"{n} {status}" for status, n in self.turn_stats.items())
        summary = (
            f"[{self.session}] session stats: {total} turn(s) ({counts}) | "
            f"avg {self.turn_seconds / total:.1f}s | total {self.turn_seconds:.1f}s"
        )
        tokens = []
        if self.tok_in:
            tokens.append(f"in={self.tok_in}")
        if self.tok_cached:
            tokens.append(f"cached={self.tok_cached}")
        if self.tok_out:
            tokens.append(f"out={self.tok_out}")
        if tokens:
            summary += " | tokens: " + " ".join(tokens)
        print(summary, file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        prog="codex-chat.py",
        description="Chat with codex over the app-server unix socket (one session per process).",
    )
    parser.add_argument(
        "socket_path",
        nargs="?",
        default=None,
        help="app-server control socket (default: $CODEX_HOME/app-server-control/app-server-control.sock)",
    )
    parser.add_argument(
        "--session",
        default="default",
        help='session name, one word or quoted multiword (default: "default")',
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="auto-accept codex approval requests instead of prompting y/n",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="start a new thread even if this session already has one",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="log raw notifications to stderr"
    )
    args = parser.parse_args()

    session = normalize_session(args.session)
    sock_path = args.socket_path or default_sock_path()
    if not os.path.exists(sock_path):
        print(f"error: socket not found: {sock_path}", file=sys.stderr)
        print(
            "(start one with codex-app-server-ctl.sh start)",
            file=sys.stderr,
        )
        sys.exit(1)

    chat = CodexChat(session, args.auto_approve, args.verbose)
    print(f"Connecting to {sock_path} ...", file=sys.stderr)
    try:
        chat.connect(sock_path)
        chat.initialize()
        state = chat.resolve_thread(args.fresh)
    except ChatError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[{session}] thread {chat.thread_id} ({state})", file=sys.stderr)
    print(
        "Type a message; Ctrl-C interrupts a running turn, Ctrl-D quits.",
        file=sys.stderr,
    )

    while True:
        try:
            line = input(f"{session}> ")
        except EOFError:
            print(file=sys.stderr)
            break
        except KeyboardInterrupt:
            print(file=sys.stderr)
            break
        line = line.strip()
        if not line:
            continue
        chat.start_turn(line)

    chat.print_summary()
    time.sleep(0.5)


if __name__ == "__main__":
    main()
