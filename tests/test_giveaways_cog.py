import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest
from conftest import fake_interaction, fake_reaction, fake_user, http_error, minutes_ago
from sqlalchemy import select

from fuzzworkbot.cogs import giveaways as giveaways_module
from fuzzworkbot.cogs.giveaways import Giveaways, autocomplete_prizes
from fuzzworkbot.db import STATUS_ACTIVE, STATUS_FINISHED, STATUS_PENDING, Giveaway, GiveawayCode, GiveawayWinner


def _channel(message=None):
    channel = MagicMock()
    channel.send = AsyncMock(return_value=SimpleNamespace(id=5555))
    channel.fetch_message = AsyncMock(return_value=message)
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


async def _create(cog, inter, prize, winners=1, duration_hours=24, channel=None):
    await cog.creategiveaway.callback(
        cog, inter, prize=prize, duration_hours=duration_hours, winners=winners, channel=channel
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
    assert "DMs closed" in channel.send.call_args.kwargs["embed"].description


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
