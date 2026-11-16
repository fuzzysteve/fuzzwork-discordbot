import datetime
import logging
import random

import disnake
from disnake import OptionChoice
from disnake.ext import commands, tasks
from sqlalchemy import select

from fuzzworkbot.config import load_config
from fuzzworkbot.db import STATUS_ACTIVE, STATUS_FINISHED, STATUS_PENDING, Giveaway, GiveawayCode, GiveawayWinner, Prize, now_utc
from fuzzworkbot.giveaway_logic import available_code_count, clear_next_giveaway, get_next_giveaway, set_next_giveaway

logger = logging.getLogger(__name__)

_config = load_config()


async def autocomplete_prizes(inter: disnake.ApplicationCommandInteraction, user_input: str) -> list[OptionChoice]:
    session_factory = inter.bot.session_factory
    user_input_lower = user_input.lower()
    choices = []
    with session_factory() as session:
        prizes = session.scalars(select(Prize).where(Prize.active.is_(True))).all()
        for prize_row in prizes:
            if user_input_lower not in prize_row.name.lower():
                continue
            available = available_code_count(session, prize_row.id)
            if available <= 0:
                continue
            choices.append(OptionChoice(name=f"{prize_row.name} ({available} available)", value=prize_row.name))
    return choices[:25]


class Giveaways(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self.poll.start()

    def cog_unload(self):
        self.poll.cancel()

    @commands.slash_command(
        guild_ids=[_config.guild_id],
        description="Creates a new Giveaway!",
        default_member_permissions=disnake.Permissions(manage_guild=True),
    )
    async def creategiveaway(
        self,
        inter: disnake.ApplicationCommandInteraction,
        prize: str = commands.Param(autocomplete=autocomplete_prizes),
        duration_hours: int = commands.Param(gt=0),
        winners: int = commands.Param(default=1, gt=0),
        channel: disnake.TextChannel = commands.Param(default=None),
    ):
        with self.bot.session_factory.begin() as session:
            # Lock the prize row so two concurrent /creategiveaway calls for the same
            # prize can't both read the same "available" number and both succeed.
            prize_row = session.scalar(
                select(Prize).where(Prize.name == prize, Prize.active.is_(True)).with_for_update()
            )
            if prize_row is None:
                await inter.response.send_message(f"No active prize named '{prize}'.", ephemeral=True)
                return

            available = available_code_count(session, prize_row.id)
            if winners > available:
                await inter.response.send_message(
                    f"Can't draw {winners} winner(s) for '{prize}' — only {available} code(s) "
                    "available right now (accounting for codes already promised to other "
                    "pending/running giveaways for this prize).",
                    ephemeral=True,
                )
                return

            giveaway = Giveaway(
                prize_id=prize_row.id,
                guild_id=inter.guild_id,
                channel_id=channel.id if channel else inter.channel_id,
                created_by_discord_id=inter.author.id,
                duration_hours=duration_hours,
                winner_count=winners,
                status=STATUS_PENDING,
            )
            session.add(giveaway)

        await inter.response.send_message(
            f"Giveaway for '{prize}' ({winners} winner(s)) queued. It will post within about a minute.",
            ephemeral=True,
        )

    @commands.slash_command(
        guild_ids=[_config.guild_id],
        description="Choose the prize for the next automatic (timer) giveaway.",
        default_member_permissions=disnake.Permissions(manage_guild=True),
    )
    async def nextgiveaway(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @nextgiveaway.sub_command(name="set", description="Set the prize the next automatic giveaway will use.")
    async def nextgiveaway_set(
        self,
        inter: disnake.ApplicationCommandInteraction,
        prize: str = commands.Param(autocomplete=autocomplete_prizes),
        winners: int = commands.Param(default=None, gt=0, description="Omit to use the timer's default (1)."),
    ):
        with self.bot.session_factory.begin() as session:
            prize_row = session.scalar(select(Prize).where(Prize.name == prize).with_for_update())
            if prize_row is None:
                await inter.response.send_message(f"No prize named '{prize}'.", ephemeral=True)
                return
            error = set_next_giveaway(session, prize_row, winners, set_by_discord_id=inter.author.id)
        if error:
            await inter.response.send_message(f"Not set: {error}", ephemeral=True)
            return
        winners_text = f"{winners} winner(s)" if winners else "the default winner count"
        await inter.response.send_message(
            f"The next automatic giveaway will be **{prize}** ({winners_text}).", ephemeral=True
        )

    @nextgiveaway.sub_command(name="show", description="Show the chosen next automatic giveaway, if any.")
    async def nextgiveaway_show(self, inter: disnake.ApplicationCommandInteraction):
        with self.bot.session_factory() as session:
            row = get_next_giveaway(session)
            if row is None:
                message = "No next giveaway chosen — the next automatic one will be a random prize."
            else:
                winners_text = f"{row.winner_count} winner(s)" if row.winner_count else "the default winner count"
                message = f"The next automatic giveaway will be **{row.prize.name}** ({winners_text})."
        await inter.response.send_message(message, ephemeral=True)

    @nextgiveaway.sub_command(name="clear", description="Go back to a random prize for the next automatic giveaway.")
    async def nextgiveaway_clear(self, inter: disnake.ApplicationCommandInteraction):
        with self.bot.session_factory.begin() as session:
            had_one = clear_next_giveaway(session)
        await inter.response.send_message(
            "Cleared — the next automatic giveaway will be a random prize." if had_one else "No next giveaway was set.",
            ephemeral=True,
        )

    @tasks.loop(minutes=1)
    async def poll(self):
        # disnake's tasks.loop only survives network-level errors — anything else
        # (a MySQL disconnect, a Discord 403/5xx) ends the loop for good while the bot
        # itself stays up. That happened in Nov 2026 and stalled every giveaway for
        # ~2.5 days, so nothing is allowed to escape from here.
        for step in (self._post_pending, self._finish_active):
            try:
                await step()
            except Exception:
                logger.exception("Giveaway poller step %s failed; retrying next minute", step.__name__)

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    async def _post_pending(self):
        with self.bot.session_factory() as session:
            pending_ids = session.scalars(select(Giveaway.id).where(Giveaway.status == STATUS_PENDING)).all()

        for giveaway_id in pending_ids:
            try:
                await self._post_one(giveaway_id)
            except Exception:
                logger.exception("Posting giveaway %s failed; will retry", giveaway_id)

    async def _post_one(self, giveaway_id: int):
        with self.bot.session_factory.begin() as session:
            giveaway = session.get(Giveaway, giveaway_id)
            if giveaway is None or giveaway.status != STATUS_PENDING:
                return
            prize = session.get(Prize, giveaway.prize_id)
            channel = self.bot.get_channel(giveaway.channel_id)
            if channel is None:
                logger.warning("Channel %s not found for giveaway %s", giveaway.channel_id, giveaway.id)
                return

            now = now_utc()
            ends_at = now + datetime.timedelta(hours=giveaway.duration_hours)
            embed = disnake.Embed(
                title="Giveaway!",
                description="This is a giveaway! React with any reaction to win the prize!",
                timestamp=now,
            )
            embed.add_field(name="Prize", value=prize.name, inline=False)
            embed.add_field(name="Winners", value=str(giveaway.winner_count), inline=False)
            embed.add_field(name="Ends At (UTC)", value=ends_at.strftime("%Y-%m-%d %H:%M UTC"), inline=False)
            message = await channel.send(embed=embed)

            giveaway.message_id = message.id
            giveaway.status = STATUS_ACTIVE
            giveaway.starts_at = now
            giveaway.ends_at = ends_at

    async def _finish_active(self):
        now = now_utc()
        with self.bot.session_factory() as session:
            ended_ids = session.scalars(
                select(Giveaway.id).where(Giveaway.status == STATUS_ACTIVE, Giveaway.ends_at < now)
            ).all()

        for giveaway_id in ended_ids:
            try:
                await self._finish_one(giveaway_id)
            except Exception:
                logger.exception("Finishing giveaway %s failed; will retry", giveaway_id)

    def _mark_finished(self, giveaway_id: int):
        with self.bot.session_factory.begin() as session:
            giveaway = session.get(Giveaway, giveaway_id)
            if giveaway is not None:
                giveaway.status = STATUS_FINISHED

    async def _finish_one(self, giveaway_id: int):
        """Three phases, so a Discord failure can never undo (or repeat) a draw whose
        codes have already been DMed: (1) gather entrants from Discord with no DB
        transaction open; (2) draw winners, claim codes and mark the giveaway finished
        in one committed transaction; (3) only then DM winners and announce. A failure
        in phase 3 is logged, never retried — the winners and codes are already on
        record for `giveaway-cli giveaway winners`."""
        # Phase 1: read state, talk to Discord, no locks held.
        with self.bot.session_factory() as session:
            giveaway = session.get(Giveaway, giveaway_id)
            if giveaway is None or giveaway.status != STATUS_ACTIVE:
                return
            guild_id, channel_id, message_id = giveaway.guild_id, giveaway.channel_id, giveaway.message_id
            prize_name = session.get(Prize, giveaway.prize_id).name

        guild = self.bot.get_guild(guild_id)
        channel = (guild.get_channel(channel_id) if guild else None) or self.bot.get_channel(channel_id)
        if channel is None:
            logger.warning("Channel missing for giveaway %s", giveaway_id)
            self._mark_finished(giveaway_id)
            return

        try:
            message = await channel.fetch_message(message_id)
        except disnake.NotFound:
            logger.warning("Giveaway message %s missing", message_id)
            self._mark_finished(giveaway_id)
            return

        entrants: dict[int, disnake.User] = {}
        for reaction in message.reactions:
            async for user in reaction.users():
                if not user.bot:
                    entrants[user.id] = user

        # Phase 2: the draw, committed before anyone is told anything.
        awards: list[tuple[disnake.User, str | None]] = []
        with self.bot.session_factory.begin() as session:
            giveaway = session.scalar(select(Giveaway).where(Giveaway.id == giveaway_id).with_for_update())
            if giveaway is None or giveaway.status != STATUS_ACTIVE:
                return
            winner_count = giveaway.winner_count
            for winner in random.sample(list(entrants.values()), min(winner_count, len(entrants))):
                code_row = session.scalar(
                    select(GiveawayCode)
                    .where(GiveawayCode.prize_id == giveaway.prize_id, GiveawayCode.assigned_at.is_(None))
                    .with_for_update()
                    .limit(1)
                )
                if code_row is None:
                    # Shouldn't happen — every creation path checks capacity up front —
                    # but handled defensively regardless.
                    logger.error("No unused code left for prize %s (giveaway %s)", prize_name, giveaway_id)
                    session.add(GiveawayWinner(giveaway_id=giveaway_id, discord_id=winner.id, drawn_at=now_utc()))
                    awards.append((winner, None))
                    continue
                code_row.assigned_to_discord_id = winner.id
                code_row.assigned_at = now_utc()
                session.add(
                    GiveawayWinner(giveaway_id=giveaway_id, discord_id=winner.id, code_id=code_row.id, drawn_at=now_utc())
                )
                awards.append((winner, code_row.code))
            giveaway.status = STATUS_FINISHED

        # Phase 3: notify. Best effort only — see docstring.
        if not awards:
            embed = disnake.Embed(title="Giveaway Ended", description=f"No one entered the giveaway for **{prize_name}**.")
        else:
            winner_lines = []
            for winner, code in awards:
                if code is None:
                    winner_lines.append(f"{winner.mention} — no code left! An admin needs to deliver one manually.")
                    continue
                try:
                    await winner.send(f"You won **{prize_name}**! Your code: `{code}`")
                    winner_lines.append(winner.mention)
                except disnake.HTTPException as e:
                    logger.warning("Could not DM winner %s (giveaway %s): %s", winner.id, giveaway_id, e)
                    winner_lines.append(f"{winner.mention} (couldn't DM you — an admin needs to deliver the code manually)")

            embed = disnake.Embed(
                title="Giveaway Winner!" if len(awards) == 1 else "Giveaway Winners!",
                description="\n".join(winner_lines),
            )
            embed.add_field(name="Prize", value=prize_name, inline=False)
            if len(awards) < winner_count:
                embed.add_field(
                    name="Note",
                    value=f"Only {len(entrants)} entrant(s) for {winner_count} requested winner(s).",
                    inline=False,
                )
        try:
            await channel.send(embed=embed)
        except disnake.HTTPException:
            logger.exception(
                "Giveaway %s was drawn but the announcement failed to post — winners/codes are "
                "recorded, see `giveaway-cli giveaway winners %s`",
                giveaway_id,
                giveaway_id,
            )


def setup(bot: commands.InteractionBot):
    bot.add_cog(Giveaways(bot))
