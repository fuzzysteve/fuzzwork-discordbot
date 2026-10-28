from types import SimpleNamespace

import pytest
from conftest import fake_interaction
from disnake.ext import commands

from fuzzworkbot.cogs import eve_lookup
from fuzzworkbot.cogs.eve_lookup import EveLookup, _build_contents_embed, _fmt_number


@pytest.mark.parametrize(
    "value, expected",
    [(None, "0"), (0, "0"), (1067000, "1,067,000"), (355.0, "355"), (2.5, "2.50"), (1234.567, "1,234.57")],
)
def test_fmt_number(value, expected):
    assert _fmt_number(value) == expected


def test_contents_embed_groups_and_counts():
    embed = _build_contents_embed(
        "Jita",
        [
            {"groupName": "Planet", "itemName": "Jita I"},
            {"groupName": "Planet", "itemName": "Jita II"},
            {"groupName": None, "itemName": "Mystery"},
        ],
    )
    assert embed.description == "3 item(s)"
    assert [(f.name, f.value) for f in embed.fields] == [("Planet (2)", "Jita I, Jita II"), ("Unknown (1)", "Mystery")]


def test_contents_embed_truncates_long_groups():
    names = [f"Jita IV - Moon {i} - Some Long Station Name Here" for i in range(60)]
    embed = _build_contents_embed("Jita", [{"groupName": "Station", "itemName": n} for n in names])
    value = embed.fields[0].value
    assert len(value) <= 1024  # Discord's field-value cap
    assert value.endswith("more")
    shown = value.count("Some Long Station Name Here")
    assert f"and {60 - shown} more" in value


@pytest.fixture
def cog():
    return EveLookup(SimpleNamespace())


async def test_type_command_ship_embed(cog, monkeypatch):
    monkeypatch.setattr(
        eve_lookup.sde,
        "lookup_type",
        lambda url, name: {
            "typeName": "Rifter", "description": "A frigate.", "groupName": "Frigate", "categoryName": "Ship",
            "mass": 1067000, "volume": 27289, "capacity": 140,
            "ship_attributes": {"hiSlots": 4, "medSlots": 3, "lowSlots": 3, "rigSlots": 3},
        },
    )
    inter = fake_interaction()

    await cog.type.callback(cog, inter, name="Rifter")

    embed = inter.response.send_message.call_args.kwargs["embed"]
    fields = {f.name: f.value for f in embed.fields}
    assert embed.title == "Rifter"
    assert fields["Mass / Volume / Cargo"] == "1,067,000 kg / 27,289 m³ / 140 m³"
    assert fields["Slots"] == "4 / 3 / 3 / 3"
    assert fields["Hardpoints"] == "0 / 0"  # absent attributes render as 0


async def test_type_command_not_found(cog, monkeypatch):
    monkeypatch.setattr(eve_lookup.sde, "lookup_type", lambda url, name: None)
    inter = fake_interaction()
    await cog.type.callback(cog, inter, name="Nope")
    assert inter.response.send_message.call_args.kwargs["ephemeral"] is True


async def test_system_command_includes_neighbours_and_contents(cog, monkeypatch):
    system = {
        "itemID": 30000142, "typeID": 5, "groupID": 5, "solarSystemID": 30000142, "solarSystemName": "Jita",
        "constellationName": "Kimotoro", "regionName": "The Forge", "orbitID": None, "radius": 1.0e13,
        "security": 0.9459, "itemName": "Jita",
    }
    monkeypatch.setattr(eve_lookup.sde, "lookup_system", lambda url, name: system)
    monkeypatch.setattr(eve_lookup.sde, "get_system_neighbours", lambda url, sid: ["Perimeter", "Maurasi"])
    monkeypatch.setattr(eve_lookup.sde, "get_system_contents", lambda url, sid: [{"groupName": "Planet", "itemName": "Jita IV"}])
    inter = fake_interaction()

    await cog.system.callback(cog, inter, name="Jita")

    detail, contents = inter.response.send_message.call_args.kwargs["embeds"]
    fields = {f.name: f.value for f in detail.fields}
    assert fields["Security"] == "0.9459"
    assert fields["Orbit ID"] == "Unknown"
    assert fields["Neighbours"] == "Perimeter, Maurasi"
    assert contents.title == "Jita — Contents"


async def test_cooldown_error_replies_with_retry_time(cog):
    inter = fake_interaction()
    error = commands.CommandOnCooldown(commands.Cooldown(1, 30), 12.4, commands.BucketType.user)
    await cog.on_cooldown_error(inter, error)
    assert inter.response.send_message.call_args.args[0] == "Slow down — try again in 12s."


async def test_other_errors_propagate(cog):
    with pytest.raises(ValueError):
        await cog.on_cooldown_error(fake_interaction(), ValueError("boom"))


async def test_autocomplete_skips_empty_input():
    assert await eve_lookup.autocomplete_type_name(None, "") == []
    assert await eve_lookup.autocomplete_system_name(None, "") == []
