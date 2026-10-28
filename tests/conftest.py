"""Shared fixtures. Everything runs against throwaway SQLite files with Discord faked
out — no live Discord, MySQL or SDE connection is ever made.

The env vars below must be set before any fuzzworkbot.cogs module is imported: cogs call
load_config() at import time (see CLAUDE.md), and config.py's load_dotenv() only fills
in vars that are *unset*, so setting every one here keeps the real .env out of tests.
"""

import os

os.environ.update(
    DISCORD_BOT_TOKEN="test-token",
    GUILD_ID="1000",
    VALIDATED_ROLE_ID="2000",
    DATABASE_URL="sqlite://",
    ESI_USER_AGENT="fuzzworkbot-tests",
    SDE_DATABASE_URL="sqlite://",
)

import datetime  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import disnake  # noqa: E402
import pytest  # noqa: E402

from fuzzworkbot.db import STATUS_PENDING, Giveaway, GiveawayCode, Prize, make_session_factory, now_utc  # noqa: E402


@pytest.fixture
def db_url(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    # The CLI builds its own session factory from DATABASE_URL via load_config().
    monkeypatch.setenv("DATABASE_URL", url)
    return url


@pytest.fixture
def session_factory(db_url):
    return make_session_factory(db_url)


@pytest.fixture
def make_prize(session_factory):
    def _make(name="PLEX x2", codes=(), active=True):
        with session_factory.begin() as session:
            prize = Prize(name=name, active=active)
            session.add(prize)
            session.flush()
            for code in codes:
                session.add(GiveawayCode(prize_id=prize.id, code=code))
            return prize.id

    return _make


@pytest.fixture
def make_giveaway(session_factory):
    def _make(prize_id, status=STATUS_PENDING, winner_count=1, duration_hours=24, **kwargs):
        with session_factory.begin() as session:
            giveaway = Giveaway(
                prize_id=prize_id,
                guild_id=1000,
                channel_id=3000,
                duration_hours=duration_hours,
                winner_count=winner_count,
                status=status,
                **kwargs,
            )
            session.add(giveaway)
            session.flush()
            return giveaway.id

    return _make


@pytest.fixture
def no_task_loops(monkeypatch):
    """Cogs start their tasks.loop in __init__; don't actually schedule it in tests."""
    monkeypatch.setattr(disnake.ext.tasks.Loop, "start", lambda self, *a, **k: None)


class AsyncIter:
    """Stand-in for disnake's async iterators (e.g. Reaction.users())."""

    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        self._iter = iter(self._items)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


def fake_user(user_id, bot=False):
    return SimpleNamespace(id=user_id, bot=bot, mention=f"<@{user_id}>", send=AsyncMock())


def fake_reaction(users, emoji="🎉"):
    return SimpleNamespace(emoji=emoji, users=lambda: AsyncIter(users))


def http_error(cls, status):
    """Build a disnake HTTPException subclass without a real aiohttp response."""
    return cls(MagicMock(status=status, reason="test"), "test")


def fake_interaction(author_id=42, guild_id=1000, channel_id=3000):
    inter = MagicMock()
    inter.author.id = author_id
    inter.author.add_roles = AsyncMock()
    inter.author.edit = AsyncMock()
    inter.guild_id = guild_id
    inter.channel_id = channel_id
    inter.response.send_message = AsyncMock()
    return inter


def minutes_ago(n):
    return now_utc() - datetime.timedelta(minutes=n)
