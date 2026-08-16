import logging

import disnake
from disnake.ext import commands

from fuzzworkbot import sde
from fuzzworkbot.config import load_config

logger = logging.getLogger(__name__)

_config = load_config()


def _fmt_number(value: float | int | None) -> str:
    if value is None:
        return "0"
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


async def autocomplete_type_name(inter: disnake.ApplicationCommandInteraction, user_input: str) -> list[str]:
    if not user_input:
        return []
    return sde.search_type_names(_config.sde_database_url, user_input)


async def autocomplete_system_name(inter: disnake.ApplicationCommandInteraction, user_input: str) -> list[str]:
    if not user_input:
        return []
    return sde.search_system_names(_config.sde_database_url, user_input)


def _build_contents_embed(system_name: str, contents: list[dict]) -> disnake.Embed:
    embed = disnake.Embed(title=f"{system_name} — Contents", description=f"{len(contents)} item(s)")

    by_group: dict[str, list[str]] = {}
    for item in contents:
        by_group.setdefault(item["groupName"] or "Unknown", []).append(item["itemName"])

    # Discord field values cap at 1024 chars — a busy system's full moon/station name
    # list can easily blow past that, so truncate and say how many were left out.
    for group_name, names in sorted(by_group.items()):
        value = ", ".join(names)
        if len(value) > 1000:
            shown = []
            length = 0
            for name in names:
                if length + len(name) + 2 > 950:
                    break
                shown.append(name)
                length += len(name) + 2
            value = ", ".join(shown) + f", … and {len(names) - len(shown)} more"
        embed.add_field(name=f"{group_name} ({len(names)})", value=value, inline=False)

    return embed


class EveLookup(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    @commands.slash_command(guild_ids=[_config.guild_id], description="Look up an EVE item/ship by name.")
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def type(
        self,
        inter: disnake.ApplicationCommandInteraction,
        name: str = commands.Param(autocomplete=autocomplete_type_name),
    ):
        result = sde.lookup_type(_config.sde_database_url, name)
        if result is None:
            await inter.response.send_message(f"No published item/ship named '{name}'.", ephemeral=True)
            return

        embed = disnake.Embed(title=result["typeName"], description=(result["description"] or "")[:2000])
        embed.add_field(name="Group", value=result["groupName"] or "Unknown", inline=True)
        embed.add_field(name="Category", value=result["categoryName"] or "Unknown", inline=True)
        embed.add_field(
            name="Mass / Volume / Cargo",
            value=(
                f"{_fmt_number(result['mass'])} kg / "
                f"{_fmt_number(result['volume'])} m³ / "
                f"{_fmt_number(result['capacity'])} m³"
            ),
            inline=False,
        )

        attrs = result["ship_attributes"]
        if attrs is not None:
            for group_name, attribute_names in sde.SHIP_ATTRIBUTE_GROUPS:
                values = [_fmt_number(attrs.get(a)) for a in attribute_names]
                embed.add_field(name=group_name, value=" / ".join(values), inline=True)

        await inter.response.send_message(embed=embed)

    @commands.slash_command(guild_ids=[_config.guild_id], description="Look up an EVE solar system by name.")
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def system(
        self,
        inter: disnake.ApplicationCommandInteraction,
        name: str = commands.Param(autocomplete=autocomplete_system_name),
    ):
        result = sde.lookup_system(_config.sde_database_url, name)
        if result is None:
            await inter.response.send_message(f"No solar system named '{name}'.", ephemeral=True)
            return

        embed = disnake.Embed(title=f"System: {result['itemName']}")
        for label, key in sde.SYSTEM_DETAIL_FIELDS:
            value = result[key]
            if isinstance(value, float):
                value = f"{value:,.4f}"
            embed.add_field(name=label, value=str(value) if value is not None else "Unknown", inline=True)

        neighbours = sde.get_system_neighbours(_config.sde_database_url, result["solarSystemID"])
        embed.add_field(
            name="Neighbours",
            value=", ".join(neighbours) if neighbours else "None (no stargates)",
            inline=False,
        )

        contents = sde.get_system_contents(_config.sde_database_url, result["itemID"])
        embeds = [embed]
        if contents:
            embeds.append(_build_contents_embed(result["itemName"], contents))

        await inter.response.send_message(embeds=embeds)

    @type.error
    @system.error
    async def on_cooldown_error(self, inter: disnake.ApplicationCommandInteraction, error: commands.CommandInvokeError):
        if isinstance(error, commands.CommandOnCooldown):
            await inter.response.send_message(f"Slow down — try again in {error.retry_after:.0f}s.", ephemeral=True)
            return
        raise error


def setup(bot: commands.InteractionBot):
    bot.add_cog(EveLookup(bot))
