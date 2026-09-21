
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
assert reloaded == {"groups": [-1001, -1002], "dest": -2001, "delay": 1.5}, reloaded
ok("state save/load round-trip")

# load_state on missing file -> defaults
os.remove(bot.STATE_FILE)
d = bot.load_state()
assert d == {"groups": [], "dest": None, "delay": 2.0}, d
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
    file_id = "FID"

class Msg:
    def __init__(s, *, gid=-1001, from_id=5, self_=False, anim=True, protected=False, command=None, mid=42):
        s.chat = Chat(gid, "supergroup")
        s.from_user = U(from_id, self_)
        s.animation = Anim() if anim else None
        s.has_protected_content = protected
        s.command = command
        s.id = mid

client = bot.client
me = U(5, self_=True)
client.me = me

calls = []
async def cached(cid, fid, *a, **k):
    calls.append(("cached", cid, fid)); return types.SimpleNamespace(id=1)
async def animsend(cid, path, *a, **k):
    calls.append(("anim", cid, path)); return types.SimpleNamespace(id=2)
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
r = asyncio.run(bot.clone_media(Msg(protected=False)))
assert r is True and calls == [("cached", -2001, "FID")], (r, calls)
ok("clone_media unprotected -> send_cached_media(file_id)")

# ── clone protected -> download into tmpdir + reupload ──
calls.clear()
r = asyncio.run(bot.clone_media(Msg(protected=True)))
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
r = asyncio.run(bot.clone_media(Msg(protected=False)))
assert r is True and n["c"] == 3, (r, n)
ok("send_with_flood_retry: retries through FloodWait to success")

# ── flood retry exhausted -> False ──
async def alwaysflood(*a, **k):
    raise errors.FloodWait(0)
client.send_cached_media = alwaysflood
r = asyncio.run(bot.clone_media(Msg(protected=False)))
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
assert m3.replies == [] and m3.edits == ["no groups watched\ndest: not set\ndelay: 1.5s"], (m3.replies, m3.edits)
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

print(f"\nALL {len(passed)} PASSED")
