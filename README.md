# codex-chat

A dependency-free (Python 3 stdlib only) chat client for a local
**codex app-server** running on a unix control socket. One process = one
session; start several instances with different `--session` names to
emulate several users talking to the same codex app-server at once.

It automates the JSON-RPC dance (`initialize` → `thread/resume` /
`thread/start` → `turn/start` per message) so you just type text. Replies
stream back live as codex produces them.

```
$ ./codex-chat.py --session alice
[alice] thread 0199b2c1-... (new)
Type a message; Ctrl-C interrupts a running turn, Ctrl-D quits.
alice> hello! what can you do?
[14:44:49] you: hello! what can you do?
[14:44:50] I can help you with coding tasks...
[14:44:52] turn: completed | 3.5s | in=14107 cached=11008 out=6
alice>
```

Every exchange is timestamped (`HH:MM:SS`): the `you:` echo of your sent
message, the first line of codex's reply, and the per-turn statistics line
(status, duration, token usage). On quit a session summary is printed —
turn counts, average/total duration, and cumulative token totals for the
session, e.g. `... | tokens: in=28228 cached=24064 out=11`. Meta lines go
to stderr, reply text to stdout.

## Contents

- [Requirements](#requirements)
- [Running the server](#running-the-server)
  - [Authentication explained](#authentication-explained)
  - [codex-app-server-ctl.sh reference](#codex-app-server-ctl-sh-reference)
  - [Login, re-login, `--device-auth`, logout](#login-re-login---device-auth-logout)
- [Running the chat](#running-the-chat)
- [How it works](#how-it-works)
- [Multi-user example](#multi-user-example)
- [Troubleshooting](#troubleshooting)

## Requirements

- Python 3 (no pip packages)
- `codex` CLI on `PATH` (tested with codex-cli 0.155.1)
- a running codex app-server (see below)

## Running the server

Everything is driven by `codex-app-server-ctl.sh` in this directory. All
commands honor `CODEX_HOME` (default: `/tmp/codex-app-server`), which
decides where the server's socket, log, and credentials live:

```sh
./codex-app-server-ctl.sh start     # launch the app-server in the background
./codex-app-server-ctl.sh status    # process + auth status
./codex-app-server-ctl.sh stop      # stop it
```

The socket ends up at
`$CODEX_HOME/app-server-control/app-server-control.sock`, the log at
`$CODEX_HOME/app-server-control/app-server.log`.

### Authentication explained

**The server needs the login, not the clients.** The `codex app-server`
process is the one that calls the OpenAI API, so it needs `auth.json` in
*its* `CODEX_HOME`. `codex-chat.py` (and `codex-app-server-client.py` in
the cli-watcher-tester project) talk to a local, owner-only unix socket
and have no login step at all — N chat instances share the single
server's credentials.

If the server has no credentials, `initialize`, `thread/start` and
`turn/start` all still *succeed*, but every turn fails with
`401 Unauthorized` and the thread flips to `systemError` status — a fully
working chat UI that can never get a reply. `status` and `start` warn you
about this.

### codex-app-server-ctl.sh reference

| Command    | What it does |
| ---------- | ------------ |
| `start`    | Starts `codex app-server --listen unix://$SOCK` in the background (logs to the log file). Warns if not logged in. |
| `stop`     | Kills the running server for this `CODEX_HOME`. |
| `status`   | Shows the running process **and** the auth status, with the fix command when not logged in. |
| `login`    | Logs codex in for this `CODEX_HOME` (extra args pass through to `codex login`). Prints the resulting status and reminds you to restart a running server. |
| `logout`   | Removes `auth.json` from this `CODEX_HOME` only (your normal `~/.codex` login is a different file and stays untouched). |

### Login, re-login, `--device-auth`, logout

```sh
./codex-app-server-ctl.sh login
```

This runs `codex login` with the script's `CODEX_HOME` and starts the
normal **browser OAuth flow** with your ChatGPT account (a free account
works, but it is heavily rate-limited — see Troubleshooting). It writes
`$CODEX_HOME/auth.json`.

- **Already logged in?** Re-running `login` simply re-authenticates and
  replaces the stored credentials — there is no separate "re-login".
- **A server is already running?** The running process may have cached
  the missing/old credentials, so restart it afterwards:
  `./codex-app-server-ctl.sh stop && ./codex-app-server-ctl.sh start`
  (the script reminds you about this).
- **Headless / remote machine?** Pass `--device-auth` — instead of
  opening a browser locally, codex prints a short code (or URL) to open
  in a browser on any other machine:

  ```sh
  ./codex-app-server-ctl.sh login --device-auth
  ```

- **No API keys involved**: this setup uses ChatGPT sign-in only. (An
  OpenAI Platform API key would be a separate, paid, pay-as-you-go
  path — it does not use your free plan, and your free plan does not
  provide one.)

Logout:

```sh
./codex-app-server-ctl.sh logout    # only removes $CODEX_HOME/auth.json
```

## Running the chat

```sh
./codex-chat.py [SOCKET-PATH] [--session NAME] [--auto-approve] [--fresh] [-v]
```

| Argument          | Meaning |
| ----------------- | ------- |
| `SOCKET-PATH`     | Optional positional path to the control socket. Defaults to `$CODEX_HOME/app-server-control/app-server-control.sock` (or `/tmp/codex-app-server/...` when `CODEX_HOME` is unset) — same default as the ctl script. |
| `--session NAME`  | Session name: one word (`alice`) or quoted multiword (`--session "multi word"` / `--session 'another one'`). Default: `default`. Used as the prompt prefix and as the key in the session store. |
| `--auto-approve`  | Answer codex's approval requests (command execution, file changes, permissions) with *accept*. Default is to **prompt** `y/n` in the terminal while a turn is running. |
| `--fresh`         | Ignore a stored thread for this session and start a brand-new one. |
| `-v`, `--verbose` | Log raw notifications and server requests to stderr. |

Keys while running:

- **Enter** sends the typed line as a prompt (`turn/start`)
- **Ctrl-C** during a turn interrupts it (`turn/interrupt`) and waits for
  codex to wind down; at an empty prompt it quits
- **Ctrl-D** quits

## How it works

1. Opens the unix socket and performs the WebSocket upgrade handshake
   (the control socket speaks RFC6455 frames, not newline-delimited JSON).
2. `initialize` (clientInfo `codex_chat_cli`) → `initialized` notification.
3. Looks up `--session` in `$CODEX_HOME/chat-sessions.json`
   (`{"alice": {"threadId": ..., "createdAt": ...}}` — written under a
   file lock, so concurrent instances don't clobber each other):
   - found → `thread/resume` (conversation history continues across runs);
     if that fails → new `thread/start`
   - not found, or `--fresh` → `thread/start`, store the new thread id
4. Each line you type → `turn/start` with
   `input: [{"type":"text","text": ...}]`.
5. The reply streams to your terminal as `item/agentMessage/delta`
   notifications, prefixed with a `[HH:MM:SS]` timestamp on the first
   line; `turn/completed` ends the line and prints a statistics line to
   stderr, e.g.
   `[14:44:52] turn: completed | 3.5s | in=14107 cached=11008 out=6`
   (duration from the server's `durationMs`, tokens from
   `thread/tokenUsage/updated`; stdout stays clean stream text).
6. Approval requests from codex arrive as server→client JSON-RPC
   requests; `codex-chat` answers them (prompt or `--auto-approve`).

## Multi-user example

Terminal A:

```sh
export CODEX_HOME=/tmp/codex-app-server
./codex-chat.py --session alice
```

Terminal B:

```sh
export CODEX_HOME=/tmp/codex-app-server
./codex-chat.py --session bob
```

Both connect to the same server over their own sockets and drive their
own threads concurrently. A third instance with `--session alice` would
*resume* alice's conversation (only if run in another terminal — one
process per session is the intended use; concurrent writers to the same
thread are not).

## Troubleshooting

| Symptom | Cause / fix |
| ------- | ----------- |
| `error: socket not found: ...` | Server not running → `./codex-app-server-ctl.sh start`. |
| Turns fail with `401 Unauthorized`, `[turn: failed]` | Server's `CODEX_HOME` has no credentials → `./codex-app-server-ctl.sh login`, then `stop && start`. `status` shows the current auth state. |
| `429 ... exceeded retry limit`, `[turn: failed]`, then `[thread status: systemError - next prompt auto-recovers]` | You hit the (free-account) rate limit. The thread flips to `systemError`, but this is recoverable: `codex-chat` retries the next prompt automatically (re-resuming the thread if the server refuses `turn/start`). Wait for the limit window to reset (`codex` in-app usage view shows the windows). |
| Reply text appears, then hangs mid-turn | An approval may be waiting on stdin — answer the `[approval needed] ... [y/N]` prompt on stderr, or start with `--auto-approve`. |
| Want to see everything codex emits | Restart with `-v`; raw notifications are echoed to stderr. |
| Session went sideways, want a clean slate | `--fresh` starts a new thread for that session (the old one stays in codex's history). |
