from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest
from conftest import fake_reaction, fake_user, http_error

from fuzzworkbot.cogs.role_reactions import RoleReactions, _emoji_key
from fuzzworkbot.db import RoleReaction

MESSAGE_ID = 500
ROLE_ID = 600
CUSTOM = disnake.PartialEmoji(name="fuzz", id=123456)


def _member(user_id=10, bot=False, roles=()):
    return SimpleNamespace(id=user_id, bot=bot, roles=list(roles), add_roles=AsyncMock(), remove_roles=AsyncMock())


@pytest.fixture
def role():
    return SimpleNamespace(id=ROLE_ID)


@pytest.fixture
def guild(role):
    guild = MagicMock()
    guild.get_role.side_effect = lambda role_id: role if role_id == ROLE_ID else None
    guild.get_member.return_value = None
    guild.fetch_member = AsyncMock(side_effect=http_error(disnake.NotFound, 404))
    return guild


@pytest.fixture
def cog(session_factory, guild):
    bot = SimpleNamespace(session_factory=session_factory, get_guild=lambda guild_id: guild)
    return RoleReactions(bot)


@pytest.fixture
def mapping(session_factory):
    def _add(emoji):
        with session_factory.begin() as session:
            session.add(RoleReaction(guild_id=1000, channel_id=3000, message_id=MESSAGE_ID, emoji=emoji, role_id=ROLE_ID))

    return _add


def _payload(emoji, member=None, user_id=10, guild_id=1000, message_id=MESSAGE_ID):
    return SimpleNamespace(guild_id=guild_id, message_id=message_id, emoji=emoji, user_id=user_id, member=member)


def test_emoji_key_unicode():
    assert _emoji_key(disnake.PartialEmoji(name="👍")) == ("👍", None)


def test_emoji_key_custom():
    assert _emoji_key(CUSTOM) == ("<:fuzz:123456>", "123456")


@pytest.mark.parametrize(
    "stored, reacted",
    [("👍", disnake.PartialEmoji(name="👍")), ("123456", CUSTOM), ("<:fuzz:123456>", CUSTOM)],
)
def test_role_id_for_matches_text_or_custom_id(cog, mapping, stored, reacted):
    mapping(stored)
    assert cog._role_id_for(MESSAGE_ID, reacted) == ROLE_ID


def test_role_id_for_distinguishes_emoji(cog, mapping):
    # Exact comparison: different emoji must not collide (see EMOJI_TYPE in db.py).
    mapping("👍")
    assert cog._role_id_for(MESSAGE_ID, disnake.PartialEmoji(name="🎉")) is None
    assert cog._role_id_for(MESSAGE_ID + 1, disnake.PartialEmoji(name="👍")) is None


async def test_reaction_add_grants_role(cog, mapping, role):
    mapping("👍")
    member = _member()
    await cog.on_raw_reaction_add(_payload(disnake.PartialEmoji(name="👍"), member=member))
    member.add_roles.assert_awaited_once_with(role)


async def test_reaction_add_resolves_member_when_payload_lacks_it(cog, mapping, guild, role):
    mapping("👍")
    member = _member()
    guild.get_member.return_value = member
    await cog.on_raw_reaction_add(_payload(disnake.PartialEmoji(name="👍")))
    member.add_roles.assert_awaited_once_with(role)


@pytest.mark.parametrize(
    "payload_kwargs",
    [
        {"emoji": disnake.PartialEmoji(name="🎉")},  # unmapped emoji
        {"emoji": disnake.PartialEmoji(name="👍"), "guild_id": None},  # DM
        {"emoji": disnake.PartialEmoji(name="👍"), "message_id": 1},  # unmapped message
    ],
)
async def test_reaction_add_ignores_irrelevant_reactions(cog, mapping, payload_kwargs):
    mapping("👍")
    member = _member()
    await cog.on_raw_reaction_add(_payload(member=member, **payload_kwargs))
    member.add_roles.assert_not_called()


async def test_reaction_add_ignores_bots(cog, mapping):
    mapping("👍")
    member = _member(bot=True)
    await cog.on_raw_reaction_add(_payload(disnake.PartialEmoji(name="👍"), member=member))
    member.add_roles.assert_not_called()


async def test_reaction_add_swallows_http_errors(cog, mapping):
    mapping("👍")
    member = _member()
    member.add_roles.side_effect = http_error(disnake.Forbidden, 403)
    await cog.on_raw_reaction_add(_payload(disnake.PartialEmoji(name="👍"), member=member))


async def test_reaction_remove_revokes_role(cog, mapping, guild, role):
    mapping("👍")
    member = _member(roles=[role])
    guild.get_member.return_value = member
    await cog.on_raw_reaction_remove(_payload(disnake.PartialEmoji(name="👍")))
    member.remove_roles.assert_awaited_once_with(role)


async def test_reaction_remove_for_member_who_left(cog, mapping):
    # get_member -> None, fetch_member -> NotFound: nothing to do, no crash.
    mapping("👍")
    await cog.on_raw_reaction_remove(_payload(disnake.PartialEmoji(name="👍")))


async def test_reconcile_adds_missing_roles_only(cog, mapping, guild, role):
    mapping("👍")
    missing, has_it, bot = _member(10), _member(11, roles=[role]), _member(12, bot=True)
    members = {m.id: m for m in (missing, has_it, bot)}
    guild.get_member.side_effect = members.get
    reactions = [
        fake_reaction([fake_user(10), fake_user(11), fake_user(12, bot=True)], emoji=disnake.PartialEmoji(name="👍")),
        fake_reaction([fake_user(10)], emoji=disnake.PartialEmoji(name="🎉")),  # unmapped
    ]
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=SimpleNamespace(reactions=reactions))
    guild.get_channel.return_value = channel

    await cog._reconcile()

    missing.add_roles.assert_awaited_once_with(role)
    has_it.add_roles.assert_not_called()
    bot.add_roles.assert_not_called()
    channel.fetch_message.assert_awaited_once_with(MESSAGE_ID)


async def test_reconcile_survives_deleted_message(cog, mapping, guild):
    mapping("👍")
    channel = MagicMock()
    channel.fetch_message = AsyncMock(side_effect=http_error(disnake.NotFound, 404))
    guild.get_channel.return_value = channel
    await cog._reconcile()
