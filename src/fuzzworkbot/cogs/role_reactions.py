import logging

import disnake
from disnake.ext import commands
from sqlalchemy import select

from fuzzworkbot.db import RoleReaction

logger = logging.getLogger(__name__)


def _emoji_key(emoji: disnake.PartialEmoji | disnake.Emoji | str) -> tuple[str, str | None]:
    """(string form, custom-emoji id as str or None) — mappings can be stored either way."""
    text = str(emoji)
    emoji_id = str(getattr(emoji, "id", None)) if getattr(emoji, "id", None) else None
    return text, emoji_id


class RoleReactions(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    def _role_id_for(self, message_id: int, emoji) -> int | None:
        text, emoji_id = _emoji_key(emoji)
        with self.bot.session_factory() as session:
            rows = session.scalars(select(RoleReaction).where(RoleReaction.message_id == message_id)).all()
        for row in rows:
            if row.emoji == text or (emoji_id is not None and row.emoji == emoji_id):
                return row.role_id
        return None

    async def _resolve_member(self, guild: disnake.Guild, user_id: int) -> disnake.Member | None:
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except disnake.NotFound:
            return None

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: disnake.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        role_id = self._role_id_for(payload.message_id, payload.emoji)
        if role_id is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = payload.member or await self._resolve_member(guild, payload.user_id)
        if member is None or member.bot:
            return

        role = guild.get_role(role_id)
        if role is None:
            logger.warning("Role %s not found for reaction role on message %s", role_id, payload.message_id)
            return

        try:
            await member.add_roles(role)
        except disnake.HTTPException as e:
            logger.warning("Could not add role %s to %s: %s", role_id, payload.user_id, e)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: disnake.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        role_id = self._role_id_for(payload.message_id, payload.emoji)
        if role_id is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = await self._resolve_member(guild, payload.user_id)
        if member is None or member.bot:
            return

        role = guild.get_role(role_id)
        if role is None:
            return

        try:
            await member.remove_roles(role)
        except disnake.HTTPException as e:
            logger.warning("Could not remove role %s from %s: %s", role_id, payload.user_id, e)

    @commands.Cog.listener()
    async def on_ready(self):
        await self._reconcile()

    async def _reconcile(self):
        with self.bot.session_factory() as session:
            message_ids = session.scalars(select(RoleReaction.message_id).distinct()).all()
        for message_id in message_ids:
            await self._reconcile_message(message_id)

    async def _reconcile_message(self, message_id: int):
        with self.bot.session_factory() as session:
            mappings = session.scalars(select(RoleReaction).where(RoleReaction.message_id == message_id)).all()
        if not mappings:
            return

        guild = self.bot.get_guild(mappings[0].guild_id)
        if guild is None:
            logger.warning("Guild %s not found while reconciling role reactions", mappings[0].guild_id)
            return
        channel = guild.get_channel(mappings[0].channel_id)
        if channel is None:
            logger.warning("Channel %s not found while reconciling role reactions", mappings[0].channel_id)
            return

        try:
            message = await channel.fetch_message(message_id)
        except disnake.NotFound:
            logger.warning("Role-reaction message %s not found while reconciling", message_id)
            return

        by_key = {}
        for mapping in mappings:
            by_key[mapping.emoji] = mapping.role_id

        for reaction in message.reactions:
            text, emoji_id = _emoji_key(reaction.emoji)
            role_id = by_key.get(text) or (by_key.get(emoji_id) if emoji_id else None)
            if role_id is None:
                continue
            role = guild.get_role(role_id)
            if role is None:
                continue
            async for user in reaction.users():
                if user.bot:
                    continue
                member = await self._resolve_member(guild, user.id)
                if member is None or role in member.roles:
                    continue
                try:
                    await member.add_roles(role)
                except disnake.HTTPException as e:
                    logger.warning("Could not reconcile role %s for %s: %s", role_id, user.id, e)


def setup(bot: commands.InteractionBot):
    bot.add_cog(RoleReactions(bot))
