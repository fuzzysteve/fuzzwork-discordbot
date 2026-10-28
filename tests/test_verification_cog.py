from types import SimpleNamespace
from unittest.mock import MagicMock

import disnake
import pytest
from conftest import fake_interaction, http_error

from fuzzworkbot.cogs import verification
from fuzzworkbot.cogs.verification import NOT_LINKED_MESSAGE, Verification
from fuzzworkbot.db import UserLookup


@pytest.fixture
def cog(session_factory):
    return Verification(SimpleNamespace(session_factory=session_factory))


@pytest.fixture
def linked(session_factory):
    with session_factory.begin() as session:
        session.add(UserLookup(discordid=42, eveid=90000001))


@pytest.fixture
def esi(monkeypatch):
    calls = []

    def fake_get_character_name(eve_id, user_agent):
        calls.append((eve_id, user_agent))
        return "Steve Ronuken"

    monkeypatch.setattr(verification, "get_character_name", fake_get_character_name)
    return calls


def _reply(inter):
    return inter.response.send_message.call_args.args[0]


def test_lookup_eveid(cog, linked):
    assert cog._lookup_eveid(42) == 90000001
    assert cog._lookup_eveid(43) is None


async def test_authme_not_linked(cog, esi):
    inter = fake_interaction(author_id=43)
    await cog.authme.callback(cog, inter)
    assert _reply(inter) == NOT_LINKED_MESSAGE
    assert esi == []


async def test_authme_grants_role_and_sets_nick(cog, linked, esi):
    inter = fake_interaction(author_id=42)
    role = SimpleNamespace(id=2000)
    inter.guild.get_role.return_value = role

    await cog.authme.callback(cog, inter)

    assert esi == [(90000001, "fuzzworkbot-tests")]
    inter.guild.get_role.assert_called_once_with(2000)
    inter.author.add_roles.assert_awaited_once_with(role)
    inter.author.edit.assert_awaited_once_with(nick="Steve Ronuken")
    assert _reply(inter) == "You are Steve Ronuken in Eve"


async def test_authme_still_replies_when_role_change_forbidden(cog, linked, esi):
    inter = fake_interaction(author_id=42)
    inter.author.add_roles.side_effect = http_error(disnake.Forbidden, 403)
    await cog.authme.callback(cog, inter)
    assert _reply(inter) == "You are Steve Ronuken in Eve"


async def test_authme_reports_esi_failure(cog, linked, monkeypatch):
    monkeypatch.setattr(verification, "get_character_name", MagicMock(side_effect=RuntimeError("ESI down")))
    inter = fake_interaction(author_id=42)
    await cog.authme.callback(cog, inter)
    assert "problem with the system" in _reply(inter)


async def test_auththem(cog, linked, esi):
    inter = fake_interaction()
    await cog.auththem.callback(cog, inter, user=SimpleNamespace(id=42, name="steve", mention="<@42>"))
    assert _reply(inter) == "<@42> is Steve Ronuken in Eve"


async def test_auththem_not_linked(cog, esi):
    inter = fake_interaction()
    await cog.auththem.callback(cog, inter, user=SimpleNamespace(id=43, name="nobody", mention="<@43>"))
    assert _reply(inter) == "Auth system doesn't know nobody yet."
