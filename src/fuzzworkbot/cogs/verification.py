import asyncio
import logging

import disnake
from disnake.ext import commands
from sqlalchemy import select

from fuzzworkbot.config import load_config
from fuzzworkbot.db import UserLookup
from fuzzworkbot.esi import get_character_name

logger = logging.getLogger(__name__)

_config = load_config()

NOT_LINKED_MESSAGE = (
    "Auth system doesn't know you yet. Please go to https://www.fuzzwork.co.uk/discord-auth/"
)
SYSTEM_ERROR_MESSAGE = "There's a problem with the system. Sorry. Maybe bug steve"


async def _character_name(eveid: int) -> str:
    # The ESI call blocks (requests), so keep it off the event loop — otherwise a slow
    # ESI stalls every command, the giveaway poller and the gateway heartbeat.
    return await asyncio.to_thread(get_character_name, eveid, _config.esi_user_agent)


class Verification(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    def _lookup_eveid(self, discord_id: int) -> int | None:
        with self.bot.session_factory() as session:
            row = session.scalar(select(UserLookup).where(UserLookup.discordid == discord_id))
            return row.eveid if row else None

    @commands.slash_command(guild_ids=[_config.guild_id])
    async def authme(self, inter: disnake.ApplicationCommandInteraction):
        # ESI can take longer than Discord's 3s interaction deadline; defer first and
        # answer via edit_original_response.
        await inter.response.defer()
        try:
            eveid = self._lookup_eveid(inter.author.id)
            if eveid is None:
                await inter.edit_original_response(NOT_LINKED_MESSAGE)
                return

            name = await _character_name(eveid)

            try:
                if inter.guild is not None:
                    role = inter.guild.get_role(_config.validated_role_id)
                    if role is not None:
                        await inter.author.add_roles(role)
                        # EVE names can be up to 37 chars; Discord nicknames max out at 32.
                        await inter.author.edit(nick=name[:32])
            except disnake.HTTPException as e:
                logger.warning("Can't set role or nick for %s: %s", inter.author.id, e)

            await inter.edit_original_response(f"You are {name} in Eve")
        except Exception:
            logger.exception("authme failed for %s", inter.author.id)
            await inter.edit_original_response(SYSTEM_ERROR_MESSAGE)

    @commands.slash_command(guild_ids=[_config.guild_id])
    async def auththem(self, inter: disnake.ApplicationCommandInteraction, user: disnake.User):
        await inter.response.defer()
        try:
            eveid = self._lookup_eveid(user.id)
            if eveid is None:
                await inter.edit_original_response(f"Auth system doesn't know {user.name} yet.")
                return

            name = await _character_name(eveid)
            await inter.edit_original_response(f"{user.mention} is {name} in Eve")
        except Exception:
            logger.exception("auththem failed for %s", user.id)
            await inter.edit_original_response(SYSTEM_ERROR_MESSAGE)


def setup(bot: commands.InteractionBot):
    bot.add_cog(Verification(bot))
