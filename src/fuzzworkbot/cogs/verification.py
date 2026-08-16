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


class Verification(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    def _lookup_eveid(self, discord_id: int) -> int | None:
        with self.bot.session_factory() as session:
            row = session.scalar(select(UserLookup).where(UserLookup.discordid == discord_id))
            return row.eveid if row else None

    @commands.slash_command(guild_ids=[_config.guild_id])
    async def authme(self, inter: disnake.ApplicationCommandInteraction):
        try:
            eveid = self._lookup_eveid(inter.author.id)
            if eveid is None:
                await inter.response.send_message(NOT_LINKED_MESSAGE)
                return

            name = get_character_name(eveid, _config.esi_user_agent)

            try:
                if inter.guild is not None:
                    role = inter.guild.get_role(_config.validated_role_id)
                    if role is not None:
                        await inter.author.add_roles(role)
                        await inter.author.edit(nick=name)
            except disnake.HTTPException as e:
                logger.warning("Can't set role or nick for %s: %s", inter.author.id, e)

            await inter.response.send_message(f"You are {name} in Eve")
        except Exception:
            logger.exception("authme failed for %s", inter.author.id)
            await inter.response.send_message("There's a problem with the system. Sorry. Maybe bug steve")

    @commands.slash_command(guild_ids=[_config.guild_id])
    async def auththem(self, inter: disnake.ApplicationCommandInteraction, user: disnake.User):
        try:
            eveid = self._lookup_eveid(user.id)
            if eveid is None:
                await inter.response.send_message(f"Auth system doesn't know {user.name} yet.")
                return

            name = get_character_name(eveid, _config.esi_user_agent)
            await inter.response.send_message(f"{user.mention} is {name} in Eve")
        except Exception:
            logger.exception("auththem failed for %s", user.id)
            await inter.response.send_message("There's a problem with the system. Sorry. Maybe bug steve")


def setup(bot: commands.InteractionBot):
    bot.add_cog(Verification(bot))
