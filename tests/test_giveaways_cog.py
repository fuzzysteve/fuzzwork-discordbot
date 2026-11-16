import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest
from conftest import fake_interaction, fake_reaction, fake_user, http_error, minutes_ago
from sqlalchemy import func, select

from fuzzworkbot.cogs import giveaways as giveaways_module
from fuzzworkbot.cogs.giveaways import Giveaways, autocomplete_prizes
from fuzzworkbot.db import STATUS_ACTIVE, STATUS_FINISHED, STATUS_PENDING, Giveaway, GiveawayCode, GiveawayWinner, Prize


def _channel(message=None):
    channel = MagicMock()
    channel.send = AsyncMock(return_value=SimpleNamespace(id=5555))
    channel.fetch_message = AsyncMock(return_value=message)
    channel.get_partial_message.return_value.edit = AsyncMock()
    return channel


@pytest.fixture
def channel():
    return _channel()


@pytest.fixture
def cog(session_factory, no_task_loops, channel):
    guild = MagicMock()
    guild.get_channel.return_value = channel
    bot = SimpleNamespace(
        session_factory=session_factory,
        get_channel=lambda channel_id: channel,
        get_guild=lambda guild_id: guild,
    )
    return Giveaways(bot)


def _get(session_factory, giveaway_id):
    with session_factory() as session:
        return session.get(Giveaway, giveaway_id)


def _winners(session_factory, giveaway_id):
    with session_factory() as session:
        return session.scalars(select(GiveawayWinner).where(GiveawayWinner.giveaway_id == giveaway_id)).all()


# --- /creategiveaway -------------------------------------------------------------


async def _create(cog, inter, prize, winners=1, duration_hours=24, channel=None, start_at=None):
    await cog.creategiveaway.callback(
        cog, inter, prize=prize, duration_hours=duration_hours, winners=winners, channel=channel, start_at=start_at
    )


async def test_create_queues_pending_giveaway(cog, session_factory, make_prize):
    make_prize(name="PLEX", codes=["A", "B"])
    inter = fake_interaction(author_id=42, channel_id=3000)

    await _create(cog, inter, "PLEX", winners=2, duration_hours=12)

    with session_factory() as session:
        giveaway = session.scalar(select(Giveaway))
    assert giveaway.status == STATUS_PENDING
    assert (giveaway.winner_count, giveaway.duration_hours) == (2, 12)
    assert (giveaway.channel_id, giveaway.created_by_discord_id) == (3000, 42)
    assert "queued" in inter.response.send_message.call_args.args[0]


async def test_create_uses_explicit_channel(cog, session_factory, make_prize):
    make_prize(name="PLEX", codes=["A"])
    await _create(cog, fake_interaction(), "PLEX", channel=SimpleNamespace(id=7777))
    with session_factory() as session:
        assert session.scalar(select(Giveaway.channel_id)) == 7777


async def test_create_refuses_over_capacity(cog, session_factory, make_prize, make_giveaway):
    prize_id = make_prize(name="PLEX", codes=["A", "B", "C"])
    make_giveaway(prize_id, status=STATUS_ACTIVE, winner_count=2)
    inter = fake_interaction()

    await _create(cog, inter, "PLEX", winners=2)

    with session_factory() as session:
        assert len(session.scalars(select(Giveaway)).all()) == 1
    message = inter.response.send_message.call_args
    assert "only 1 code(s)" in message.args[0]
    assert message.kwargs["ephemeral"] is True


@pytest.mark.parametrize("active", [True, False])
async def test_create_rejects_unknown_or_inactive_prize(cog, session_factory, make_prize, active):
    make_prize(name="PLEX", codes=["A"], active=active)
    inter = fake_interaction()

    await _create(cog, inter, "PLEX" if not active else "Nope")

    with session_factory() as session:
        assert session.scalar(select(Giveaway)) is None
    assert "No active prize" in inter.response.send_message.call_args.args[0]


async def test_autocomplete_lists_only_matching_prizes_with_capacity(session_factory, make_prize, make_giveaway):
    make_prize(name="PLEX x2", codes=["A", "B"])
    fully_promised = make_prize(name="PLEX x5", codes=["C"])
    make_giveaway(fully_promised, winner_count=1)
    make_prize(name="Skin", codes=["D"])
    make_prize(name="PLEX old", codes=["E"], active=False)
    inter = SimpleNamespace(bot=SimpleNamespace(session_factory=session_factory))

    choices = await autocomplete_prizes(inter, "plex")

    assert [(c.name, c.value) for c in choices] == [("PLEX x2 (2 available)", "PLEX x2")]


# --- poller: posting -------------------------------------------------------------


async def test_post_pending_activates_giveaway(cog, channel, session_factory, make_prize, make_giveaway):
    prize_id = make_prize(name="PLEX", codes=["A"])
    giveaway_id = make_giveaway(prize_id, duration_hours=48, winner_count=1)

    await cog._post_pending()

    giveaway = _get(session_factory, giveaway_id)
    assert giveaway.status == STATUS_ACTIVE
    assert giveaway.message_id == 5555
    assert giveaway.ends_at - giveaway.starts_at == datetime.timedelta(hours=48)
    assert abs(giveaway.starts_at - minutes_ago(0)) < datetime.timedelta(seconds=5)
    embed = channel.send.call_args.kwargs["embed"]
    assert embed.fields[0].value == "PLEX"


async def test_post_leaves_giveaway_pending_when_channel_missing(cog, session_factory, make_prize, make_giveaway):
    giveaway_id = make_giveaway(make_prize(codes=["A"]))
    cog.bot.get_channel = lambda channel_id: None

    await cog._post_pending()

    assert _get(session_factory, giveaway_id).status == STATUS_PENDING


# --- poller: finishing -----------------------------------------------------------


async def test_finish_active_only_picks_expired(cog, make_prize, make_giveaway, monkeypatch):
    prize_id = make_prize(codes=["A", "B"])
    expired = make_giveaway(prize_id, status=STATUS_ACTIVE, message_id=1, ends_at=minutes_ago(1))
    make_giveaway(prize_id, status=STATUS_ACTIVE, message_id=2, ends_at=minutes_ago(-60))
    make_giveaway(prize_id, status=STATUS_PENDING)
    finished = []
    monkeypatch.setattr(cog, "_finish_one", AsyncMock(side_effect=finished.append))

    await cog._finish_active()

    assert finished == [expired]


async def _finish_with(cog, channel, make_prize, make_giveaway, users, codes=("A", "B", "C"), winner_count=1):
    prize_id = make_prize(name="PLEX", codes=codes)
    giveaway_id = make_giveaway(
        prize_id, status=STATUS_ACTIVE, message_id=99, ends_at=minutes_ago(1), winner_count=winner_count
    )
    channel.fetch_message.return_value = SimpleNamespace(reactions=[fake_reaction(users)])
    await cog._finish_one(giveaway_id)
    return giveaway_id


async def test_finish_draws_winner_assigns_code_and_dms(cog, channel, session_factory, make_prize, make_giveaway):
    winner = fake_user(10)
    giveaway_id = await _finish_with(cog, channel, make_prize, make_giveaway, [winner, fake_user(11, bot=True)])

    assert _get(session_factory, giveaway_id).status == STATUS_FINISHED
    [row] = _winners(session_factory, giveaway_id)
    assert row.discord_id == 10
    with session_factory() as session:
        code = session.get(GiveawayCode, row.code_id)
    assert code.assigned_to_discord_id == 10 and code.assigned_at is not None
    assert code.code in winner.send.call_args.args[0]
    assert channel.send.call_args.kwargs["embed"].title == "Giveaway Winner!"


async def test_finish_ignores_bots_and_dedupes_across_reactions(cog, channel, session_factory, make_prize, make_giveaway):
    prize_id = make_prize(codes=["A", "B", "C"])
    giveaway_id = make_giveaway(prize_id, status=STATUS_ACTIVE, message_id=99, ends_at=minutes_ago(1), winner_count=3)
    human = fake_user(10)
    channel.fetch_message.return_value = SimpleNamespace(
        reactions=[fake_reaction([human, fake_user(1, bot=True)]), fake_reaction([human], emoji="👍")]
    )

    await cog._finish_one(giveaway_id)

    assert [w.discord_id for w in _winners(session_factory, giveaway_id)] == [10]
    note = channel.send.call_args.kwargs["embed"].fields[-1]
    assert note.name == "Note" and "Only 1 entrant(s) for 3" in note.value


async def test_finish_multiple_winners_get_distinct_codes(cog, channel, session_factory, make_prize, make_giveaway):
    users = [fake_user(i) for i in (10, 11, 12)]
    giveaway_id = await _finish_with(cog, channel, make_prize, make_giveaway, users, winner_count=2)

    rows = _winners(session_factory, giveaway_id)
    assert len(rows) == 2
    assert len({r.discord_id for r in rows}) == 2
    assert len({r.code_id for r in rows}) == 2
    assert channel.send.call_args.kwargs["embed"].title == "Giveaway Winners!"


async def test_finish_with_no_entrants(cog, channel, session_factory, make_prize, make_giveaway):
    giveaway_id = await _finish_with(cog, channel, make_prize, make_giveaway, [fake_user(1, bot=True)])

    assert _get(session_factory, giveaway_id).status == STATUS_FINISHED
    assert _winners(session_factory, giveaway_id) == []
    assert "No one entered" in channel.send.call_args.kwargs["embed"].description


async def test_finish_dm_forbidden_still_assigns_code(cog, channel, session_factory, make_prize, make_giveaway):
    winner = fake_user(10)
    winner.send.side_effect = http_error(disnake.Forbidden, 403)

    giveaway_id = await _finish_with(cog, channel, make_prize, make_giveaway, [winner])

    [row] = _winners(session_factory, giveaway_id)
    assert row.code_id is not None
    assert "couldn't DM you" in channel.send.call_args.kwargs["embed"].description


async def test_finish_out_of_codes_records_winner_without_code(cog, channel, session_factory, make_prize, make_giveaway):
    giveaway_id = await _finish_with(cog, channel, make_prize, make_giveaway, [fake_user(10)], codes=())

    [row] = _winners(session_factory, giveaway_id)
    assert row.code_id is None
    assert "no code left" in channel.send.call_args.kwargs["embed"].description


async def test_finish_message_deleted_closes_giveaway(cog, channel, session_factory, make_prize, make_giveaway):
    giveaway_id = make_giveaway(make_prize(codes=["A"]), status=STATUS_ACTIVE, message_id=99, ends_at=minutes_ago(1))
    channel.fetch_message.side_effect = http_error(disnake.NotFound, 404)

    await cog._finish_one(giveaway_id)

    assert _get(session_factory, giveaway_id).status == STATUS_FINISHED
    assert _winners(session_factory, giveaway_id) == []


async def test_finish_skips_non_active(cog, channel, session_factory, make_prize, make_giveaway):
    giveaway_id = make_giveaway(make_prize(codes=["A"]), status=STATUS_FINISHED)
    await cog._finish_one(giveaway_id)
    channel.fetch_message.assert_not_called()


def test_module_config_uses_test_guild():
    assert giveaways_module._config.guild_id == 1000


# --- /giveawayqueue --------------------------------------------------------------


async def test_giveawayqueue_add_list_remove_clear(cog, session_factory, make_prize):
    from fuzzworkbot.db import QueuedGiveaway

    prize_id = make_prize(name="PLEX", codes=["A", "B", "C"])
    inter = fake_interaction(author_id=42)
    await cog.giveawayqueue_add.callback(cog, inter, prize="PLEX", winners=2)
    assert "position 1" in inter.response.send_message.call_args.args[0]
    inter = fake_interaction()
    await cog.giveawayqueue_add.callback(cog, inter, prize="PLEX", winners=None)
    assert "position 2" in inter.response.send_message.call_args.args[0]
    with session_factory() as session:
        rows = session.scalars(select(QueuedGiveaway).order_by(QueuedGiveaway.id)).all()
    assert [(r.prize_id, r.winner_count, r.added_by_discord_id) for r in rows] == [(prize_id, 2, 42), (prize_id, None, 42)]

    inter = fake_interaction()
    await cog.giveawayqueue_list.callback(cog, inter)
    listing = inter.response.send_message.call_args.args[0]
    assert listing.startswith("1. #") and "PLEX (2 winner(s))" in listing and "2. #" in listing

    inter = SimpleNamespace(bot=cog.bot)
    choices = await giveaways_module.autocomplete_queue(inter, "")
    assert [c.value for c in choices] == [str(rows[0].id), str(rows[1].id)]

    inter = fake_interaction()
    await cog.giveawayqueue_remove.callback(cog, inter, entry=str(rows[0].id))
    assert inter.response.send_message.call_args.args[0] == "Removed."
    inter = fake_interaction()
    await cog.giveawayqueue_remove.callback(cog, inter, entry="nonsense")
    assert inter.response.send_message.call_args.args[0] == "No such queue entry."

    inter = fake_interaction()
    await cog.giveawayqueue_clear.callback(cog, inter)
    assert "Cleared 1" in inter.response.send_message.call_args.args[0]


async def test_giveawayqueue_add_rejects_over_capacity(cog, session_factory, make_prize):
    from fuzzworkbot.db import QueuedGiveaway

    make_prize(name="PLEX", codes=["A"])
    inter = fake_interaction()
    await cog.giveawayqueue_add.callback(cog, inter, prize="PLEX", winners=3)
    assert "Not queued" in inter.response.send_message.call_args.args[0]
    with session_factory() as session:
        assert session.scalar(select(QueuedGiveaway)) is None


# --- poller resilience -----------------------------------------------------------


async def test_poll_survives_step_failures(cog, monkeypatch):
    finish = AsyncMock()
    monkeypatch.setattr(cog, "_post_pending", AsyncMock(side_effect=RuntimeError("MySQL went away")))
    monkeypatch.setattr(cog, "_finish_active", finish)

    await cog.poll.coro(cog)  # must not raise, or tasks.loop stops for good

    finish.assert_awaited_once()


async def test_one_failing_giveaway_does_not_block_others(cog, make_prize, make_giveaway, monkeypatch):
    prize_id = make_prize(codes=["A", "B"])
    first = make_giveaway(prize_id, status=STATUS_ACTIVE, message_id=1, ends_at=minutes_ago(2))
    second = make_giveaway(prize_id, status=STATUS_ACTIVE, message_id=2, ends_at=minutes_ago(1))
    attempted = []

    async def finish(giveaway_id):
        attempted.append(giveaway_id)
        if giveaway_id == first:
            raise http_error(disnake.HTTPException, 500)

    monkeypatch.setattr(cog, "_finish_one", finish)
    await cog._finish_active()
    assert sorted(attempted) == sorted([first, second])


async def test_finish_announcement_failure_keeps_draw_committed(cog, channel, session_factory, make_prize, make_giveaway):
    # The DM with the code has already gone out by the time the announcement fails —
    # the draw must stay committed, or the retry would hand the same code to someone else.
    channel.send.side_effect = http_error(disnake.HTTPException, 500)
    winner = fake_user(10)

    giveaway_id = await _finish_with(cog, channel, make_prize, make_giveaway, [winner])

    assert _get(session_factory, giveaway_id).status == STATUS_FINISHED
    [row] = _winners(session_factory, giveaway_id)
    with session_factory() as session:
        code = session.get(GiveawayCode, row.code_id)
    assert code.assigned_to_discord_id == 10
    assert code.code in winner.send.call_args.args[0]

    channel.send.reset_mock()
    await cog._finish_one(giveaway_id)  # a retry is a no-op: no second draw
    assert len(_winners(session_factory, giveaway_id)) == 1
    channel.send.assert_not_called()


async def test_finish_dm_server_error_still_finishes(cog, channel, session_factory, make_prize, make_giveaway):
    first, second = fake_user(10), fake_user(11)
    first.send.side_effect = http_error(disnake.HTTPException, 500)

    giveaway_id = await _finish_with(cog, channel, make_prize, make_giveaway, [first, second], winner_count=2)

    assert len(_winners(session_factory, giveaway_id)) == 2
    second.send.assert_awaited_once()
    assert "couldn't DM you" in channel.send.call_args.kwargs["embed"].description



# --- button entry, countdown, entrant counts, heartbeat ---------------------------


def test_giveaway_embed_has_discord_countdown():
    ends_at = datetime.datetime(2026, 11, 18, 12, 0, 0)  # naive UTC
    embed = giveaways_module.giveaway_embed("PLEX", 1, ends_at, entrants=3)
    ends = embed.fields[-1]
    assert ends.name == "Ends"
    assert ends.value == "<t:1795003200:R> (<t:1795003200:f>)"
    assert {f.name: f.value for f in embed.fields}["Entrants"] == "3"
    assert embed.fields[-1].name == "Ends"
    assert giveaways_module.giveaway_embed("PLEX", 1, ends_at, None, ended=True).fields[-1].name == "Ended"


async def test_post_uses_enter_button(cog, channel, session_factory, make_prize, make_giveaway):
    from fuzzworkbot.db import ENTRY_BUTTON

    giveaway_id = make_giveaway(make_prize(name="PLEX", codes=["A"]))
    await cog._post_pending()

    [button] = channel.send.call_args.kwargs["components"]
    assert button.custom_id == f"giveaway:enter:{giveaway_id}" and not button.disabled
    assert _get(session_factory, giveaway_id).entry_mode == ENTRY_BUTTON
    assert {f.name: f.value for f in channel.send.call_args.kwargs["embed"].fields}["Entrants"] == "0"


def _click(giveaway_id, author_id=10, custom_id=None):
    inter = MagicMock()
    inter.component.custom_id = custom_id or f"giveaway:enter:{giveaway_id}"
    inter.author.id = author_id
    inter.response.send_message = AsyncMock()
    return inter


def _entries(session_factory, giveaway_id):
    from fuzzworkbot.db import GiveawayEntry

    with session_factory() as session:
        return sorted(session.scalars(select(GiveawayEntry.discord_id).where(GiveawayEntry.giveaway_id == giveaway_id)))


async def test_enter_button_records_entry_once(cog, session_factory, make_prize, make_giveaway):
    giveaway_id = make_giveaway(make_prize(codes=["A"]), status=STATUS_ACTIVE, message_id=1, ends_at=minutes_ago(-60))

    first = _click(giveaway_id)
    await cog.on_button_click(first)
    again = _click(giveaway_id)
    await cog.on_button_click(again)

    assert "You're entered" in first.response.send_message.call_args.args[0]
    assert "already entered" in again.response.send_message.call_args.args[0]
    assert first.response.send_message.call_args.kwargs["ephemeral"] is True
    assert _entries(session_factory, giveaway_id) == [10]


async def test_enter_button_refuses_ended_giveaways(cog, session_factory, make_prize, make_giveaway):
    prize_id = make_prize(codes=["A"])
    past_end = make_giveaway(prize_id, status=STATUS_ACTIVE, message_id=1, ends_at=minutes_ago(1))
    finished = make_giveaway(prize_id, status=STATUS_FINISHED, message_id=2, ends_at=minutes_ago(-60))
    for giveaway_id in (past_end, finished, 999):
        inter = _click(giveaway_id)
        await cog.on_button_click(inter)
        assert "has ended" in inter.response.send_message.call_args.args[0]
        assert _entries(session_factory, giveaway_id) == []


async def test_other_buttons_are_ignored(cog):
    inter = _click(0, custom_id="something:else")
    await cog.on_button_click(inter)
    inter.response.send_message.assert_not_called()


async def test_refresh_entrant_counts_edits_only_changed_posts(cog, channel, session_factory, make_prize, make_giveaway):
    giveaway_id = make_giveaway(
        make_prize(name="PLEX", codes=["A"]), status=STATUS_ACTIVE, message_id=77, ends_at=minutes_ago(-60), entry_mode="button"
    )
    edit = channel.get_partial_message.return_value.edit

    await cog._refresh_entrant_counts()  # first look after a restart: always refreshed
    assert edit.await_count == 1
    await cog._refresh_entrant_counts()  # unchanged: no edit
    assert edit.await_count == 1

    await cog.on_button_click(_click(giveaway_id))
    await cog._refresh_entrant_counts()
    assert edit.await_count == 2
    channel.get_partial_message.assert_called_with(77)
    assert {f.name: f.value for f in edit.call_args.kwargs["embed"].fields}["Entrants"] == "1"


async def test_finish_button_giveaway_draws_from_entries(cog, channel, session_factory, make_prize, make_giveaway):
    giveaway_id = make_giveaway(
        make_prize(name="PLEX", codes=["A"]), status=STATUS_ACTIVE, message_id=77, ends_at=minutes_ago(-60), entry_mode="button"
    )
    await cog.on_button_click(_click(giveaway_id, author_id=10))
    with session_factory.begin() as session:
        session.get(Giveaway, giveaway_id).ends_at = minutes_ago(1)
    winner = fake_user(10)
    cog.bot.get_user = lambda user_id: None
    cog.bot.fetch_user = AsyncMock(return_value=winner)

    await cog._finish_one(giveaway_id)

    channel.fetch_message.assert_not_called()  # entries come from the DB, not reactions
    [row] = _winners(session_factory, giveaway_id)
    assert row.discord_id == 10
    assert "Your code: `A`" in winner.send.call_args.args[0]
    assert channel.send.call_args.kwargs["embed"].description == "<@10>"
    edit = channel.get_partial_message.return_value.edit.call_args.kwargs
    assert edit["components"][0].disabled is True
    assert edit["embed"].fields[-1].name == "Ended"


async def test_poll_writes_heartbeat_only_when_healthy(cog, monkeypatch):
    from fuzzworkbot import watchdog

    for step in ("_post_pending", "_refresh_entrant_counts", "_finish_active"):
        monkeypatch.setattr(cog, step, AsyncMock())
    await cog.poll.coro(cog)
    assert watchdog.read_heartbeat() is not None

    watchdog.HEARTBEAT_FILE.unlink()
    monkeypatch.setattr(cog, "_finish_active", AsyncMock(side_effect=RuntimeError("DB down")))
    await cog.poll.coro(cog)
    assert watchdog.read_heartbeat() is None


# --- scheduled start -------------------------------------------------------------


async def test_create_scheduled_waits_for_start(cog, channel, session_factory, make_prize):
    from fuzzworkbot.db import now_utc

    make_prize(name="PLEX", codes=["A"])
    start = now_utc() + datetime.timedelta(days=2)
    inter = fake_interaction()

    await _create(cog, inter, "PLEX", start_at=start.strftime("%Y-%m-%d %H:%M"))

    assert "scheduled for <t:" in inter.response.send_message.call_args.args[0]
    with session_factory() as session:
        giveaway = session.scalar(select(Giveaway))
    assert giveaway.status == STATUS_PENDING and giveaway.starts_at == start.replace(second=0, microsecond=0)

    await cog._post_pending()
    channel.send.assert_not_called()  # not yet

    with session_factory.begin() as session:
        session.get(Giveaway, giveaway.id).starts_at = minutes_ago(1)
    await cog._post_pending()
    channel.send.assert_awaited_once()
    assert _get(session_factory, giveaway.id).status == STATUS_ACTIVE


async def test_create_rejects_bad_start(cog, session_factory, make_prize):
    make_prize(name="PLEX", codes=["A"])
    inter = fake_interaction()
    await _create(cog, inter, "PLEX", start_at="next tuesday")
    assert "must look like" in inter.response.send_message.call_args.args[0]
    with session_factory() as session:
        assert session.scalar(select(Giveaway)) is None


# --- /giveaway cancel | endnow ----------------------------------------------------


def _deferred_reply(inter):
    inter.response.defer.assert_awaited_once_with(ephemeral=True)
    return inter.edit_original_response.call_args.args[0]


async def test_cancel_scheduled_frees_its_codes(cog, session_factory, make_prize, make_giveaway):
    from fuzzworkbot.db import STATUS_CANCELLED
    from fuzzworkbot.giveaway_logic import available_code_count

    prize_id = make_prize(codes=["A", "B"])
    giveaway_id = make_giveaway(prize_id, winner_count=2, starts_at=minutes_ago(-600))
    with session_factory() as session:
        assert available_code_count(session, prize_id) == 0

    inter = fake_interaction()
    await cog.giveaway_cancel.callback(cog, inter, giveaway=str(giveaway_id))

    assert "2 reserved code(s) are available again" in _deferred_reply(inter)
    assert _get(session_factory, giveaway_id).status == STATUS_CANCELLED
    with session_factory() as session:
        assert available_code_count(session, prize_id) == 2


async def test_cancel_running_button_giveaway_updates_post(cog, channel, session_factory, make_prize, make_giveaway):
    from fuzzworkbot.db import STATUS_CANCELLED

    giveaway_id = make_giveaway(
        make_prize(name="PLEX", codes=["A"]), status=STATUS_ACTIVE, message_id=77, ends_at=minutes_ago(-60), entry_mode="button"
    )
    await cog.on_button_click(_click(giveaway_id))

    await cog.giveaway_cancel.callback(cog, fake_interaction(), giveaway=str(giveaway_id))

    edit = channel.get_partial_message.return_value.edit.call_args.kwargs
    assert "cancelled" in edit["embed"].description
    assert edit["components"][0].disabled and edit["components"][0].label == "Giveaway cancelled"
    assert _get(session_factory, giveaway_id).status == STATUS_CANCELLED

    # Clicks after cancelling are refused, and the poller never draws it.
    late = _click(giveaway_id, author_id=11)
    await cog.on_button_click(late)
    assert "has ended" in late.response.send_message.call_args.args[0]
    with session_factory.begin() as session:
        session.get(Giveaway, giveaway_id).ends_at = minutes_ago(1)
    await cog._finish_active()
    assert _winners(session_factory, giveaway_id) == []


async def test_cancel_refuses_finished(cog, session_factory, make_prize, make_giveaway):
    giveaway_id = make_giveaway(make_prize(codes=["A"]), status=STATUS_FINISHED)
    inter = fake_interaction()
    await cog.giveaway_cancel.callback(cog, inter, giveaway=str(giveaway_id))
    assert "isn't scheduled or running" in _deferred_reply(inter)
    assert _get(session_factory, giveaway_id).status == STATUS_FINISHED


async def test_endnow_draws_immediately(cog, channel, session_factory, make_prize, make_giveaway):
    giveaway_id = make_giveaway(
        make_prize(name="PLEX", codes=["A"]), status=STATUS_ACTIVE, message_id=77, ends_at=minutes_ago(-600), entry_mode="button"
    )
    await cog.on_button_click(_click(giveaway_id, author_id=10))
    cog.bot.get_user = lambda user_id: fake_user(user_id)

    inter = fake_interaction()
    await cog.giveaway_endnow.callback(cog, inter, giveaway=str(giveaway_id))

    assert "ended and its winners were drawn" in _deferred_reply(inter)
    assert _get(session_factory, giveaway_id).status == STATUS_FINISHED
    assert [w.discord_id for w in _winners(session_factory, giveaway_id)] == [10]


async def test_endnow_refuses_unposted(cog, session_factory, make_prize, make_giveaway):
    giveaway_id = make_giveaway(make_prize(codes=["A"]))
    inter = fake_interaction()
    await cog.giveaway_endnow.callback(cog, inter, giveaway=str(giveaway_id))
    assert "hasn't been posted yet" in _deferred_reply(inter)
    assert _get(session_factory, giveaway_id).status == STATUS_PENDING


async def test_autocomplete_open_giveaways(cog, make_prize, make_giveaway):
    prize_id = make_prize(name="PLEX", codes=["A", "B", "C"])
    running = make_giveaway(prize_id, status=STATUS_ACTIVE, message_id=1, ends_at=datetime.datetime(2026, 11, 18, 12, 0))
    scheduled = make_giveaway(prize_id, starts_at=datetime.datetime(2026, 11, 21, 18, 0))
    make_giveaway(prize_id, status=STATUS_FINISHED)

    choices = await giveaways_module.autocomplete_open_giveaways(SimpleNamespace(bot=cog.bot), "")

    assert [(c.value, c.name) for c in choices] == [
        (str(scheduled), f"#{scheduled} PLEX (scheduled for 2026-11-21 18:00 UTC)"),
        (str(running), f"#{running} PLEX (running, ends 2026-11-18 12:00 UTC)"),
    ]


# --- /prize list | import -------------------------------------------------------


async def test_prize_list(cog, session_factory, make_prize, make_giveaway):
    prize_id = make_prize(name="PLEX", codes=["A", "B", "C"])
    make_giveaway(prize_id, winner_count=2)
    make_prize(name="Old", codes=["X"], active=False)
    make_prize(name="Empty", codes=[])
    used_up = make_prize(name="Used up", codes=["U"])
    all_reserved = make_prize(name="All reserved", codes=["R"])
    make_giveaway(all_reserved, winner_count=1)  # 0 available but not empty: stays listed
    with session_factory.begin() as session:
        session.scalar(select(GiveawayCode).where(GiveawayCode.prize_id == used_up)).assigned_at = minutes_ago(1)

    inter = fake_interaction()
    await cog.prize_list.callback(cog, inter, show_empty=False)
    text = inter.response.send_message.call_args.args[0]
    assert text.splitlines() == [
        "**All reserved** — 0 available, 1 reserved by giveaways",
        "**PLEX** — 1 available, 2 reserved by giveaways",
        "**Old** *(inactive)* — 1 available",
        "*2 prize(s) with no codes left hidden; use `show_empty` to see them.*",
    ]
    assert inter.response.send_message.call_args.kwargs["ephemeral"] is True

    inter = fake_interaction()
    await cog.prize_list.callback(cog, inter, show_empty=True)
    text = inter.response.send_message.call_args.args[0]
    assert "**Empty** — 0 available" in text and "**Used up** — 0 available" in text and "hidden" not in text


async def test_prize_list_all_empty(cog, make_prize):
    make_prize(name="Empty", codes=[])
    inter = fake_interaction()
    await cog.prize_list.callback(cog, inter, show_empty=False)
    assert inter.response.send_message.call_args.args[0].splitlines() == [
        "Every prize is out of codes. Add more with `/prize import`.",
        "*1 prize(s) with no codes left hidden; use `show_empty` to see them.*",
    ]


def _attachment(content: bytes):
    return SimpleNamespace(size=len(content), read=AsyncMock(return_value=content))


async def _import(cog, content, prize=None):
    inter = fake_interaction()
    await cog.prize_import.callback(cog, inter, file=_attachment(content), prize=prize)
    return _deferred_reply(inter)


async def test_prize_import_csv_creates_prizes_and_never_echoes_codes(cog, session_factory, make_prize):
    make_prize(name="PLEX", codes=["SECRET-A"])
    reply = await _import(cog, "﻿Prize,Code,Given\nPLEX,SECRET-A,\nPLEX,SECRET-B,\nPLEX,SECRET-C,yes\nSkin,SECRET-D,\n".encode())

    assert reply == (
        "Imported 2 code(s) into 2 prize(s). New prize(s): Skin. Skipped 1 duplicate(s). "
        "Skipped 1 row(s) marked as already given."
    )
    assert "SECRET" not in reply
    with session_factory() as session:
        assert sorted(session.scalars(select(GiveawayCode.code))) == ["SECRET-A", "SECRET-B", "SECRET-D"]


async def test_prize_import_lines_into_existing_prize(cog, session_factory, make_prize):
    make_prize(name="PLEX", codes=[])
    assert await _import(cog, b"A\n\n B \nA\n", prize="PLEX") == "Imported 2 code(s) into 1 prize(s). Skipped 1 duplicate(s)."
    assert "No prize named 'Nope'" in await _import(cog, b"A\n", prize="Nope")
    with session_factory() as session:
        assert session.scalar(select(func.count(Prize.id))) == 1


@pytest.mark.parametrize(
    "content, error",
    [
        (b"just,some,columns\n1,2,3\n", "must have 'prize' and 'code' columns"),
        (b"prize,code\n", "No codes found"),
        (b"\xff\xfe\x00bad", "isn't UTF-8"),
    ],
)
async def test_prize_import_rejects_bad_files(cog, session_factory, content, error):
    assert error in await _import(cog, content)
    with session_factory() as session:
        assert session.scalar(select(GiveawayCode)) is None


async def test_prize_import_rejects_oversized(cog):
    inter = fake_interaction()
    big = SimpleNamespace(size=giveaways_module.MAX_IMPORT_BYTES + 1, read=AsyncMock())
    await cog.prize_import.callback(cog, inter, file=big, prize=None)
    assert "too big" in _deferred_reply(inter)
    big.read.assert_not_called()
