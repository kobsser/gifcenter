
"""Offline smoke test for gifcenter (no network, mocks the client). Run: python _smoke.py"""
import asyncio, os, sys, tempfile, types

tmp = tempfile.mkdtemp(prefix="gc_smoke_")
os.environ["DATA_DIR"] = tmp
os.environ["API_ID"] = "1"
os.environ["API_HASH"] = "x" * 32
os.environ["HANDLER_PREFIX"] = "."

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
from pyrogram import enums, errors  # noqa: E402

passed = []


def ok(name):
    passed.append(name)
    print("PASS", name)


# ── state round-trip ──
bot.state.update(groups=[-1001, -1002], dest=-2001, delay=1.5)
bot.save_state(bot.state)
reloaded = bot.load_state()
assert reloaded == {"groups": [-1001, -1002], "dest": -2001, "delay": 1.5, "dedup": True,
                       "keywords_exact": [], "keywords_contains": [], "keywords_enabled": False,
                       "keyword_allow_all": False, "keyword_users": [],
                       "keyword_antispam_enabled": True, "keyword_antispam_seconds": 300.0, "keyword_antispam_whitelist": True}, reloaded
ok("state save/load round-trip")

# load_state on missing file -> defaults
os.remove(bot.STATE_FILE)
d = bot.load_state()
assert d == {"groups": [], "dest": None, "delay": 2.0, "dedup": True,
             "keywords_exact": [], "keywords_contains": [], "keywords_enabled": False,
             "keyword_allow_all": False, "keyword_users": [],
             "keyword_antispam_enabled": True, "keyword_antispam_seconds": 300.0, "keyword_antispam_whitelist": True}, d
ok("load_state defaults on missing file")

# ── fakes ──
class U:
    def __init__(s, i, self_=False):
        s.id, s.is_self, s.username = i, self_, "me"

class Chat:
    def __init__(s, i, t="channel", title="Dest"):
        s.id, s.type, s.title, s.username = i, t, title, None

class Mem:
    def __init__(s, status): s.status = status

class Anim:
    def __init__(s, fid="FID", unique_id=None):
        s.file_id = fid
        s.file_unique_id = unique_id or fid

class Msg:
    def __init__(s, *, gid=-1001, from_id=5, self_=False, anim=True, protected=False, command=None, mid=42, fid="FID", unique_id=None):
        s.chat = Chat(gid, "supergroup")
        s.from_user = U(from_id, self_)
        s.animation = Anim(fid, unique_id) if anim else None
        s.has_protected_content = protected
        s.command = command
        s.id = mid
        s.text = None
        s.reply_to_message = None

client = bot.client
me = U(5, self_=True)
client.me = me

calls = []
async def cached(cid, fid, *a, **k):
    calls.append(("cached", cid, fid)); return types.SimpleNamespace(
        id=1, animation=Anim("DEST-" + fid, "DEST-UNIQUE"))
async def animsend(cid, path, *a, **k):
    calls.append(("anim", cid, path)); return types.SimpleNamespace(
        id=2, animation=Anim("DEST-UPLOAD", "DEST-UPLOAD-UNIQUE"))
async def dl(msg, file_name, in_memory):
    calls.append(("dl", file_name))
    p = os.path.join(os.path.dirname(file_name) or ".", "gif.mp4")
    open(p, "w").write("x")
    return p
client.send_cached_media = cached
client.send_animation = animsend
client.download_media = dl

# ── clone unprotected -> cached file_id ──
bot.state.update(dest=-2001)
calls.clear()
r = asyncio.run(bot.clone_media(Msg(protected=False, fid="F1")))
assert r is True and calls == [("cached", -2001, "F1")], (r, calls)
ok("clone_media unprotected -> send_cached_media(file_id)")

# ── Telegram can report forward restriction even when the message flag is false ──
async def restricted(cid, fid, *a, **k):
    calls.append(("cached-restricted", cid, fid))
    raise errors.ChatForwardsRestricted("protected chat")

client.send_cached_media = restricted
calls.clear()
r = asyncio.run(bot.clone_media(Msg(protected=False, fid="F1-restricted")))
assert r is True, r
assert calls[0] == ("cached-restricted", -2001, "F1-restricted")
assert calls[1][0] == "dl" and calls[2] == ("anim", -2001, calls[1][1]), calls
ok("clone_media forward restriction -> download + reupload fallback")

# ── clone protected -> download into tmpdir + reupload ──
calls.clear()
r = asyncio.run(bot.clone_media(Msg(protected=True, fid="F2")))
assert r is True, r
assert calls[0][0] == "dl" and os.path.dirname(calls[0][1]).startswith(tmp), calls
assert calls[1] == ("anim", -2001, calls[0][1]), calls
assert not os.path.exists(calls[1][2]), "temp file should be cleaned up"
ok("clone_media protected -> download(tmpdir/gif.mp4)+send_animation, cleaned")

# ── clone with no dest -> False ──
bot.state.update(dest=None)
assert asyncio.run(bot.clone_media(Msg())) is False
ok("clone_media no dest -> False, no send")

# ── flood retry then success ──
n = {"c": 0}
async def flaky(*a, **k):
    n["c"] += 1
    if n["c"] < 3:
        raise errors.FloodWait(0)
    calls.append(("flaky-ok",)); return types.SimpleNamespace(id=9)
client.send_cached_media = flaky
bot.state.update(dest=-2001)
asyncio.get_event_loop_policy()
calls.clear()
r = asyncio.run(bot.clone_media(Msg(protected=False, fid="F3")))
assert r is True and n["c"] == 3, (r, n)
ok("send_with_flood_retry: retries through FloodWait to success")

# ── flood retry exhausted -> False ──
async def alwaysflood(*a, **k):
    raise errors.FloodWait(0)
client.send_cached_media = alwaysflood
r = asyncio.run(bot.clone_media(Msg(protected=False, fid="F4")))
assert r is False, r
ok("send_with_flood_retry: gives up after 5 FloodWaits -> False")

# ── _parse_chat_id ──
p = bot._parse_chat_id
assert p("-100123") == -100123 and p("123") == 123 and p("@mychan") == "@mychan"
assert p("abc") is None and p("-12ab") is None
ok("_parse_chat_id int/username/reject")

# ── dispatch: owner guard + command routing ──
class DMsg:
    def __init__(s, from_id, command, mid=7):
        s.from_user, s.command, s.id = U(from_id), command, mid
        s.replies, s.edits = [], []
    async def reply_text(s, text):
        s.replies.append(text)
    async def edit_text(s, text):
        s.edits.append(text)

bot.state.update(groups=[], dest=None, delay=2.0)
m1 = DMsg(999, ["gc", "delay", "1.5"])
asyncio.run(bot.gc_command(None, m1))
assert m1.replies == [] and m1.edits == [], "non-owner must be ignored"
assert bot.state["delay"] == 2.0
ok("gc_command: non-owner ignored (no reply, no change)")

m2 = DMsg(5, ["gc", "delay", "1.5"])
asyncio.run(bot.gc_command(None, m2))
assert bot.state["delay"] == 1.5, bot.state
assert m2.replies == [] and m2.edits == ["delay: 1.5s"], (m2.replies, m2.edits)
ok("gc_command: owner .gc delay 1.5 updates state + confirms")
m3 = DMsg(5, ["gc", "list"])
asyncio.run(bot.gc_command(None, m3))
assert m3.replies == [] and m3.edits == ["no groups watched\ndest: not set\ndelay: 1.5s\ndedup: on"], (m3.replies, m3.edits)
ok("gc_command: owner .gc list renders state")

m4 = DMsg(5, ["gc", "nope"])
asyncio.run(bot.gc_command(None, m4))
assert m4.replies == [] and m4.edits and m4.edits[0].startswith("unknown subcommand"), (m4.replies, m4.edits)
ok("gc_command: unknown subcommand reported")

# ── .gc session awaits async export_session_string ──
async def fake_export():
    return "SS:1234"
client.export_session_string = fake_export
m5 = DMsg(5, ["gc", "session"])
asyncio.run(bot.gc_command(None, m5))
assert m5.replies == [] and m5.edits == ["your SESSION_STRING (put in .env on the deploy host, then delete this message):\n"
                                         "SS:1234"], (m5.replies, m5.edits)
ok("gc_command: .gc session awaits export_session_string (no coroutine concat)")

# ── .gc load limit raised to 50000 ──

# mocks for the n=50000 path (get_chat -> member check -> history)
async def fake_get_chat(gid):
    return types.SimpleNamespace(id=gid, type=enums.ChatType.GROUP, title="G", username=None)
async def fake_member(chat_id, uid):
    return types.SimpleNamespace(status=enums.ChatMemberStatus.MEMBER)
async def hist_mock(gid, limit=None):
    return
    yield
client.get_chat = fake_get_chat
client.get_chat_member = fake_member
client.get_chat_history = hist_mock
m6 = DMsg(5, ["gc", "load", "-1001", "50001"])
asyncio.run(bot.gc_command(None, m6))
assert m6.edits and m6.edits[0] == "number must be 1..50000", m6.edits
ok("gc_command: .gc load rejects n > 50000")
m7 = DMsg(5, ["gc", "load", "-1001", "50000"])
asyncio.run(bot.gc_command(None, m7))
assert m7.edits and m7.edits[0].startswith("sent"), m7.edits
ok("gc_command: .gc load accepts n = 50000")

# ── dedup: skip re-sends, record in sqlite ──
client.send_cached_media = cached
bot.state.update(dedup=True, dest=-2001)
calls.clear()
r1 = asyncio.run(bot.clone_media(Msg(fid="F9", unique_id="U9")))
assert r1 is True and calls == [("cached", -2001, "F9")], (r1, calls)
calls.clear()
r2 = asyncio.run(bot.clone_media(Msg(fid="F9-new", unique_id="U9")))
assert r2 is False and calls == [], (r2, calls)
ok("dedup on: duplicate gif skipped, no second send")
assert bot.db.execute("SELECT dest_file_id, dest_file_unique_id, dest FROM sent WHERE file_unique_id='U9'").fetchall() == [("DEST-F9", "DEST-UNIQUE", -2001)]
ok("dedup: sent gif recorded in sent.db")
bot.state.update(dest=-2002)
calls.clear()
r3 = asyncio.run(bot.clone_media(Msg(fid="F9", unique_id="U9")))
assert r3 is True and calls == [("cached", -2002, "F9")], (r3, calls)
ok("dedup: dest-scoped, new dest allowed")
# If the source GIF was previously sent to this destination by someone else,
# its source unique_id can equal the stored destination unique_id. Reuse it.
bot.db.execute(
    "INSERT INTO sent (file_unique_id, dest_file_id, dest_file_unique_id, dest, sent_at) VALUES (?, ?, ?, ?, datetime('now'))",
    ("OTHER-SOURCE", "DEST-KNOWN", "KNOWN-DEST-UNIQUE", -2003),
)
bot.db.commit()
bot.state.update(dest=-2003, dedup=True)
calls.clear()
r_known = asyncio.run(bot.clone_media(Msg(fid="NEW-SOURCE-ID", unique_id="KNOWN-DEST-UNIQUE")))
assert r_known is False and calls == [], (r_known, calls)
# A forced keyword resend reuses that destination file_id instead of uploading.
calls.clear()
r_known_force = asyncio.run(bot.clone_media(Msg(fid="NEW-SOURCE-ID", unique_id="KNOWN-DEST-UNIQUE"), force=True))
assert r_known_force is True and calls == [("cached", -2003, "DEST-KNOWN")], (r_known_force, calls)
ok("dedup: destination file_unique_id prevents re-upload and supports reuse")
bot.state.update(dest=-2002, dedup=False)
calls.clear()
r4 = asyncio.run(bot.clone_media(Msg(fid="F9", unique_id="U9")))
assert r4 is True and calls == [("cached", -2002, "DEST-F9")], (r4, calls)
assert bot.db.execute("SELECT COUNT(*) FROM sent WHERE file_unique_id='U9' AND dest=-2002").fetchone() == (1,)
ok("dedup off: re-sends + still records in db")
bot.state.update(dedup=True, dest=None)
m8 = DMsg(5, ["gc", "dedup", "off"])
asyncio.run(bot.gc_command(None, m8))
assert m8.edits == ["dedup: off"], m8.edits
assert bot.state["dedup"] is False and bot.load_state()["dedup"] is False, bot.state
ok("gc_command: owner .gc dedup off toggles + persists")
m9 = DMsg(5, ["gc", "dedup", "maybe"])
asyncio.run(bot.gc_command(None, m9))
assert m9.edits == ["dedup must be on or off"], m9.edits
assert bot.state["dedup"] is False
ok("gc_command: .gc dedup maybe rejected, state unchanged")
m10 = DMsg(5, ["gc", "dedup"])
asyncio.run(bot.gc_command(None, m10))
assert m10.edits and m10.edits[0].startswith("dedup: off. usage:"), m10.edits
ok("gc_command: .gc dedup no-arg shows current")
m11 = DMsg(999, ["gc", "dedup", "on"])
asyncio.run(bot.gc_command(None, m11))
assert m11.replies == [] and m11.edits == [] and bot.state["dedup"] is False, (m11.edits, bot.state)
ok("gc_command: non-owner .gc dedup ignored")

# ── keyword configuration ──
bot.state.update(keywords_exact=[], keywords_contains=[], keywords_enabled=False, keyword_allow_all=False, keyword_users=[])
for cmd, expected in [
    (["gc", "kw", "add", "exact", "again"], "exact keyword added: again"),
    (["gc", "kw", "add", "contains", "please again"], "contains keyword added: please again"),
    (["gc", "kw", "on"], "keywords: on"),
    (["gc", "kw", "all", "off"], "keyword everyone: off"),
    (["gc", "kw", "user", "add", "123"], "keyword user allowed: 123"),
]:
    m = DMsg(5, cmd)
    asyncio.run(bot.gc_command(None, m))
    assert m.edits == [expected], (cmd, m.edits)
assert bot.state["keywords_exact"] == ["again"] and bot.state["keywords_contains"] == ["please again"] and bot.state["keywords_enabled"]
assert bot.state["keyword_users"] == [123] and not bot.state["keyword_allow_all"]
ok("gc_command: keyword add/on/allowlist config")
m = DMsg(5, ["gc", "kw", "list"])
asyncio.run(bot.gc_command(None, m))
assert "• again" in m.edits[0] and "• please again" in m.edits[0] and "everyone: off" in m.edits[0]
ok("gc_command: keyword list")

# ── keyword reply trigger ──
client.send_cached_media = cached
bot.state.update(groups=[-1001], dest=-2001, keywords_exact=["again"], keywords_contains=["please again"], keywords_enabled=True,
                 keyword_allow_all=False, keyword_users=[123], dedup=True, delay=0.0)
source = Msg(gid=-1001, from_id=77, fid="KW-SOURCE", unique_id="KW-U", mid=900)
reply = Msg(gid=-1001, from_id=123, anim=False, mid=901)
reply.text = "again"
reply.reply_to_message = source
calls.clear()
asyncio.run(bot.on_group_message(None, reply))
assert calls == [("cached", -2001, "KW-SOURCE")], calls
ok("keyword reply: exact keyword re-sends GIF")

contains_reply = Msg(gid=-1001, from_id=123, anim=False, mid=903)
contains_reply.text = "please again now"
contains_reply.reply_to_message = source
calls.clear()
asyncio.run(bot.on_group_message(None, contains_reply))
assert calls == [], calls
ok("keyword anti-spam: same GIF blocked across keyword messages")

# Whitelisted users can optionally bypass the GIF cooldown. The bypass is
# specific to users in the whitelist; everyone else remains rate-limited.
bot.state["keyword_antispam_whitelist"] = False
whitelist_bypass_reply = Msg(gid=-1001, from_id=123, anim=False, mid=910)
whitelist_bypass_reply.text = "again"
whitelist_bypass_reply.reply_to_message = source
calls.clear()
asyncio.run(bot.on_group_message(None, whitelist_bypass_reply))
assert calls == [("cached", -2001, "DEST-KW-SOURCE")], calls
ok("keyword anti-spam: whitelisted user bypasses cooldown when disabled")
bot.state["keyword_antispam_whitelist"] = True

other_source = Msg(gid=-1001, from_id=77, fid="KW-SOURCE-2", unique_id="KW-U-2", mid=904)
contains_reply.reply_to_message = other_source
calls.clear()
asyncio.run(bot.on_group_message(None, contains_reply))
assert calls == [("cached", -2001, "KW-SOURCE-2")], calls
ok("keyword anti-spam: different GIF is allowed")

blocked = Msg(gid=-1001, from_id=456, anim=False, mid=902)
blocked.text = "again"
blocked.reply_to_message = source
calls.clear()
asyncio.run(bot.on_group_message(None, blocked))
assert calls == [], calls
ok("keyword reply: non-whitelisted user blocked")

bot.state["keyword_allow_all"] = True
blocked.reply_to_message = source
calls.clear()
asyncio.run(bot.on_group_message(None, blocked))
assert calls == [], calls
ok("keyword anti-spam: cooldown is shared across users")

all_source = Msg(gid=-1001, from_id=77, fid="KW-ALL", unique_id="KW-ALL-U", mid=909)
blocked.reply_to_message = all_source
calls.clear()
asyncio.run(bot.on_group_message(None, blocked))
assert calls == [("cached", -2001, "KW-ALL")], calls
ok("keyword reply: everyone toggle allows all users")

# Same GIF cooldown is independent of user/chat/message and expires normally.
bot.state["keyword_antispam_seconds"] = 1.0
source2 = Msg(gid=-1001, from_id=77, fid="KW-TEMP", unique_id="KW-TEMP-U", mid=905)
reply2 = Msg(gid=-1001, from_id=123, anim=False, mid=906)
reply2.text = "again"
reply2.reply_to_message = source2
calls.clear()
asyncio.run(bot.on_group_message(None, reply2))
assert calls == [("cached", -2001, "KW-TEMP")], calls
calls.clear()
asyncio.run(bot.on_group_message(None, reply2))
assert calls == [], calls
import time as _time
_time.sleep(1.05)
calls.clear()
asyncio.run(bot.on_group_message(None, reply2))
assert calls == [("cached", -2001, "DEST-KW-TEMP")], calls
ok("keyword anti-spam: cooldown expires by GIF unique_id")

# Turning anti-spam off permits repeated keyword sends. Re-enabling honors active reservations.
bot.state["keyword_antispam_seconds"] = 300.0
bot.state["keyword_antispam_enabled"] = False
calls.clear()
asyncio.run(bot.on_group_message(None, reply2))
assert calls == [("cached", -2001, "DEST-KW-TEMP")], calls
ok("keyword anti-spam: off allows repeated sends")
bot.state["keyword_antispam_enabled"] = True

# A failed queued send releases the reservation.
orig_send_force = bot.send_to_dest_with_force
async def fail_send(_message):
    return False
bot.send_to_dest_with_force = fail_send
failed_source = Msg(gid=-1001, from_id=77, fid="KW-FAIL", unique_id="KW-FAIL-U", mid=907)
failed_reply = Msg(gid=-1001, from_id=123, anim=False, mid=908)
failed_reply.text = "again"
failed_reply.reply_to_message = failed_source
asyncio.run(bot.on_group_message(None, failed_reply))
assert bot.db.execute("SELECT 1 FROM keyword_antispam WHERE file_unique_id='KW-FAIL-U'").fetchone() is None
bot.send_to_dest_with_force = orig_send_force
ok("keyword anti-spam: failed send releases reservation")

# Command configuration and persistence.
for cmd, expected in [
    (["gc", "kw", "antispam"], "keyword anti-spam: on\ncooldown: 300.0s\nwhitelisted users limited: on"),
    (["gc", "kw", "antispam", "whitelist", "off"], "keyword anti-spam for whitelisted users: off"),
    (["gc", "kw", "antispam"], "keyword anti-spam: on\ncooldown: 300.0s\nwhitelisted users limited: off"),
    (["gc", "kw", "antispam", "whitelist", "on"], "keyword anti-spam for whitelisted users: on"),
    (["gc", "kw", "antispam", "600"], "keyword anti-spam cooldown: 600.0s"),
    (["gc", "kw", "antispam", "off"], "keyword anti-spam: off"),
    (["gc", "kw", "antispam", "on"], "keyword anti-spam: on"),
]:
    m = DMsg(5, cmd)
    asyncio.run(bot.gc_command(None, m))
    assert m.edits == [expected], (cmd, m.edits)
assert bot.load_state()["keyword_antispam_seconds"] == 600.0
for cmd in (["gc", "kw", "antispam", "0"], ["gc", "kw", "antispam", "86401"], ["gc", "kw", "antispam", "nope"]):
    m = DMsg(5, cmd)
    asyncio.run(bot.gc_command(None, m))
    assert m.edits and m.edits[0].startswith(("cooldown must be 1..86400", "cooldown must be on, off, or a number")), (cmd, m.edits)
ok("gc_command: invalid anti-spam cooldown rejected")

# Reservation is synchronous/atomic within the single-process SQLite userbot.
bot.state["keyword_antispam_enabled"] = True
bot.state["keyword_antispam_seconds"] = 300.0
bot.db.execute("DELETE FROM keyword_antispam")
bot.db.commit()
async def reserve_many():
    return await asyncio.gather(*[asyncio.sleep(0, result=bot.try_reserve_keyword_antispam("KW-CONCURRENT")) for _ in range(8)])
results = asyncio.run(reserve_many())
assert sum(ok for ok, _ in results) == 1, results
ok("keyword anti-spam: concurrent reservations allow exactly one")

# Non-keyword send paths never create anti-spam reservations.
bot.db.execute("DELETE FROM keyword_antispam")
bot.db.commit()
client.send_cached_media = cached
asyncio.run(bot.clone_media(Msg(fid="NON-KW", unique_id="NON-KW-U"), force=True))
assert bot.db.execute("SELECT 1 FROM keyword_antispam WHERE file_unique_id='NON-KW-U'").fetchone() is None
ok("keyword anti-spam: watcher/load/forced clone path does not reserve")

# ── global send queue ──
# All entry points use the same queue, so concurrent sends are serialized and
# the configured delay is enforced between successful sends.
orig_clone_media = bot.clone_media
queue_events = []


async def fake_clone(message, *, force=False):
    queue_events.append(("send", message.id, force, asyncio.get_running_loop().time()))
    return True


async def queue_test():
    bot.clone_media = fake_clone
    bot.state["delay"] = 0.03
    m_a = Msg(mid=1001)
    m_b = Msg(mid=1002)
    m_c = Msg(mid=1003)
    await asyncio.gather(
        bot.send_to_dest(m_a),
        bot.send_to_dest_with_force(m_b),
        bot.send_to_dest(m_c),
    )


asyncio.run(queue_test())
bot.clone_media = orig_clone_media
assert [(e[1], e[2]) for e in queue_events] == [(1001, False), (1002, True), (1003, False)], queue_events
assert queue_events[2][3] - queue_events[1][3] >= 0.025, queue_events
assert queue_events[1][3] - queue_events[0][3] >= 0.025, queue_events
ok("global send queue: watcher/load/keyword sends share FIFO + delay")

# The smoke loop is closing, so stop its worker before the next asyncio.run.
if bot.send_worker_task is not None:
    bot.send_worker_task.cancel()
bot.send_worker_task = None

print(f"\nALL {len(passed)} PASSED")
