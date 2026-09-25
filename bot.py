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
import time
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

DEFAULT_STATE = {
    "groups": [], "dest": None, "delay": 2.0, "dedup": True,
    "keywords_exact": [], "keywords_contains": [],
    "keywords_enabled": False, "keyword_allow_all": False, "keyword_users": [],
    "keyword_antispam_enabled": True, "keyword_antispam_seconds": 300.0,
    "keyword_antispam_whitelist": True,
}


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
    state["keyword_antispam_enabled"] = bool(state["keyword_antispam_enabled"])
    state["keyword_antispam_seconds"] = float(state["keyword_antispam_seconds"])
    state["keyword_antispam_whitelist"] = bool(state["keyword_antispam_whitelist"])
    legacy_keywords = state.pop("keywords", [])
    state["keywords_exact"] = [str(k).casefold() for k in state["keywords_exact"] if str(k).strip()]
    state["keywords_contains"] = [str(k).casefold() for k in state["keywords_contains"] if str(k).strip()]
    for keyword in legacy_keywords:
        keyword = str(keyword).strip().casefold()
        if keyword and keyword not in state["keywords_exact"]:
            state["keywords_exact"].append(keyword)
    state["keyword_users"] = [int(u) for u in state["keyword_users"]]
    return state


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


state = load_state()

# Every GIF send goes through this single queue, regardless of whether it came
# from the watcher, .gc load, or a keyword trigger. The worker owns the delay,
# so concurrent message handlers cannot bypass it.
send_queue: asyncio.Queue | None = None
send_queue_loop: asyncio.AbstractEventLoop | None = None
send_worker_task: asyncio.Task | None = None

SENT_DB = DATA_DIR / "sent.db"
db = sqlite3.connect(str(SENT_DB))
db.execute(
    "CREATE TABLE IF NOT EXISTS sent ("
    "file_unique_id TEXT, dest_file_id TEXT, dest_file_unique_id TEXT, "
    "dest INTEGER NOT NULL, sent_at TEXT NOT NULL)"
)
# Migrate the old schema. Older versions had a NOT NULL `file_id` column,
# which cannot accept the new INSERT shape. Those old file_id values were the
# source file IDs, not destination file IDs, so they are intentionally discarded.
columns_info = list(db.execute("PRAGMA table_info(sent)"))
columns = {row[1]: row for row in columns_info}
if "file_id" in columns:
    db.execute("DROP INDEX IF EXISTS sent_ix")
    db.execute("DROP TABLE IF EXISTS sent_new")
    db.execute(
        "CREATE TABLE sent_new ("
        "file_unique_id TEXT, dest_file_id TEXT, dest_file_unique_id TEXT, "
        "dest INTEGER NOT NULL, sent_at TEXT NOT NULL)"
    )
    db.execute("DROP TABLE sent")
    db.execute("ALTER TABLE sent_new RENAME TO sent")
    columns = {row[1]: row for row in db.execute("PRAGMA table_info(sent)")}
elif "file_unique_id" not in columns:
    db.execute("ALTER TABLE sent ADD COLUMN file_unique_id TEXT")
if "dest_file_id" not in columns:
    db.execute("ALTER TABLE sent ADD COLUMN dest_file_id TEXT")
if "dest_file_unique_id" not in columns:
    db.execute("ALTER TABLE sent ADD COLUMN dest_file_unique_id TEXT")
db.execute("DROP INDEX IF EXISTS sent_ix")
db.execute("DROP INDEX IF EXISTS sent_dest_unique_ix")
db.execute("CREATE INDEX sent_ix ON sent (file_unique_id, dest)")
db.execute("CREATE INDEX sent_dest_unique_ix ON sent (dest_file_unique_id, dest)")
db.execute(
    "CREATE TABLE IF NOT EXISTS keyword_antispam ("
    "file_unique_id TEXT PRIMARY KEY, expires_at REAL NOT NULL)"
)
db.commit()


def try_reserve_keyword_antispam(file_unique_id: str) -> tuple[bool, float]:
    if not state["keyword_antispam_enabled"]:
        return True, 0.0
    now = time.time()
    db.execute("DELETE FROM keyword_antispam WHERE expires_at <= ?", (now,))
    row = db.execute("SELECT expires_at FROM keyword_antispam WHERE file_unique_id = ?", (file_unique_id,)).fetchone()
    if row is not None and row[0] > now:
        db.commit()
        return False, row[0] - now
    db.execute(
        "INSERT OR REPLACE INTO keyword_antispam (file_unique_id, expires_at) VALUES (?, ?)",
        (file_unique_id, now + state["keyword_antispam_seconds"]),
    )
    db.commit()
    return True, 0.0


def release_keyword_antispam(file_unique_id: str) -> None:
    db.execute("DELETE FROM keyword_antispam WHERE file_unique_id = ?", (file_unique_id,))
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


async def clone_media(message: Message, *, force: bool = False):
    """Clone one GIF message to the destination chat.

    file_unique_id identifies the source GIF for deduplication.  The
    destination's file_id is cached separately so dedup-off can resend that
    already-uploaded copy instead of uploading the GIF again.
    """
    dest = state["dest"]
    if dest is None:
        log.warning("GIF from %s/%s skipped: no destination set (.gc dest <chat_id>)",
                    message.chat.id, message.id)
        return False

    anim = message.animation
    source_file_id = anim.file_id
    source_unique_id = anim.file_unique_id
    cached = db.execute(
        "SELECT dest_file_id FROM sent "
        "WHERE (file_unique_id = ? OR dest_file_unique_id = ?) "
        "AND dest = ? AND dest_file_id IS NOT NULL "
        "ORDER BY rowid DESC LIMIT 1",
        (source_unique_id, source_unique_id, dest),
    ).fetchone()

    if cached:
        if state["dedup"] and not force:
            log.info(
                "skip %s/%s: gif unique_id=%s already in dest",
                message.chat.id, message.id, source_unique_id,
            )
            return False

        log.info(
            "GIF %s/%s: %s — reusing destination file_id=%s "
            "for unique_id=%s (no re-upload)",
            message.chat.id, message.id, "keyword trigger" if force else "dedup off", cached[0], source_unique_id,
        )
        try:
            return await send_with_flood_retry("send_cached_media", dest, cached[0]) is not None
        except errors.RPCError:
            log.warning(
                "GIF %s/%s: cached destination file_id=%s failed; "
                "falling back to a fresh clone",
                message.chat.id, message.id, cached[0],
            )

    async def reupload_protected():
        # Protected/no-forward GIFs cannot be sent by file_id.
        with tempfile.TemporaryDirectory(dir=DATA_DIR, prefix="gif_") as tmpdir:
            path = await client.download_media(
                message,
                file_name=os.path.join(tmpdir, "gif.mp4"),
                in_memory=False,
            )
            if not path:
                log.warning("skip %s/%s: could not download protected GIF",
                            message.chat.id, message.id)
                return None
            return await send_with_flood_retry("send_animation", dest, path)

    sent_message = None
    if message.has_protected_content:
        log.info(
            "GIF %s/%s: re-uploading because has_protected_content=True",
            message.chat.id,
            message.id,
        )
        sent_message = await reupload_protected()
    else:
        try:
            sent_message = await send_with_flood_retry(
                "send_cached_media", dest, source_file_id
            )
        except errors.ChatForwardsRestricted:
            # Telegram can report a protected/no-forward restriction even when
            # has_protected_content is false/stale. Fall back to re-upload so
            # this RPC error never escapes the message handler.
            log.info(
                "GIF %s/%s: re-uploading because send_cached_media raised "
                "ChatForwardsRestricted (has_protected_content=False)",
                message.chat.id,
                message.id,
            )
            sent_message = await reupload_protected()

    if sent_message is None:
        return False

    dest_anim = getattr(sent_message, "animation", None)
    dest_file_id = getattr(dest_anim, "file_id", None)
    dest_unique_id = getattr(dest_anim, "file_unique_id", None)
    if not dest_file_id:
        log.error(
            "GIF %s/%s: send succeeded but destination animation has no file_id; "
            "not caching the result",
            message.chat.id, message.id,
        )
        return True

    db.execute(
        "INSERT INTO sent (file_unique_id, dest_file_id, dest_file_unique_id, dest, sent_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (source_unique_id, dest_file_id, dest_unique_id, dest),
    )
    db.commit()
    log.info(
        "GIF %s/%s: cached source unique_id=%s -> destination file_id=%s, "
        "destination unique_id=%s",
        message.chat.id, message.id, source_unique_id, dest_file_id, dest_unique_id,
    )
    return True


async def _send_worker(queue: asyncio.Queue) -> None:
    """Process every GIF send in one FIFO stream and enforce the global delay."""
    while True:
        message, force, result = await queue.get()
        try:
            ok = await clone_media(message, force=force)
            if not result.done():
                result.set_result(ok)
            if ok:
                await asyncio.sleep(state["delay"])
        except Exception as e:
            log.exception("queued GIF send failed")
            if not result.done():
                result.set_exception(e)
        finally:
            queue.task_done()


def _ensure_send_worker() -> None:
    global send_queue, send_queue_loop, send_worker_task
    loop = asyncio.get_running_loop()
    if send_worker_task is None or send_worker_task.done() or send_queue_loop is not loop:
        send_queue = asyncio.Queue()
        send_queue_loop = loop
        send_worker_task = asyncio.create_task(_send_worker(send_queue))


async def _queue_send(message: Message, *, force: bool = False) -> bool:
    _ensure_send_worker()
    loop = asyncio.get_running_loop()
    result = loop.create_future()
    assert send_queue is not None
    await send_queue.put((message, force, result))
    return await result


async def send_to_dest(message: Message) -> bool:
    return await _queue_send(message)


async def send_to_dest_with_force(message: Message) -> bool:
    return await _queue_send(message, force=True)


# ─────────────────────────── live listener ───────────────────────────



@client.on_message(filters.group)
async def on_group_message(app, message, *a):
    if message.chat.id not in state["groups"]:
        return
    if not message.from_user or message.from_user.is_self:
        return
    if message.animation is not None:
        log.info("GIF in watched group %s (msg %d) — cloning", message.chat.id, message.id)
        await send_to_dest(message)
        return
    if not state["keywords_enabled"] or not message.text:
        return
    text = message.text.strip().casefold()
    exact = next((k for k in state["keywords_exact"] if text == k), None)
    contains = next((k for k in state["keywords_contains"] if k in text), None)
    keyword = exact or contains
    if keyword is None:
        return
    if not state["keyword_allow_all"] and message.from_user.id not in state["keyword_users"]:
        log.info("keyword %r ignored for user %s: not whitelisted", keyword, message.from_user.id)
        return
    replied = getattr(message, "reply_to_message", None)
    if replied is None or replied.animation is None:
        return
    file_unique_id = replied.animation.file_unique_id
    is_whitelisted = message.from_user.id in state["keyword_users"]
    if is_whitelisted and not state["keyword_antispam_whitelist"]:
        log.info("keyword %r accepted for whitelisted user %s: anti-spam bypassed for GIF unique_id=%s", keyword, message.from_user.id, file_unique_id)
    else:
        reserved, remaining = try_reserve_keyword_antispam(file_unique_id)
        if not reserved:
            log.info("keyword %r ignored: GIF unique_id=%s is on anti-spam cooldown for %.1fs", keyword, file_unique_id, remaining)
            return
        log.info("keyword %r accepted for GIF unique_id=%s by user %s; cooldown=%ss", keyword, file_unique_id, message.from_user.id, state["keyword_antispam_seconds"])
    ok = await send_to_dest_with_force(replied)
    if not ok and not (is_whitelisted and not state["keyword_antispam_whitelist"]):
        release_keyword_antispam(file_unique_id)
        log.info("keyword GIF unique_id=%s send failed; anti-spam reservation released", file_unique_id)


# ─────────────────────────── commands ───────────────────────────

HELP_TEXT = (
    f"gifcenter - GIF watcher/loader\n"
    f"Commands are private-chat, owner-only. Prefix: {PREFIX!r}\n"
    f"\n"
    f"+-- WATCHING ------------------------------------------------+\n"
    f"| {PREFIX}gc add <group_id>         Start watching a group\n"
    f"| {PREFIX}gc remove <group_id>      Stop watching a group\n"
    f"| {PREFIX}gc list                   Show groups and settings\n"
    f"| {PREFIX}gc dest <chat_id>         Set destination chat\n"
    f"| {PREFIX}gc load <group_id> <n>    Queue last n messages (1-50000)\n"
    f"+--------------------------------------------------------------+\n"
    f"\n"
    f"+-- SENDING --------------------------------------------------+\n"
    f"| {PREFIX}gc delay <seconds>        Set global send delay\n"
    f"| {PREFIX}gc dedup [on|off]         Toggle destination dedup\n"
    f"| {PREFIX}gc kw ...                 Keyword-triggered GIF sends\n"
    f"| {PREFIX}gc session                Export the session string\n"
    f"+--------------------------------------------------------------+\n"
    f"\n"
    f"+-- KEYWORDS -------------------------------------------------+\n"
    f"| {PREFIX}gc kw add exact <text>      Add exact-match keyword\n"
    f"| {PREFIX}gc kw add contains <text>   Add contains-match keyword\n"
    f"| {PREFIX}gc kw remove <text>        Remove a keyword\n"
    f"| {PREFIX}gc kw list                 Show keywords and settings\n"
    f"| {PREFIX}gc kw on|off               Enable/disable triggers\n"
    f"| {PREFIX}gc kw all on|off           Allow everyone or whitelist\n"
    f"| {PREFIX}gc kw user add <id>        Add a whitelisted user\n"
    f"| {PREFIX}gc kw user remove <id>     Remove a whitelisted user\n"
    f"| {PREFIX}gc kw user list            List whitelisted users\n"
    f"| {PREFIX}gc kw antispam [on|off|<seconds>]\n"
    f"|                               Configure GIF cooldown\n"
    f"| {PREFIX}gc kw antispam whitelist on|off\n"
    f"|                               Apply cooldown to whitelist\n"
    f"| {PREFIX}gc kw help                Show keyword help\n"
    f"+--------------------------------------------------------------+\n"
    f"\n"
    f"+-- NOTES ----------------------------------------------------+\n"
    f"| All GIF sends use one FIFO queue, so delay applies globally.\n"
    f"| Keyword replies bypass dedup and may reuse cached destination files.\n"
    f"+--------------------------------------------------------------+"
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


async def cmd_kw(args, message) -> str:
    if len(args) < 2:
        return ("kw: " + ("on" if state["keywords_enabled"] else "off") +
                f"; everyone: {'on' if state['keyword_allow_all'] else 'off'}\n"
                f"exact: {', '.join(state['keywords_exact']) or '(none)'}\n"
                f"contains: {', '.join(state['keywords_contains']) or '(none)'}\n"
                f"users: {', '.join(map(str, state['keyword_users'])) or '(none)'}\n"
                "usage: .gc kw <add|list|remove|on|off|all|user> ...")
    sub = args[1].casefold()
    if sub == "help":
        return (".gc kw add exact <keyword>\n.gc kw add contains <keyword>\n"
                ".gc kw list\n.gc kw remove <keyword>\n.gc kw on|off\n"
                ".gc kw all on|off\n.gc kw antispam [on|off|<seconds>]\n.gc kw antispam whitelist on|off\n.gc kw user add <user_id>\n"
                ".gc kw user remove <user_id>\n.gc kw user list")
    if sub == "antispam":
        if len(args) >= 4 and args[2].casefold() == "whitelist":
            value = args[3].casefold()
            if value not in ("on", "off"):
                return "usage: .gc kw antispam whitelist <on|off>"
            state["keyword_antispam_whitelist"] = value == "on"
            save_state(state)
            return f"keyword anti-spam for whitelisted users: {value}"
        if len(args) < 3:
            return (f"keyword anti-spam: {'on' if state['keyword_antispam_enabled'] else 'off'}\n"
                    f"cooldown: {state['keyword_antispam_seconds']}s\n"
                    f"whitelisted users limited: {'on' if state['keyword_antispam_whitelist'] else 'off'}")
        value = args[2].casefold()
        if value in ("on", "off"):
            state["keyword_antispam_enabled"] = value == "on"
            save_state(state)
            return f"keyword anti-spam: {value}"
        try:
            seconds = float(args[2])
        except ValueError:
            return "cooldown must be on, off, or a number of seconds"
        if not 1 <= seconds <= 86400:
            return "cooldown must be 1..86400 seconds"
        state["keyword_antispam_seconds"] = seconds
        save_state(state)
        return f"keyword anti-spam cooldown: {seconds}s"
    if sub in ("on", "off"):
        state["keywords_enabled"] = sub == "on"
        save_state(state)
        return f"keywords: {sub}"
    if sub == "all":
        if len(args) < 3 or args[2].casefold() not in ("on", "off"):
            return "usage: .gc kw all <on|off>"
        state["keyword_allow_all"] = args[2].casefold() == "on"
        save_state(state)
        return f"keyword everyone: {args[2].casefold()}"
    if sub == "add":
        if len(args) < 4 or args[2].casefold() not in ("exact", "contains"):
            return "usage: .gc kw add <exact|contains> <keyword>"
        match_type = args[2].casefold()
        keyword = " ".join(args[3:]).strip().casefold()
        if not keyword:
            return "usage: .gc kw add <exact|contains> <keyword>"
        key = "keywords_exact" if match_type == "exact" else "keywords_contains"
        if keyword in state[key]:
            return "keyword already exists"
        state[key].append(keyword)
        save_state(state)
        return f"{match_type} keyword added: {keyword}"
    if sub == "remove":
        keyword = " ".join(args[2:]).strip().casefold()
        if not keyword:
            return "usage: .gc kw remove <keyword>"
        removed = False
        for key in ("keywords_exact", "keywords_contains"):
            if keyword in state[key]:
                state[key].remove(keyword)
                removed = True
        if not removed:
            return "keyword not found"
        save_state(state)
        return f"keyword removed: {keyword}"
    if sub == "list":
        lines = [
            f"keywords: {'on' if state['keywords_enabled'] else 'off'}",
            f"everyone: {'on' if state['keyword_allow_all'] else 'off'}",
            f"anti-spam: {'on' if state['keyword_antispam_enabled'] else 'off'}",
            f"cooldown: {state['keyword_antispam_seconds']}s",
            f"whitelisted users limited: {'on' if state['keyword_antispam_whitelist'] else 'off'}",
        ]
        lines.append("exact:")
        lines.extend(f"• {k}" for k in state["keywords_exact"] or ["(none)"])
        lines.append("contains:")
        lines.extend(f"• {k}" for k in state["keywords_contains"] or ["(none)"])
        return "\n".join(lines)
    if sub == "user":
        if len(args) < 3 or args[2].casefold() not in ("add", "remove", "list"):
            return "usage: .gc kw user <add|remove|list> [user_id]"
        action = args[2].casefold()
        if action == "list":
            return "\n".join(map(str, state["keyword_users"])) or "(none)"
        if len(args) < 4:
            return f"usage: .gc kw user {action} <user_id>"
        try:
            uid = int(args[3])
        except ValueError:
            return "user_id must be an integer"
        if action == "add":
            if uid not in state["keyword_users"]:
                state["keyword_users"].append(uid)
                save_state(state)
            return f"keyword user allowed: {uid}"
        if uid not in state["keyword_users"]:
            return "user not found"
        state["keyword_users"].remove(uid)
        save_state(state)
        return f"keyword user removed: {uid}"
    return "unknown kw subcommand; use .gc kw help"


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
        elif cmd == "kw":
            text = await cmd_kw(args, message)
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
