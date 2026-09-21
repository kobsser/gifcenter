# gifcenter

Telegram **userbot** (runs on your real account, not a bot) built on [kurigram](https://kurigram.icu) (a Pyrogram fork).
It watches configured group chats for **GIFs** and clones every one to a destination channel/group:

- unprotected GIF → sent by `file_id` (`send_cached_media`) — instant, no re-download
- protected (no-forward) GIF → downloaded and re-uploaded (`send_animation`) — no "forwarded" arrow

Managed with `.gc` commands from **your private chat** (prefix configurable).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env       # fill in API_ID + API_HASH
```

Get `API_ID` / `API_HASH` from <https://my.telegram.org> → *API development tools*.

Load env before running (or use your shell's loader):

```bash
set -a; . ./.env; set +a
```

## First login

Run once interactively:

```bash
.venv/bin/python bot.py
```

You'll be prompted for phone → code → (2FA password if set). This creates the session file
`gifcenter.session` in `DATA_DIR` (a SQLite DB). From then on the same session file (or a
`SESSION_STRING`) logs in silently.

For **portable deploys**, message yourself `.gc session` — the command message edits
itself to show a `SESSION_STRING`. Put it in `.env` on the deploy host and the `.session`
file is no longer needed.

> **Security:** `SESSION_STRING` (and the `.session` file) are the account itself.
> Anyone with either can read/send everything as you. Never commit them, never paste them in chat.

## Commands (private chat, owner-only, prefix `.`)

| Command | What it does |
|---|---|
| `.gc add <group_id>` | Watch a group. Must be a group/supergroup you're a member of. Rejects duplicates. |
| `.gc remove <group_id>` | Stop watching a group. |
| `.gc list` | Watched groups, destination, delay. |
| `.gc dest <chat_id>` | Set destination. Any chat you can write to: a **channel or forum channel** (you must be admin/owner) or a **group** (you must be a member). |
| `.gc load <group_id> <n>` | Backfill: clone GIFs from the last `n` messages (1–50000) of *any* group you're in — no need to have watched it. Sent in chronological order. |
| `.gc delay <seconds>` | Pause between clones, 0.1–3600 (default 2). |
| `.gc session` | Edit its own message to show your session string (portable login for deploys). |
| `.gc help` or bare `.gc` | Usage text. |

**Getting chat ids:** forward a message from the group/channel to a bot like [@userinfobot](https://t.me/userinfobot) —
the forwarded caption contains the chat id. Channel ids look like `-100123...`; supergroup ids like
`-100987...`; plain groups are small negative numbers.

### Examples

```
.gc dest -1001234567890
.gc add -1009876543210
.gc delay 2
.gc list
.gc load -1009876543210 200
```

## Behavior notes

- **GIF only.** A "GIF" here is Telegram's animated media (`message.animation`) — both real `.gif`
  uploads and mp4-based animated media. Plain videos/photos are ignored.
- **Flood control.** On `FloodWait` the bot sleeps `seconds + 1` and retries (up to 5 times per send).
  A global lock serializes the live listener and `.gc load`, so they can't interleave or double-flood.
- **Self messages ignored** in watched groups.
- **State** is one JSON file `state.json` in `DATA_DIR` (atomic writes). Reset by deleting it.
- **Offline check:** `python _smoke.py` (no network; mocks the client) exercises state
  round-trip, both clone paths, flood retry, and command dispatch. Run before a live change.

## Deploy — VPS (systemd)

```bash
sudo mkdir -p /opt/gifcenter && sudo cp -r . /opt/gifcenter/
cd /opt/gifcenter
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env
# first login:
set -a; . ./.env; set +a; .venv/bin/python bot.py   # phone → code → 2FA
```

`/etc/systemd/system/gifcenter.service`:

```ini
[Unit]
Description=gifcenter Telegram userbot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/gifcenter
EnvironmentFile=/opt/gifcenter/.env
ExecStart=/opt/gifcenter/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now gifcenter
journalctl -u gifcenter -f
```

> Note: systemd `EnvironmentFile` does **not** parse `export`/quotes the way shells do —
> keep `.env` as bare `KEY=value` lines.

## Deploy — Railway

1. Push this repo; Railway auto-detects the `Dockerfile`.
2. Add a **volume** mounted at `/data`, and set env vars `API_ID`, `API_HASH`,
   `DATA_DIR=/data` (plus optional `SESSION_STRING`, `HANDLER_PREFIX`, `LOG_LEVEL`).
3. First login: open the service's **console** (PTY) and run `python bot.py` there,
   or pre-generate `SESSION_STRING` on your machine and put it in env (no console login needed).

## Verification checklist (first run, live)

1. `.gc dest <your channel id>` → `destination: <title> (id)`
2. `.gc add <group id>` → `watching <title> (id)`; `.gc list` shows both
3. `.gc delay 2` → `delay: 2.0s`
4. Send a normal GIF in the watched group → it appears in the destination **without** a "forwarded from" line, within ~1s.
5. Send a GIF from a no-forward (protected) group → still cloned (slower: download+reupload), media intact, no forward arrow.
6. `.gc load <group id> 50` → `sent N gif(s) from last M message(s)`, chronological order in the destination.
7. Restart the process → same state (no re-adding needed).
