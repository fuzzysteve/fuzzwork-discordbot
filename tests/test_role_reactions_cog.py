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
    bot = SimpleNamespace(session_factory=session_factory, get_guild=lambda guild_id: guild, get_emoji=lambda emoji_id: None)
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


async def test_reconcile_continues_past_unreadable_message(cog, session_factory, guild, role):
    # First message's channel is 403/500; the second must still be reconciled.
    with session_factory.begin() as session:
        session.add(RoleReaction(guild_id=1000, channel_id=3001, message_id=501, emoji="👍", role_id=ROLE_ID))
        session.add(RoleReaction(guild_id=1000, channel_id=3002, message_id=502, emoji="👍", role_id=ROLE_ID))
    member = _member(10)
    guild.get_member.side_effect = {10: member}.get
    forbidden, broken, ok = MagicMock(id=3001), MagicMock(id=3001), MagicMock(id=3002)
    forbidden.fetch_message = AsyncMock(side_effect=http_error(disnake.Forbidden, 403))
    ok.fetch_message = AsyncMock(
        return_value=SimpleNamespace(reactions=[fake_reaction([fake_user(10)], emoji=disnake.PartialEmoji(name="👍"))])
    )

    for first in (forbidden, broken):
        if first is broken:
            broken.fetch_message = AsyncMock(side_effect=http_error(disnake.HTTPException, 500))
            member.add_roles.reset_mock()
        guild.get_channel.side_effect = {3001: first, 3002: ok}.get
        await cog._reconcile()
        member.add_roles.assert_awaited_once_with(role)


# --- /rolemenu -------------------------------------------------------------------

import functools  # noqa: E402

from sqlalchemy import select  # noqa: E402

from conftest import fake_interaction  # noqa: E402

from fuzzworkbot.cogs import role_reactions as rr_module  # noqa: E402
from fuzzworkbot.db import RoleMenu  # noqa: E402


@functools.total_ordering
class FakeRole:
    def __init__(self, role_id, position, *, default=False, managed=False):
        self.id, self.position, self.managed, self._default = role_id, position, managed, default
        self.mention = f"<@&{role_id}>"

    def is_default(self):
        return self._default

    def __eq__(self, other):
        return self.position == other.position

    def __lt__(self, other):
        return self.position < other.position


@pytest.fixture
def menu_channel():
    channel = MagicMock(id=3000, mention="#roles")
    message = MagicMock()
    message.add_reaction = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=message)
    channel.send = AsyncMock(return_value=SimpleNamespace(id=900))
    channel.get_partial_message.return_value.edit = AsyncMock()
    channel.get_partial_message.return_value.remove_reaction = AsyncMock()
    return channel


@pytest.fixture
def menu_inter(menu_channel):
    """A moderator (top role at position 10) in a guild where the bot's top role is 20."""
    inter = fake_interaction(author_id=42)
    inter.author.top_role = FakeRole(1, 10)
    inter.guild.owner_id = 1
    inter.guild.me.top_role = FakeRole(2, 20)
    inter.guild.me.guild_permissions.manage_roles = True
    inter.guild.get_channel.side_effect = {3000: menu_channel}.get
    return inter


async def _create_menu(cog, menu_inter, menu_channel):
    await cog.rolemenu_create.callback(cog, menu_inter, channel=menu_channel, title="Pick roles", description="Choose!")
    return 900


def _reply(inter):
    return inter.response.send_message.call_args.args[0]


async def test_rolemenu_create_posts_and_records_menu(cog, session_factory, menu_inter, menu_channel):
    await _create_menu(cog, menu_inter, menu_channel)
    embed = menu_channel.send.call_args.kwargs["embed"]
    assert embed.title == "Pick roles" and "Choose!" in embed.description and "No roles yet" in embed.description
    with session_factory() as session:
        menu = session.get(RoleMenu, 900)
    assert (menu.channel_id, menu.guild_id, menu.created_by_discord_id) == (3000, 1000, 42)


async def test_rolemenu_add_reacts_stores_and_rerenders(cog, session_factory, menu_inter, menu_channel):
    message_id = await _create_menu(cog, menu_inter, menu_channel)
    role = FakeRole(ROLE_ID, 5)

    await cog.rolemenu_add.callback(cog, menu_inter, menu=str(message_id), emoji="🛡️", role=role)

    menu_channel.fetch_message.return_value.add_reaction.assert_awaited_once_with("🛡️")
    with session_factory() as session:
        [row] = session.scalars(select(RoleReaction)).all()
    assert (row.message_id, row.emoji, row.role_id, row.channel_id) == (900, "🛡️", ROLE_ID, 3000)
    embed = menu_channel.get_partial_message.return_value.edit.call_args.kwargs["embed"]
    assert f"🛡️ — <@&{ROLE_ID}>" in embed.description
    assert _reply(menu_inter).startswith("Added")
    # The bot's own reaction must not grant it the role.
    assert cog._role_id_for(900, disnake.PartialEmoji(name="🛡️")) == ROLE_ID


async def test_rolemenu_add_stores_custom_emoji_by_id(cog, session_factory, menu_inter, menu_channel):
    message_id = await _create_menu(cog, menu_inter, menu_channel)
    await cog.rolemenu_add.callback(cog, menu_inter, menu=str(message_id), emoji="<:fuzz:123456>", role=FakeRole(ROLE_ID, 5))
    with session_factory() as session:
        assert session.scalar(select(RoleReaction.emoji)) == "123456"


@pytest.mark.parametrize(
    "role, expected",
    [
        (FakeRole(ROLE_ID, 10), "below your own highest role"),  # same as the moderator's own
        (FakeRole(ROLE_ID, 15), "below your own highest role"),  # above the moderator
        (FakeRole(ROLE_ID, 25), "above my highest role"),
        (FakeRole(ROLE_ID, 0, default=True), "@everyone"),
        (FakeRole(ROLE_ID, 5, managed=True), "managed by an integration"),
    ],
)
async def test_rolemenu_add_refuses_roles_it_must_not_hand_out(cog, session_factory, menu_inter, menu_channel, role, expected):
    message_id = await _create_menu(cog, menu_inter, menu_channel)
    await cog.rolemenu_add.callback(cog, menu_inter, menu=str(message_id), emoji="🛡️", role=role)
    assert expected in _reply(menu_inter)
    menu_channel.fetch_message.return_value.add_reaction.assert_not_called()
    with session_factory() as session:
        assert session.scalar(select(RoleReaction)) is None


async def test_rolemenu_owner_may_hand_out_roles_above_their_own(cog, session_factory, menu_inter, menu_channel):
    message_id = await _create_menu(cog, menu_inter, menu_channel)
    menu_inter.guild.owner_id = 42
    await cog.rolemenu_add.callback(cog, menu_inter, menu=str(message_id), emoji="🛡️", role=FakeRole(ROLE_ID, 15))
    assert _reply(menu_inter).startswith("Added")


async def test_rolemenu_add_rejects_bad_emoji_without_storing(cog, session_factory, menu_inter, menu_channel):
    message_id = await _create_menu(cog, menu_inter, menu_channel)
    menu_channel.fetch_message.return_value.add_reaction.side_effect = http_error(disnake.HTTPException, 400)
    await cog.rolemenu_add.callback(cog, menu_inter, menu=str(message_id), emoji="not an emoji", role=FakeRole(ROLE_ID, 5))
    assert "wouldn't add that reaction" in _reply(menu_inter)
    with session_factory() as session:
        assert session.scalar(select(RoleReaction)) is None


async def test_rolemenu_accepts_message_links_and_rejects_unknown(cog, session_factory, menu_inter, menu_channel):
    role = FakeRole(ROLE_ID, 5)
    await cog.rolemenu_add.callback(
        cog, menu_inter, menu="https://discord.com/channels/1000/3000/555", emoji="👍", role=role
    )
    with session_factory() as session:
        assert session.scalar(select(RoleReaction.message_id)) == 555
    # Not a bot-posted menu: mapping stored, nothing to re-render.
    menu_channel.get_partial_message.return_value.edit.assert_not_called()

    await cog.rolemenu_add.callback(cog, menu_inter, menu="https://discord.com/channels/999/3000/555", emoji="👍", role=role)
    assert "different server" in _reply(menu_inter)
    await cog.rolemenu_add.callback(cog, menu_inter, menu="123", emoji="👍", role=role)
    assert "don't know that message" in _reply(menu_inter)


async def test_rolemenu_remove(cog, session_factory, menu_inter, menu_channel):
    message_id = await _create_menu(cog, menu_inter, menu_channel)
    await cog.rolemenu_add.callback(cog, menu_inter, menu=str(message_id), emoji="🛡️", role=FakeRole(ROLE_ID, 5))

    await cog.rolemenu_remove.callback(cog, menu_inter, menu=str(message_id), emoji="🛡️")

    assert _reply(menu_inter).startswith("Removed")
    menu_channel.get_partial_message.return_value.remove_reaction.assert_awaited_once_with("🛡️", menu_inter.guild.me)
    with session_factory() as session:
        assert session.scalar(select(RoleReaction)) is None
    await cog.rolemenu_remove.callback(cog, menu_inter, menu=str(message_id), emoji="🛡️")
    assert "isn't on that menu" in _reply(menu_inter)


async def test_rolemenu_list_and_autocomplete(cog, session_factory, menu_inter, menu_channel):
    message_id = await _create_menu(cog, menu_inter, menu_channel)
    await cog.rolemenu_add.callback(cog, menu_inter, menu=str(message_id), emoji="🛡️", role=FakeRole(ROLE_ID, 5))
    await cog.rolemenu_add.callback(cog, menu_inter, menu="https://discord.com/channels/1000/3000/555", emoji="👍", role=FakeRole(ROLE_ID, 5))

    await cog.rolemenu_list.callback(cog, menu_inter)
    listing = _reply(menu_inter)
    assert "**Pick roles** — https://discord.com/channels/1000/3000/900" in listing
    assert f"🛡️ → <@&{ROLE_ID}>" in listing and "**Message 555**" in listing

    inter = SimpleNamespace(bot=cog.bot, guild_id=1000)
    choices = await rr_module.autocomplete_menus(inter, "")
    assert [(c.name, c.value) for c in choices] == [("Pick roles", "900"), ("Message 555", "555")]
    assert [c.value for c in await rr_module.autocomplete_menus(inter, "pick")] == ["900"]
