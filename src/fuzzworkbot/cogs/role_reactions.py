import logging
import re

import disnake
from disnake import OptionChoice
from disnake.ext import commands
from sqlalchemy import select

from fuzzworkbot.config import load_config
from fuzzworkbot.db import RoleMenu, RoleReaction

_config = load_config()

# "Copy Message Link": https://discord.com/channels/<guild>/<channel>/<message>
CUSTOM_EMOJI_RE = re.compile(r"^<(a?):(\w+):(\d+)>$")
MESSAGE_LINK_RE = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)/?$")

logger = logging.getLogger(__name__)


def _emoji_key(emoji: disnake.PartialEmoji | disnake.Emoji | str) -> tuple[str, str | None]:
    """(string form, custom-emoji id as str or None) — mappings can be stored either way."""
    text = str(emoji)
    emoji_id = str(getattr(emoji, "id", None)) if getattr(emoji, "id", None) else None
    return text, emoji_id


def _stored_emoji(raw: str) -> tuple[str, disnake.PartialEmoji | str]:
    """(value stored in role_reactions.emoji, what to pass to add_reaction). Custom emoji
    are stored by numeric ID, unicode emoji as themselves — the same forms the CLI uses
    and _emoji_key matches."""
    raw = raw.strip()
    custom = CUSTOM_EMOJI_RE.match(raw)
    if custom:
        animated, name, emoji_id = custom.groups()
        return emoji_id, disnake.PartialEmoji(name=name, id=int(emoji_id), animated=bool(animated))
    if raw.isdigit():  # bare custom-emoji ID, as the CLI accepts
        return raw, disnake.PartialEmoji(name="_", id=int(raw))
    return raw, raw


async def autocomplete_menus(inter: disnake.ApplicationCommandInteraction, user_input: str) -> list[OptionChoice]:
    """Menus the bot posted (by title), then other messages that already have mappings."""
    with inter.bot.session_factory() as session:
        menus = session.scalars(select(RoleMenu).where(RoleMenu.guild_id == inter.guild_id)).all()
        mapped = session.scalars(
            select(RoleReaction.message_id).where(RoleReaction.guild_id == inter.guild_id).distinct()
        ).all()
    titled = {m.message_id: m.title for m in menus}
    choices = [OptionChoice(name=title[:100], value=str(mid)) for mid, title in titled.items()]
    choices += [OptionChoice(name=f"Message {mid}", value=str(mid)) for mid in mapped if mid not in titled]
    needle = user_input.lower()
    return [c for c in choices if needle in c.name.lower() or needle in c.value][:25]


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
            # One unreadable channel/message (403, Discord 5xx) mustn't stop the rest.
            try:
                await self._reconcile_message(message_id)
            except Exception:
                logger.exception("Reconciling role reactions for message %s failed", message_id)

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
        except disnake.Forbidden:
            logger.warning("No access to role-reaction message %s (channel %s) while reconciling", message_id, channel.id)
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


    # --- /rolemenu ---------------------------------------------------------------

    def _role_problem(self, inter: disnake.ApplicationCommandInteraction, role: disnake.Role) -> str | None:
        """Why `role` can't go on a menu set up by this user, or None. Stops someone with
        Manage Roles from mapping a role above their own and then reacting to grant it to
        themselves."""
        guild = inter.guild
        if role.is_default():
            return "@everyone can't be handed out."
        if role.managed:
            return f"{role.mention} is managed by an integration or bot and can't be handed out."
        if not guild.me.guild_permissions.manage_roles:
            return "I need the Manage Roles permission first."
        if role >= guild.me.top_role:
            return f"{role.mention} is at or above my highest role, so I can't hand it out. Move my role above it in Server Settings → Roles."
        if inter.author.id != guild.owner_id and role >= inter.author.top_role:
            return f"You can only put roles below your own highest role on a menu, and {role.mention} isn't."
        return None

    def _resolve_menu(self, guild_id: int, raw: str) -> tuple[int, int] | str:
        """(channel_id, message_id) for a message link or a known menu's message ID, or an
        error message."""
        raw = raw.strip()
        link = MESSAGE_LINK_RE.match(raw)
        if link:
            link_guild, channel_id, message_id = (int(g) for g in link.groups())
            if link_guild != guild_id:
                return "That message link is from a different server."
            return channel_id, message_id
        if not raw.isdigit():
            return "Pick a menu from the list, or paste the message's link (right-click → Copy Message Link)."
        message_id = int(raw)
        with self.bot.session_factory() as session:
            menu = session.get(RoleMenu, message_id)
            if menu is not None:
                return menu.channel_id, message_id
            mapping = session.scalar(select(RoleReaction).where(RoleReaction.message_id == message_id).limit(1))
            if mapping is not None:
                return mapping.channel_id, message_id
        return "I don't know that message. Paste its link instead (right-click → Copy Message Link)."

    def _display_emoji(self, stored: str) -> str:
        if stored.isdigit():
            emoji = self.bot.get_emoji(int(stored))
            return str(emoji) if emoji else f"(emoji {stored})"
        return stored

    def _menu_embed(self, menu: RoleMenu, mappings: list[RoleReaction]) -> disnake.Embed:
        lines = [f"{self._display_emoji(m.emoji)} — <@&{m.role_id}>" for m in mappings]
        body = "\n".join(lines) if lines else "*No roles yet.*"
        intro = f"{menu.description}\n\n" if menu.description else ""
        return disnake.Embed(title=menu.title, description=f"{intro}{body}\n\nReact to get a role; remove your reaction to drop it.")

    async def _refresh_menu(self, channel: disnake.abc.Messageable, message_id: int) -> None:
        """Re-render the role list on a menu the bot posted. No-op for other messages."""
        with self.bot.session_factory() as session:
            menu = session.get(RoleMenu, message_id)
            if menu is None:
                return
            mappings = session.scalars(
                select(RoleReaction).where(RoleReaction.message_id == message_id).order_by(RoleReaction.id)
            ).all()
            embed = self._menu_embed(menu, mappings)
        await channel.get_partial_message(message_id).edit(embed=embed)

    @commands.slash_command(
        guild_ids=[_config.guild_id],
        description="Create and manage reaction role menus.",
        default_member_permissions=disnake.Permissions(manage_roles=True),
    )
    async def rolemenu(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @rolemenu.sub_command(name="create", description="Post a new, empty role menu. Then add roles with /rolemenu add.")
    async def rolemenu_create(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: disnake.TextChannel,
        title: str = commands.Param(max_length=256),
        description: str = commands.Param(default=None, max_length=1500),
    ):
        menu = RoleMenu(
            message_id=0, guild_id=inter.guild_id, channel_id=channel.id, title=title, description=description,
            created_by_discord_id=inter.author.id,
        )
        try:
            message = await channel.send(embed=self._menu_embed(menu, []))
        except disnake.HTTPException as e:
            await inter.response.send_message(f"Couldn't post in {channel.mention}: {e.text or e}", ephemeral=True)
            return
        menu.message_id = message.id
        with self.bot.session_factory.begin() as session:
            session.add(menu)
        await inter.response.send_message(
            f"Menu posted in {channel.mention}. Add roles with `/rolemenu add`, picking **{title}**.", ephemeral=True
        )

    @rolemenu.sub_command(name="add", description="Add (or change) an emoji → role on a menu.")
    async def rolemenu_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        menu: str = commands.Param(autocomplete=autocomplete_menus, description="A menu, or any message's link."),
        emoji: str = commands.Param(description="A unicode emoji, or a custom emoji from this server."),
        role: disnake.Role = commands.Param(),
    ):
        target = self._resolve_menu(inter.guild_id, menu)
        if isinstance(target, str):
            await inter.response.send_message(target, ephemeral=True)
            return
        channel_id, message_id = target
        problem = self._role_problem(inter, role)
        if problem:
            await inter.response.send_message(problem, ephemeral=True)
            return

        channel = inter.guild.get_channel(channel_id)
        if channel is None:
            await inter.response.send_message("I can't see that message's channel.", ephemeral=True)
            return
        stored, reaction = _stored_emoji(emoji)
        try:
            message = await channel.fetch_message(message_id)
            # Reacting first doubles as validation: Discord rejects anything that isn't a
            # single emoji the bot can use, and nothing gets stored.
            await message.add_reaction(reaction)
        except disnake.NotFound:
            await inter.response.send_message("That message doesn't exist any more.", ephemeral=True)
            return
        except disnake.HTTPException as e:
            await inter.response.send_message(
                f"Discord wouldn't add that reaction ({e.text or e}). Use one emoji, either unicode or a custom emoji from this server.",
                ephemeral=True,
            )
            return

        with self.bot.session_factory.begin() as session:
            existing = session.scalar(
                select(RoleReaction).where(RoleReaction.message_id == message_id, RoleReaction.emoji == stored)
            )
            if existing:
                existing.role_id = role.id
            else:
                session.add(
                    RoleReaction(
                        guild_id=inter.guild_id, channel_id=channel_id, message_id=message_id, emoji=stored, role_id=role.id
                    )
                )
        await self._refresh_menu(channel, message_id)
        verb = "Changed" if existing else "Added"
        await inter.response.send_message(f"{verb}: {self._display_emoji(stored)} → {role.mention}.", ephemeral=True)

    @rolemenu.sub_command(name="remove", description="Remove an emoji from a menu.")
    async def rolemenu_remove(
        self,
        inter: disnake.ApplicationCommandInteraction,
        menu: str = commands.Param(autocomplete=autocomplete_menus, description="A menu, or any message's link."),
        emoji: str = commands.Param(description="The emoji to remove."),
    ):
        target = self._resolve_menu(inter.guild_id, menu)
        if isinstance(target, str):
            await inter.response.send_message(target, ephemeral=True)
            return
        channel_id, message_id = target
        stored, reaction = _stored_emoji(emoji)
        with self.bot.session_factory.begin() as session:
            existing = session.scalar(
                select(RoleReaction).where(RoleReaction.message_id == message_id, RoleReaction.emoji == stored)
            )
            if existing is not None:
                session.delete(existing)
        if existing is None:
            await inter.response.send_message("That emoji isn't on that menu.", ephemeral=True)
            return

        channel = inter.guild.get_channel(channel_id)
        if channel is not None:
            try:
                await channel.get_partial_message(message_id).remove_reaction(reaction, inter.guild.me)
                await self._refresh_menu(channel, message_id)
            except disnake.HTTPException as e:
                logger.warning("Removed mapping but couldn't tidy message %s: %s", message_id, e)
        await inter.response.send_message(
            f"Removed {self._display_emoji(stored)}. Members who already have the role keep it.", ephemeral=True
        )

    @rolemenu.sub_command(name="list", description="List every role menu and its emoji → role mappings.")
    async def rolemenu_list(self, inter: disnake.ApplicationCommandInteraction):
        with self.bot.session_factory() as session:
            rows = session.scalars(
                select(RoleReaction).where(RoleReaction.guild_id == inter.guild_id).order_by(RoleReaction.message_id, RoleReaction.id)
            ).all()
            titles = {m.message_id: m.title for m in session.scalars(select(RoleMenu).where(RoleMenu.guild_id == inter.guild_id))}
        if not rows and not titles:
            await inter.response.send_message("No role menus yet. Start one with `/rolemenu create`.", ephemeral=True)
            return
        by_message: dict[int, list[RoleReaction]] = {mid: [] for mid in titles}
        channels: dict[int, int] = {}
        for row in rows:
            by_message.setdefault(row.message_id, []).append(row)
            channels[row.message_id] = row.channel_id
        with self.bot.session_factory() as session:
            for mid in titles:
                channels.setdefault(mid, session.get(RoleMenu, mid).channel_id)
        blocks = []
        for mid, mappings in by_message.items():
            link = f"https://discord.com/channels/{inter.guild_id}/{channels[mid]}/{mid}"
            header = f"**{titles.get(mid, f'Message {mid}')}** — {link}"
            lines = [f"  {self._display_emoji(m.emoji)} → <@&{m.role_id}>" for m in mappings] or ["  *(no roles)*"]
            blocks.append("\n".join([header, *lines]))
        text = "\n\n".join(blocks)
        if len(text) > 2000:
            text = text[:1980] + "\n…(truncated)"
        await inter.response.send_message(text, ephemeral=True, allowed_mentions=disnake.AllowedMentions.none())


def setup(bot: commands.InteractionBot):
    bot.add_cog(RoleReactions(bot))
