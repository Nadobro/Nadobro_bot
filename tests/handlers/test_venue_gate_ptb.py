"""The venue gate inside a REAL python-telegram-bot Application (Arcus P1).

The suite usually runs against the telegram stubs (tests/_stubs.py), which have
no Application, handler groups or ApplicationHandlerStop semantics. So this test
drives real PTB 22.x in a fresh interpreter: the same group layout main.py builds
(private-chat -3, language -2, gate -1, commands + venue handlers + catch-all in
group 0), real ``Update`` objects, and PTB's own ``process_update``. Network
calls (getMe, answerCallbackQuery, sendMessage, editMessageText) are replaced
by recorders; nothing leaves the process and no database is touched.

What it proves, on the real dispatcher:
* a stopped update never reaches ``handle_callback`` / ``handle_message`` / a
  Nado command handler, while a passed one does;
* ``venue:`` / ``ax:`` callbacks are claimed by the venue handler, not the
  catch-all;
* an update the gate passed at arrival that then queues behind a venue switch
  (per-user lock) never runs its Nado handler — main.py's ``serialized_for``
  re-check, on the real dispatcher;
* with the gate NOT registered (flag off, nobody on Arcus) the groups are just
  -3/-2/0, the venue is never read and routing is exactly today's.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UID = 990_023_201

_SCRIPT = r"""
import asyncio, json, sys
sys.path.insert(0, REPO)

from telegram import CallbackQuery, Message, Update, User
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ExtBot,
                          MessageHandler, TypeHandler, filters)

from src.nadobro.handlers import venue_gate, venue_handler

STATE = {"venue": "nado", "reads": 0}
events = []


async def fake_get_me(self, *a, **k):
    self._bot_user = User(id=424242, first_name="Nadobro", is_bot=True, username="nadobro_test_bot")
    return self._bot_user


async def fake_answer(self, text=None, show_alert=None, **kw):
    events.append(["answer", text, bool(show_alert)])
    return True


async def fake_reply(self, text, **kw):
    events.append(["reply", text])


async def fake_edit(self, text, **kw):
    events.append(["edit", text])


ExtBot.get_me = fake_get_me
CallbackQuery.answer = fake_answer
CallbackQuery.edit_message_text = fake_edit
Message.reply_text = fake_reply


async def read(uid):
    STATE["reads"] += 1
    return STATE["venue"]


# context=: Arcus P3b (03 §11.2/§20) — the gate passes the context to the renderer.
async def render(target, uid, *, query=None, message=None, context=None):
    events.append(["render", target, "query" if query is not None else "message"])


async def venue_view(query, uid):
    events.append(["venue_view"])


async def switch_to_arcus(query, context, uid):
    STATE["venue"] = "arcus"
    events.append(["switched"])


async def slow(update, context):
    await asyncio.sleep(0.2)
    events.append(["slow"])


venue_gate.read_active_venue = read
venue_handler.read_active_venue = read
venue_gate.render_arcus_target = render
venue_handler.render_arcus_target = render
venue_handler._venue_view = venue_view
venue_handler._switch_to_arcus = switch_to_arcus
venue_gate.get_or_create_user = lambda *a, **k: None


def rec(name):
    async def _handler(update, context):
        events.append([name])
    return _handler


def build(enabled):
    # main.setup_bot's layout, wrappers included (serialized_for).
    serialized = venue_gate.serialized_for(enabled)
    app = Application.builder().token("424242:TEST-TOKEN").concurrent_updates(True).build()
    app.add_handler(TypeHandler(Update, rec("private")), group=-3)
    app.add_handler(TypeHandler(Update, rec("language")), group=-2)
    for name in ("start", "stop_all", "desk", "brief"):
        app.add_handler(CommandHandler(name, serialized(rec("cmd_" + name))))
    app.add_handler(CommandHandler("slow", serialized(slow)))
    venue_gate.register_venue_handlers(app, enabled=enabled)
    app.add_handler(CallbackQueryHandler(serialized(rec("handle_callback"))))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, serialized(rec("handle_message"))))
    return app


def _from():
    return {"id": UID, "is_bot": False, "first_name": "U"}


def _chat():
    return {"id": UID, "type": "private"}


def cb_update(n, data, bot):
    return Update.de_json({"update_id": n, "callback_query": {
        "id": str(n), "from": _from(), "chat_instance": "ci", "data": data,
        "message": {"message_id": 5, "date": 1700000000, "chat": _chat(), "text": "card"},
    }}, bot)


def msg_update(n, text, bot, edited=False):
    body = {"message_id": n, "date": 1700000000, "chat": _chat(), "from": _from(), "text": text}
    if text.startswith("/"):
        body["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    if edited:
        body["edit_date"] = 1700000001
    return Update.de_json({"update_id": n, ("edited_message" if edited else "message"): body}, bot)


CASES = [
    ["nado", "cb", "strategy:start:grid:BTC"],
    ["nado", "cb", "ax:home"],
    ["nado", "msg", "long BTC 10x"],
    ["nado", "msg", "/desk"],
    ["nado", "edited", "long BTC 10x"],
    ["arcus", "cb", "strategy:start:grid:BTC"],
    ["arcus", "cb", "strategy:stop"],
    ["arcus", "cb", "copy:stop:3"],
    ["arcus", "cb", "nav:main"],
    ["arcus", "cb", "portfolio:view"],
    ["arcus", "cb", "ax:home"],
    ["arcus", "cb", "venue:view"],
    ["arcus", "msg", "long BTC 10x"],
    ["arcus", "msg", "/start"],
    ["arcus", "msg", "/stop_all"],
    ["arcus", "msg", "/desk"],
    ["arcus", "msg", "/brief"],
    ["arcus", "edited", "long BTC 10x"],
]


async def run(enabled):
    app = build(enabled)
    await app.initialize()
    out = {"groups": sorted(app.handlers), "cases": {}}
    for n, (venue, kind, payload) in enumerate(CASES, start=1):
        STATE["venue"] = venue
        events.clear()
        if kind == "cb":
            update = cb_update(n, payload, app.bot)
        else:
            update = msg_update(n, payload, app.bot, edited=(kind == "edited"))
        await app.process_update(update)
        for _ in range(5):
            await asyncio.sleep(0)
        out["cases"]["|".join((venue, kind, payload))] = [e[:] for e in events]
    out["reads"] = STATE["reads"]
    await app.shutdown()
    return out


async def race():
    # A slow Nado handler holds the user's lock; venue:set:arcus queues next;
    # then a Nado tap and free text arrive while the row still says 'nado'.
    app = build(True)
    await app.initialize()
    STATE["venue"] = "nado"
    events.clear()
    tasks = [asyncio.create_task(app.process_update(msg_update(900, "/slow", app.bot)))]
    await asyncio.sleep(0.03)
    tasks.append(asyncio.create_task(app.process_update(cb_update(901, "venue:set:arcus", app.bot))))
    await asyncio.sleep(0.03)
    tasks.append(asyncio.create_task(app.process_update(cb_update(902, "strategy:start:grid:BTC", app.bot))))
    tasks.append(asyncio.create_task(app.process_update(msg_update(903, "long BTC 10x", app.bot))))
    tasks.append(asyncio.create_task(app.process_update(cb_update(904, "strategy:stop", app.bot))))
    await asyncio.gather(*tasks)
    for _ in range(5):
        await asyncio.sleep(0)
    out = [e[:] for e in events]
    await app.shutdown()
    return out


async def main():
    enabled = await run(True)
    STATE["reads"] = 0
    disabled = await run(False)
    raced = await race()
    print("RESULT " + json.dumps({"enabled": enabled, "disabled": disabled, "race": raced}))


asyncio.run(main())
"""


def _run_real_ptb() -> dict:
    script = _SCRIPT.replace("REPO)", repr(str(REPO)) + ")", 1).replace("UID", str(UID))
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=str(REPO), capture_output=True, text=True, timeout=120,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert proc.returncode == 0 and lines, f"subprocess failed:\nSTDOUT:\n{proc.stdout[-3000:]}\nSTDERR:\n{proc.stderr[-3000:]}"
    return json.loads(lines[-1][len("RESULT "):])


_RESULT: dict | None = None


def _result() -> dict:
    global _RESULT
    if _RESULT is None:
        _RESULT = _run_real_ptb()
    return _RESULT


def _names(events):
    return [e[0] for e in events]


def test_groups_are_ordered_private_language_gate_handlers():
    assert _result()["enabled"]["groups"] == [-3, -2, -1, 0]
    assert _result()["disabled"]["groups"] == [-3, -2, 0]


def test_real_ptb_routing_with_the_gate_registered():
    from src.nadobro.handlers.venue_handler import (
        TEXT_ARCUS_BUTTON_ON_NADO,
        TEXT_ARCUS_FREE_TEXT_HINT,
        TEXT_DENIED_ON_ARCUS,
    )

    c = _result()["enabled"]["cases"]
    pre = ["private", "language"]
    # Nado view: exactly today's routing; ax:* denied.
    assert _names(c["nado|cb|strategy:start:grid:BTC"]) == pre + ["handle_callback"]
    assert c["nado|cb|ax:home"] == [["private"], ["language"], ["answer", TEXT_ARCUS_BUTTON_ON_NADO, True]]
    assert _names(c["nado|msg|long BTC 10x"]) == pre + ["handle_message"]
    assert _names(c["nado|msg|/desk"]) == pre + ["cmd_desk"]
    # Arcus P3b (03 D-13/§20): with the gate registered every edited message stops at
    # the gate (edits are only delivered once the gate widens allowed_updates).
    assert c["nado|edited|long BTC 10x"] == [["private"], ["language"]]
    # Arcus view.
    assert c["arcus|cb|strategy:start:grid:BTC"] == [["private"], ["language"], ["answer", TEXT_DENIED_ON_ARCUS, True]]
    assert _names(c["arcus|cb|strategy:stop"]) == pre + ["handle_callback"]  # NEVER_GATE reaches Nado
    assert _names(c["arcus|cb|copy:stop:3"]) == pre + ["handle_callback"]
    for key, target in (("arcus|cb|nav:main", "ax:home"), ("arcus|cb|portfolio:view", "ax:unavailable")):
        events = c[key]
        assert ["render", target, "query"] in events, events
        assert ["answer", None, False] in events  # bare ack
        assert "handle_callback" not in _names(events)
    events = c["arcus|cb|ax:home"]
    assert ["render", "ax:home", "query"] in events and "handle_callback" not in _names(events)
    events = c["arcus|cb|venue:view"]
    assert "venue_view" in _names(events) and "handle_callback" not in _names(events)
    assert c["arcus|msg|long BTC 10x"] == [["private"], ["language"], ["reply", TEXT_ARCUS_FREE_TEXT_HINT]]
    events = c["arcus|msg|/start"]
    assert ["render", "ax:home", "message"] in events and "cmd_start" not in _names(events)
    assert _names(c["arcus|msg|/stop_all"]) == pre + ["cmd_stop_all"]
    assert _names(c["arcus|msg|/desk"]) == pre + ["cmd_desk"]  # the desk list: entry to desk:stop
    assert c["arcus|msg|/brief"] == [["private"], ["language"], ["reply", TEXT_DENIED_ON_ARCUS]]
    assert c["arcus|edited|long BTC 10x"] == [["private"], ["language"]]


def test_real_ptb_routing_without_the_gate_is_todays():
    r = _result()["disabled"]
    c = r["cases"]
    pre = ["private", "language"]
    assert r["reads"] == 0  # nothing reads active_venue
    # Even a user whose row says 'arcus' routes exactly as today.
    assert _names(c["arcus|cb|strategy:start:grid:BTC"]) == pre + ["handle_callback"]
    assert _names(c["arcus|cb|ax:home"]) == pre + ["handle_callback"]  # "Unknown action", as today
    assert _names(c["arcus|cb|venue:view"]) == pre + ["handle_callback"]
    assert _names(c["arcus|msg|long BTC 10x"]) == pre + ["handle_message"]
    assert _names(c["arcus|msg|/desk"]) == pre + ["cmd_desk"]
    assert _names(c["arcus|msg|/brief"]) == pre + ["cmd_brief"]
    assert _names(c["arcus|msg|/start"]) == pre + ["cmd_start"]


def test_real_ptb_update_queued_behind_a_switch_never_runs_a_nado_handler():
    from src.nadobro.handlers.venue_handler import TEXT_ARCUS_FREE_TEXT_HINT, TEXT_DENIED_ON_ARCUS

    events = _result()["race"]
    names = _names(events)
    assert "slow" in names and "switched" in names
    assert names.index("slow") < names.index("switched")
    after = events[names.index("switched") + 1:]
    # Passed by the gate at arrival (row still 'nado'), then re-checked under the
    # lock after the switch: no Nado handler runs, the user gets the Arcus replies.
    assert "handle_message" not in _names(after)
    assert ["answer", TEXT_DENIED_ON_ARCUS, True] in after
    assert ["reply", TEXT_ARCUS_FREE_TEXT_HINT] in after
    # The only Nado handler that ran after the switch is the queued STOP.
    assert _names(after).count("handle_callback") == 1
