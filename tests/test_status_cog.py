import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import fake_interaction, minutes_ago

from fuzzworkbot import update_check, watchdog
from fuzzworkbot.cogs import status
from fuzzworkbot.db import STATUS_ACTIVE, GiveawayEntry, QueuedGiveaway, UserLookup, now_utc


def _fields(embed):
    return {f.name: f.value for f in embed.fields}


@pytest.fixture
def update_state(tmp_path, monkeypatch):
    path = tmp_path / "update-check.json"
    monkeypatch.setattr(update_check, "STATE_FILE", path)
    return path


@pytest.fixture
def cog(session_factory, monkeypatch):
    monkeypatch.setattr(status, "next_timer_run", AsyncMock(return_value=datetime.datetime(2026, 11, 16, 20, 18, 33)))
    giveaways = SimpleNamespace(last_poller_error=None)
    bot = SimpleNamespace(session_factory=session_factory, latency=0.052, get_cog=lambda name: giveaways)
    return status.Status(bot)


async def _run(cog):
    inter = fake_interaction()
    await cog.botstatus.callback(cog, inter)
    inter.response.defer.assert_awaited_once_with(ephemeral=True)
    return inter.edit_original_response.call_args.kwargs["embed"]


async def test_botstatus_full_picture(cog, session_factory, make_prize, make_giveaway, update_state):
    plex = make_prize(name="PLEX", codes=list("ABCDEF"))
    make_prize(name="Nightmare", codes=["N1", "N2"])  # 2 available: low
    tight = make_prize(name="Tight", codes=["T1"])
    make_giveaway(tight, winner_count=1, starts_at=minutes_ago(-600))  # scheduled, reserves its only code
    make_prize(name="Empty", codes=[])
    make_prize(name="Old", codes=["X"], active=False)  # inactive: never "low stock"
    running = make_giveaway(plex, status=STATUS_ACTIVE, message_id=1, ends_at=minutes_ago(-60), entry_mode="button")
    with session_factory.begin() as session:
        session.add(GiveawayEntry(giveaway_id=running, discord_id=10, entered_at=now_utc()))
        session.add(QueuedGiveaway(prize_id=plex, winner_count=2, added_at=now_utc()))
        session.add(UserLookup(discordid=1, eveid=2))
    watchdog.write_heartbeat()
    update_state.write_text(json.dumps({"Python (pyapi)|httpx2|2.16.2": "2026-11-16", "Python (pyapi)|httpcore2|2.16.2": "2026-11-16", "uv|uv|0.13": "2026-11-15"}))

    embed = await _run(cog)
    f = _fields(embed)

    assert embed.colour.value == 0x2ECC71  # green: heartbeat fresh, no alert
    assert "gateway 52 ms" in f["Bot"]
    assert f["Poller"].startswith("Last good run ") and "No errors since start" in f["Poller"] and "Watchdog: OK" in f["Poller"]
    g = f["Giveaways (1 running, 1 scheduled)"]
    assert f"#{running} PLEX, ends <t:" in g and "(1 entrants)" in g and "Tight, starts <t:" in g
    assert f["Timer"] == "Next random giveaway <t:1794860313:R>\nQueue: PLEX, 2 winners"
    assert f["Prizes"].splitlines() == [
        # PLEX 6 unused - 1 running = 5; Nightmare 2; Tight 1 unused, 1 reserved = 0; Old 1 (inactive)
        "4 with codes · 8 codes available · 2 reserved",
        "1 empty",
        "Low stock (≤3 available):",
        "Tight: 0 left",
        "Nightmare: 2 left",
    ]
    assert f["Verified members"] == "1"
    assert f["Dependency updates"].splitlines()[0].startswith("3 pending (checked <t:")
    assert f["Dependency updates"].splitlines()[1:] == ["Python (pyapi): 2", "uv: 1"]


async def test_botstatus_flags_trouble(cog, update_state):
    watchdog.HEARTBEAT_FILE.write_text((now_utc() - datetime.timedelta(minutes=30)).isoformat())
    watchdog._save_alerting(True)
    cog.bot.get_cog("Giveaways").last_poller_error = (minutes_ago(3), "finishing giveaway #7: OperationalError: gone away")
    cog.bot.latency = float("nan")  # before the gateway connects
    status.next_timer_run.return_value = None

    embed = await _run(cog)
    f = _fields(embed)

    assert embed.colour.value == 0xE74C3C
    assert "**(stale)**" in f["Poller"] and "**alert open**" in f["Poller"]
    assert "Last error <t:" in f["Poller"] and "OperationalError: gone away" in f["Poller"]
    assert "gateway not connected" in f["Bot"]
    assert "timer not scheduled or unreadable" in f["Timer"] and "Queue empty" in f["Timer"]
    assert f["Giveaways (0 running, 0 scheduled)"] == "None"
    assert f["Dependency updates"] == "No dependency report yet"


async def test_botstatus_lists_are_capped(cog, make_prize, update_state):
    for n in range(status.MAX_LISTED + 3):
        make_prize(name=f"Low {n:02}", codes=["c"])
    update_state.write_text("{}")
    f = _fields(await _run(cog))
    assert f["Prizes"].endswith("…and 3 more")
    assert f["Dependency updates"].startswith("None pending")


def test_read_commit(tmp_path):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text("f3f9e96abcdef\n")
    assert status.read_commit(tmp_path) == "f3f9e96"

    (git / "refs" / "heads" / "main").unlink()
    (git / "packed-refs").write_text("# pack-refs with: peeled\n0123456789ab refs/heads/main\n")
    assert status.read_commit(tmp_path) == "0123456"

    (git / "HEAD").write_text("deadbeefcafe\n")  # detached
    assert status.read_commit(tmp_path) == "deadbee"
    assert status.read_commit(tmp_path / "nope") is None


def test_read_commit_of_this_repo():
    assert status.read_commit(status.REPO_DIR) is not None


async def test_next_timer_run_parses_systemctl_json(monkeypatch):
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b'[{"next":1794860313573583,"unit":"x.timer"}]', b""))
    monkeypatch.setattr(status.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    assert await status.next_timer_run("x.timer") == datetime.datetime(2026, 11, 16, 20, 18, 33, 573583)

    proc.communicate = AsyncMock(return_value=(b"[]", b""))
    assert await status.next_timer_run("x.timer") is None
    monkeypatch.setattr(status.asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError))
    assert await status.next_timer_run("x.timer") is None


async def test_giveaways_poller_records_last_error(session_factory, no_task_loops, monkeypatch):
    from fuzzworkbot.cogs.giveaways import Giveaways

    giveaways = Giveaways(SimpleNamespace(session_factory=session_factory))
    monkeypatch.setattr(giveaways, "_post_pending", AsyncMock(side_effect=RuntimeError("MySQL went away")))
    monkeypatch.setattr(giveaways, "_refresh_entrant_counts", AsyncMock())
    monkeypatch.setattr(giveaways, "_finish_active", AsyncMock())

    await giveaways.poll.coro(giveaways)

    when, what = giveaways.last_poller_error
    # (the step's name is the mock's here; in the bot it's e.g. "step _post_pending")
    assert what.startswith("step ") and what.endswith(": RuntimeError: MySQL went away")
    assert abs(when - now_utc()) < datetime.timedelta(seconds=5)
