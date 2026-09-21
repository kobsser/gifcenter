"""gifcenter — Telegram userbot: watches configured groups for GIFs, clones them to a destination chat.

GIFs arrive as `message.animation` (Document with mime video/mp4 or real .gif).
Unprotected GIFs are sent by file_id (send_cached_media, instant, no re-download).
Protected (no-forward) GIFs are downloaded and re-uploaded (send_animation).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from pyrogram import Client, enums, errors, filters
from pyrogram.types import Message

# ─────────────────────────── config ───────────────────────────

DATA_DIR = Path(os.environ.get("DATA_DIR", ".")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
PREFIX = os.environ.get("HANDLER_PREFIX", ".") or "."
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gifcenter")

api_id = os.environ.get("API_ID", "")
api_hash = os.environ.get("API_HASH", "")
if not api_id or not api_hash:
    raise SystemExit("API_ID and API_HASH must be set (see .env.example)")

client = Client(
    name=str(DATA_DIR / "gifcenter"),
    api_id=int(api_id),
    api_hash=api_hash,
    session_string=os.environ.get("SESSION_STRING") or None,
    workdir=str(DATA_DIR),
    proxy=os.environ.get("PROXY_URL") or os.environ.get("HTTPS_PROXY") or None,
)

# ─────────────────────────── state ───────────────────────────

DEFAULT_STATE = {"groups": [], "dest": None, "delay": 2.0, "dedup": True}


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return dict(DEFAULT_STATE)
    for key, default in DEFAULT_STATE.items():
        state.setdefault(key, default)
    state["groups"] = [int(g) for g in state["groups"]]
    state["delay"] = float(state["delay"])
    return state


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


state = load_state()
send_lock = asyncio.Lock()  # serializes clones from listener + .gc load

SENT_DB = DATA_DIR / "sent.db"
db = sqlite3.connect(str(SENT_DB))
db.execute(
    "CREATE TABLE IF NOT EXISTS sent ("
    "file_id TEXT NOT NULL, dest INTEGER NOT NULL, sent_at TEXT NOT NULL)"
)
db.execute("CREATE INDEX IF NOT EXISTS sent_ix ON sent (file_id, dest)")
db.commit()

# ─────────────────────────── send core ───────────────────────────

FRIENDLY_ERRORS = {
    errors.ChatInvalid: "that chat id is not a valid chat",
    errors.ChatForbidden: "the account is not a member of that chat (or it is private)",
    errors.ChatWriteForbidden: "the account cannot send messages there",
    errors.ChatAdminRequired: "admin rights are required in that chat",
    errors.PeerIdInvalid: "invalid chat id",
    errors.UsernameInvalid: "invalid @username",
}


async def send_with_flood_retry(fn: str, *args, tries: int = 5):
    """Call `getattr(client, fn)(*args)`, sleeping through FloodWait."""
    for attempt in range(tries):
        try:
            return await getattr(client, fn)(*args)
        except errors.FloodWait as e:
            wait = e.seconds + 1
            log.warning("FloodWait %ss on %s — sleeping %ss (try %d/%d)",
                        e.seconds, fn, wait, attempt + 1, tries)
            await asyncio.sleep(wait)
    log.error("gave up on %s after %d FloodWaits", fn, tries)
    return None


async def clone_media(message: Message):
    """Clone one GIF message to the destination chat. True on success, False on skip/failure."""
    dest = state["dest"]
    if dest is None:
        log.warning("GIF from %s/%s skipped: no destination set (.gc dest <chat_id>)",
                    message.chat.id, message.id)
        return False

    anim = message.animation
    file_id = anim.file_id
    if state["dedup"] and db.execute(
            "SELECT 1 FROM sent WHERE file_id = ? AND dest = ?",
            (file_id, dest)).fetchone():
        log.info("skip %s/%s: gif %s already in dest", message.chat.id, message.id, file_id)
        return False

    if not message.has_protected_content:
        ok = await send_with_flood_retry("send_cached_media", dest, file_id) is not None
    else:
        # Protected: file_id cannot be reused → download + reupload.
        with tempfile.TemporaryDirectory(dir=DATA_DIR, prefix="gif_") as tmpdir:
            path = await client.download_media(message, file_name=os.path.join(tmpdir, "gif.mp4"), in_memory=False)
            ok = await send_with_flood_retry("send_animation", dest, path) is not None

    if ok:
        db.execute("INSERT INTO sent (file_id, dest, sent_at) VALUES (?, ?, datetime('now'))",
                   (file_id, dest))
        db.commit()
    return ok


async def send_to_dest(message: Message) -> bool:
    async with send_lock:
        ok = await clone_media(message)
        if ok:
            await asyncio.sleep(state["delay"])
        return ok


# ─────────────────────────── live listener ───────────────────────────



@client.on_message(filters.group)
async def on_group_message(app, message, *a):
    if message.chat.id not in state["groups"]:
        return
    if not message.from_user or message.from_user.is_self:
        return
    if message.animation is None:
        return
    log.info("GIF in watched group %s (msg %d) — cloning", message.chat.id, message.id)
    await send_to_dest(message)


# ─────────────────────────── commands ───────────────────────────

HELP_TEXT = (
    f"gifcenter commands (prefix {PREFIX!r}, private, owner only):\n"
    f"{PREFIX}gc add <group_id>      watch a group for GIFs\n"
    f"{PREFIX}gc remove <group_id>   stop watching a group\n"
    f"{PREFIX}gc list                watched groups + destination + delay\n"
    f"{PREFIX}gc dest <chat_id>      where GIFs get cloned (channel or group)\n"
    f"{PREFIX}gc load <group_id> <n> clone last n messages' GIFs (1–50000)\n"
    f"{PREFIX}gc delay <seconds>     pause between clones (0.1–3600, default 2)\n"
    f"{PREFIX}gc dedup [on|off]      skip GIFs already sent to the destination (on/off)\n"
    f"{PREFIX}gc session             export your session string (for porting)\n"
    f"{PREFIX}gc help                this text"
)


def _chat_title(chat) -> str:
    return chat.title or (f"@{chat.username}" if chat.username else str(chat.id))


async def _member_ok(chat_id: int, statuses) -> tuple[bool, str]:
    try:
        member = await client.get_chat_member(chat_id, client.me.id)
    except (errors.ChatForbidden, errors.PeerIdInvalid) as e:
        return False, FRIENDLY_ERRORS.get(type(e), str(e))
    return member.status in statuses, f"this account is not a member of that chat (status: {member.status.value})"


async def cmd_add(args, message) -> str:
    if len(args) < 2:
        return "usage: .gc add <group_id>"
    gid = _parse_chat_id(args[1])
    if gid is None:
        return "invalid chat id"
    if gid in state["groups"]:
        return f"already watching {gid}"
    try:
        chat = await client.get_chat(gid)
    except errors.ChatForbidden:
        return "that chat is not valid, not joined, or its id is wrong"
    except (errors.PeerIdInvalid, errors.UsernameInvalid) as e:
        return FRIENDLY_ERRORS.get(type(e), "could not resolve that chat")
    if chat.type not in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
        return f"that is a {chat.type.value}, not a group"
    ok, why = await _member_ok(gid, (
        enums.ChatMemberStatus.OWNER,
        enums.ChatMemberStatus.ADMINISTRATOR,
        enums.ChatMemberStatus.MEMBER,
        enums.ChatMemberStatus.RESTRICTED,
    ))
    if not ok:
        return why
    state["groups"].append(gid)
    save_state(state)
    log.info("now watching %s (%s)", chat.id, _chat_title(chat))
    return f"watching {_chat_title(chat)} ({chat.id})"


async def cmd_remove(args, message) -> str:
    if len(args) < 2:
        return "usage: .gc remove <group_id>"
    gid = _parse_chat_id(args[1])
    if gid is None:
        return "invalid chat id"
    if gid not in state["groups"]:
        return "not watching that group"
    state["groups"].remove(gid)
    save_state(state)
    return f"no longer watching {gid}"


async def cmd_list(args, message) -> str:
    lines = []
    if state["groups"]:
        for gid in state["groups"]:
            try:
                title = _chat_title(await client.get_chat(gid))
            except Exception:
                title = "(unreachable)"
            lines.append(f"• {title} ({gid})")
    else:
        lines.append("no groups watched")
    if state["dest"] is not None:
        try:
            dtitle = _chat_title(await client.get_chat(state["dest"]))
        except Exception:
            dtitle = "(unreachable)"
        lines.append(f"dest: {dtitle} ({state['dest']})")
    else:
        lines.append("dest: not set")
    lines.append(f"delay: {state['delay']}s")
    lines.append(f"dedup: {'on' if state['dedup'] else 'off'}")
    return "\n".join(lines)


async def cmd_dest(args, message) -> str:
    if len(args) < 2:
        return "usage: .gc dest <chat_id>"
    cid = _parse_chat_id(args[1])
    if cid is None:
        return "invalid chat id"
    try:
        chat = await client.get_chat(cid)
    except errors.ChatForbidden:
        return "that chat is not valid, not joined, or its id is wrong"
    except (errors.PeerIdInvalid, errors.UsernameInvalid) as e:
        return FRIENDLY_ERRORS.get(type(e), "could not resolve that chat")
    if chat.type in (enums.ChatType.CHANNEL, enums.ChatType.FORUM):
        ok, why = await _member_ok(cid, (enums.ChatMemberStatus.OWNER, enums.ChatMemberStatus.ADMINISTRATOR))
        if not ok:
            return "need to be an admin (can post) in a channel destination"
    else:
        ok, why = await _member_ok(cid, (
            enums.ChatMemberStatus.OWNER,
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.MEMBER,
            enums.ChatMemberStatus.RESTRICTED,
        ))
        if not ok:
            return why
    state["dest"] = cid
    save_state(state)
    log.info("destination set to %s", _chat_title(chat))
    return f"destination: {_chat_title(chat)} ({chat.id})"


async def cmd_load(args, message) -> str:
    if len(args) < 3:
        return "usage: .gc load <group_id> <number>"
    gid = _parse_chat_id(args[1])
    if gid is None:
        return "invalid chat id"
    try:
        n = int(args[2])
    except ValueError:
        return "number must be an integer"
    if not 1 <= n <= 50_000:
        return "number must be 1..50000"
    try:
        chat = await client.get_chat(gid)
    except errors.ChatForbidden:
        return "that chat is not valid, not joined, or its id is wrong"
    except (errors.PeerIdInvalid, errors.UsernameInvalid) as e:
        return FRIENDLY_ERRORS.get(type(e), "could not resolve that chat")
    if chat.type not in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
        return f"that is a {chat.type.value}, not a group"
    ok, why = await _member_ok(gid, (
        enums.ChatMemberStatus.OWNER,
        enums.ChatMemberStatus.ADMINISTRATOR,
        enums.ChatMemberStatus.MEMBER,
        enums.ChatMemberStatus.RESTRICTED,
    ))
    if not ok:
        return why

    # newest-first fetch (100/chunk); reverse whole list for chronological replay
    messages = [m async for m in client.get_chat_history(gid, limit=n)]
    messages.reverse()
    gifs = [m for m in messages if m.animation is not None]
    sent = 0
    for m in gifs:
        if await send_to_dest(m):
            sent += 1
    return f"sent {sent} gif(s) from last {len(messages)} message(s)"


async def cmd_delay(args, message) -> str:
    if len(args) < 2:
        return f"delay is {state['delay']}s. usage: .gc delay <seconds>"
    try:
        d = float(args[1])
    except ValueError:
        return "seconds must be a number"
    if not 0.1 <= d <= 3600:
        return "delay must be 0.1..3600 seconds"
    state["delay"] = d
    save_state(state)
    return f"delay: {d}s"


async def cmd_dedup(args, message) -> str:
    if len(args) < 2:
        return f"dedup: {'on' if state['dedup'] else 'off'}. usage: .gc dedup <on|off>"
    v = args[1].lower()
    if v not in ("on", "off"):
        return "dedup must be on or off"
    state["dedup"] = v == "on"
    save_state(state)
    return f"dedup: {v}"


async def cmd_session(args, message) -> str:
    try:
        s = await client.export_session_string()
    except Exception:
        return "no active session yet — log in first"
    log.info("session string exported to private chat")
    return ("your SESSION_STRING (put in .env on the deploy host, then delete this message):\n"
            + s)


async def gc_command(app, message: Message, *a):
    if not message.from_user or message.from_user.id != client.me.id:
        return  # owner-only
    args = (message.command or [])[1:]  # message.command[0] is "gc"
    cmd = args[0].lower() if args else "help"
    try:
        if cmd == "help" or not args:
            text = HELP_TEXT
        elif cmd == "add":
            text = await cmd_add(args, message)
        elif cmd == "remove":
            text = await cmd_remove(args, message)
        elif cmd == "list":
            text = await cmd_list(args, message)
        elif cmd == "dest":
            text = await cmd_dest(args, message)
        elif cmd == "load":
            text = await cmd_load(args, message)
        elif cmd == "delay":
            text = await cmd_delay(args, message)
        elif cmd == "dedup":
            text = await cmd_dedup(args, message)
        elif cmd == "session":
            text = await cmd_session(args, message)
        else:
            text = f"unknown subcommand {cmd!r}\n\n{HELP_TEXT}"
    except errors.FloodWait as e:
        text = f"floodwait: try again in {e.seconds}s"
    except Exception as e:
        log.exception("gc command failed")
        text = f"error: {type(e).__name__}: {e}"
    try:
        await message.edit_text(text)
    except errors.MessageNotModified:
        pass  # identical result already in the message


@client.on_message(filters.private & filters.command("gc", prefixes=PREFIX))
async def gc_handler(app, message, *a):
    await gc_command(app, message, *a)


# ─────────────────────────── helpers ───────────────────────────

def _parse_chat_id(token: str):
    token = token.strip()
    if token.isdigit():
        return int(token)
    if token.startswith("-100") and token[4:].isdigit():
        return int(token)
    if token.startswith("@") and token[1:].isalnum() and len(token) >= 5:
        return token
    return None


# ─────────────────────────── main ───────────────────────────

if __name__ == "__main__":
    print(f"gifcenter starting (data dir: {DATA_DIR}, prefix: {PREFIX!r})", flush=True)
    client.run()  # start (interactive login on first run) + idle + stop
