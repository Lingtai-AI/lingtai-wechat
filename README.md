# lingtai-wechat

LingTai WeChat MCP server — iLink Bot API client with QR-code login, multi-modal messaging, and LICC inbox callback.

This is the canonical setup, configuration, and troubleshooting doc for the `lingtai-wechat` MCP. It is fetched by LingTai agents (or anyone else) when they need to install or configure this server.

> **MCP / LICC contract spec:** see the `lingtai-anatomy` skill, `reference/mcp-protocol.md`, for the canonical specification of the catalog → registry → activation chain, environment-variable injection, and the LICC v1 inbox callback protocol. The reference client implementation is `src/lingtai_wechat/licc.py` in this repo (vendored verbatim into all first-party LingTai MCP repos — copy it if you're writing your own).

## Tools

One omnibus MCP tool: `wechat(action=...)`. Actions: `send`, `check`, `read`, `reply`, `search`, `contacts`, `add_contact`, `remove_contact`. Supports text, images, voice, video, and files (auto-detected from `media_path` extension).

## Inbound messages (LICC)

Inbound WeChat messages flow into the host agent's inbox via the LingTai Inbox Callback Contract. Each new message is delivered as a LICC event with:

- `from` — contact alias (or raw `wxid_...` if no contact saved).
- `subject` — `"wechat message from <name>"`.
- `body` — a ~300 char preview, including bracketed media references like `[Image: /path]`, `[Voice: /path]`, `[Video: /path]`.
- `metadata.message_id` — for `reply`.
- `metadata.from_user_id` — raw `wxid_...`.
- `metadata.item_types` — list of message item types (text, image, voice, video, file).

Session expiry events are also delivered via LICC with `metadata.event_type: "session_expired"` so the agent knows to ask for re-login.

## Install

```bash
# Into the LingTai agent's venv (typically ~/.lingtai-tui/runtime/venv/)
pip install git+https://github.com/Lingtai-AI/lingtai-wechat.git
```

After install, `python -m lingtai_wechat` (or the `lingtai-wechat` script) starts the MCP server over stdio.

## QR-code login (one-time, before first use)

WeChat doesn't issue static bot tokens. Authenticate by scanning a QR code with the WeChat mobile app.

> ⚠ **The login QR is admin-only — do not share it.** Scanning it logs a WeChat account in as the bot's backend identity. If a friend or end user scans it instead of you, their account binds in place of yours and your credentials are replaced. This is **not** a contact / group / customer-service QR — those are shared from inside WeChat *after* login, and are how users actually chat with the bot.

### Recommended: browser bootstrap

```bash
lingtai-wechat-bootstrap .secrets/wechat
```

This is the preferred first-time setup. It:

1. Creates `.secrets/wechat/config.json` with defaults if missing.
2. Renders the QR as an SVG inside a self-contained HTML page (with an admin-only warning banner) and opens it in your default browser.
3. Polls iLink for confirmation, auto-refreshing the QR if it expires.
4. Writes `credentials.json` (chmod 600) and tells you to refresh the MCP.

The page auto-refreshes every 3s and prints a clear admin-only warning so the scanner cannot mistake it for a chat QR.

### Headless fallback: terminal QR

If the host has no browser or you're connecting over SSH without X-forwarding:

```bash
python -c "from lingtai_wechat.login import cli_login; cli_login('.secrets/wechat')"
```

This prints an ASCII QR directly to the terminal. Scan it with WeChat. On success, `credentials.json` is written into the directory you passed.

### After login

`credentials.json` contains `bot_token`, `user_id`, and `base_url`. Sessions expire periodically (typically ~30 days). When expired, you'll see a LICC event with `metadata.event_type: "session_expired"` — re-run the bootstrap.

## Configure

The server reads two files from the directory pointed at by `LINGTAI_WECHAT_CONFIG`:

- `config.json` — user-controlled options.
- `credentials.json` — written by `lingtai-wechat-bootstrap` (or the `cli_login` fallback). Don't edit by hand.

### config.json schema

```json
{
  "poll_interval": 1.0,
  "allowed_users": ["wxid_abc123"],
  "cdn_base_url": "https://..."
}
```

- `poll_interval` — seconds between iLink long-polls (default 1.0).
- `allowed_users` — optional allow-list of WeChat user IDs. When set, messages from other senders are silently ignored. Omit to accept any sender.
- `cdn_base_url` — usually omit; the default works.

### Activation in LingTai

```json
{
  "addons": ["wechat"],
  "mcp": {
    "wechat": {
      "type": "stdio",
      "command": "/path/to/your/python",
      "args": ["-m", "lingtai_wechat"],
      "env": {
        "LINGTAI_WECHAT_CONFIG": ".secrets/wechat/config.json"
      }
    }
  }
}
```

Then run `system(action="refresh")` from the agent. The MCP subprocess starts, the iLink long-poll begins, and the omnibus `wechat` tool becomes available.

## Troubleshooting

- **`LINGTAI_WECHAT_CONFIG env var not set`** — your `init.json` `mcp.wechat.env` entry is missing the `LINGTAI_WECHAT_CONFIG` key.
- **`WeChat config not found`** — the path resolves but no file exists. Relative paths are resolved against `LINGTAI_AGENT_DIR`.
- **`WeChat credentials not found`** — config exists but `credentials.json` doesn't. Run the QR-code login flow above.
- **`WeChat session expired`** event in agent inbox — re-run the QR-code login flow.
- **`All connection attempts failed`** in stderr — usually a stale `base_url` in credentials. Re-run login.
- **MCP server failed to start** — usually the `command` path in `init.json` doesn't have `lingtai_wechat` installed. Confirm with `<command> -m lingtai_wechat --help` from a shell.
- **Tool calls return `WeChat manager not initialized`** — server boot failed (missing config or expired creds). Check stderr.

### Multiple pollers after upgrading

iLink's `getUpdates` is a single-consumer long-poll: two processes holding the same `bot_token` race over inbound messages and each one gets a different subset. To prevent this, every `lingtai-wechat` poller takes an exclusive `fcntl.flock` on `~/.lingtai-wechat/locks/poller-<sha256(bot_token)[:16]>.lock` at startup. If a second poller starts for the same account, it refuses with a `PollerLockBusy` error that includes the holder's PID.

After upgrading to a `lingtai-wechat` version with the lock, the new process may refuse to start because a pre-upgrade poller from another LingTai project is still running. Diagnose and stop it:

```bash
# 1. Find every lingtai_wechat poller currently running.
pgrep -af 'python.*lingtai_wechat'

# 2. The PollerLockBusy error names the holder PID. Inspect it:
ps -p <pid> -o pid,command
lsof -p <pid> 2>/dev/null | grep cwd   # which working dir launched it

# 3. Stop the old poller (refresh that project's MCP after).
kill -TERM <pid>
```

Notes:

- Lockfiles in `~/.lingtai-wechat/locks/` are intentionally left on disk after process exit — the `flock` kernel state is what's authoritative, not the file's presence. A leftover lockfile from a dead process is harmless; a new poller will reacquire the lock cleanly.
- The lock is keyed on `sha256(bot_token)`, so two accounts produce different lockfiles and don't conflict. Multiple LingTai projects sharing one account is the case the lock prevents.
- On Windows, `fcntl` is unavailable; `lingtai-wechat` will refuse to start with `UnsupportedPlatformError` rather than silently re-introducing the race.

## License

MIT.
