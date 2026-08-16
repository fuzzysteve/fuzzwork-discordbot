import datetime
import logging
import random

import disnake
from disnake import OptionChoice
from disnake.ext import commands, tasks
from sqlalchemy import select

from fuzzworkbot.config import load_config
from fuzzworkbot.db import STATUS_ACTIVE, STATUS_FINISHED, STATUS_PENDING, Giveaway, GiveawayCode, GiveawayWinner, Prize, now_utc
from fuzzworkbot.giveaway_logic import available_code_count

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

    @tasks.loop(minutes=1)
    async def poll(self):
        await self._post_pending()
        await self._finish_active()

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    async def _post_pending(self):
        with self.bot.session_factory() as session:
            pending_ids = session.scalars(select(Giveaway.id).where(Giveaway.status == STATUS_PENDING)).all()

        for giveaway_id in pending_ids:
            await self._post_one(giveaway_id)

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
            await self._finish_one(giveaway_id)

    async def _finish_one(self, giveaway_id: int):
        with self.bot.session_factory.begin() as session:
            giveaway = session.get(Giveaway, giveaway_id)
            if giveaway is None or giveaway.status != STATUS_ACTIVE:
                return
            prize = session.get(Prize, giveaway.prize_id)

            guild = self.bot.get_guild(giveaway.guild_id)
            channel = (guild.get_channel(giveaway.channel_id) if guild else None) or self.bot.get_channel(
                giveaway.channel_id
            )
            if channel is None:
                logger.warning("Channel missing for giveaway %s", giveaway.id)
                giveaway.status = STATUS_FINISHED
                return

            try:
                message = await channel.fetch_message(giveaway.message_id)
            except disnake.NotFound:
                logger.warning("Giveaway message %s missing", giveaway.message_id)
                giveaway.status = STATUS_FINISHED
                return

            entrants: dict[int, disnake.User] = {}
            for reaction in message.reactions:
                async for user in reaction.users():
                    if not user.bot:
                        entrants[user.id] = user

            if not entrants:
                await channel.send(
                    embed=disnake.Embed(
                        title="Giveaway Ended",
                        description=f"No one entered the giveaway for **{prize.name}**.",
                    )
                )
                giveaway.status = STATUS_FINISHED
                return

            pool = list(entrants.values())
            num_winners = min(giveaway.winner_count, len(pool))
            winners = random.sample(pool, num_winners)

            winner_lines = []
            for winner in winners:
                code_row = session.scalar(
                    select(GiveawayCode)
                    .where(
                        GiveawayCode.prize_id == giveaway.prize_id,
                        GiveawayCode.assigned_to_discord_id.is_(None),
                    )
                    .with_for_update()
                    .limit(1)
                )

                if code_row is None:
                    # Shouldn't happen — /creategiveaway and the CLI both check
                    # capacity up front — but handled defensively regardless.
                    logger.error("No unused code left for prize %s (giveaway %s)", prize.name, giveaway.id)
                    session.add(GiveawayWinner(giveaway_id=giveaway.id, discord_id=winner.id, drawn_at=now_utc()))
                    winner_lines.append(f"{winner.mention} — no code left! An admin needs to deliver one manually.")
                    continue

                code_row.assigned_to_discord_id = winner.id
                code_row.assigned_at = now_utc()
                session.add(
                    GiveawayWinner(
                        giveaway_id=giveaway.id, discord_id=winner.id, code_id=code_row.id, drawn_at=now_utc()
                    )
                )

                try:
                    await winner.send(f"You won **{prize.name}**! Your code: `{code_row.code}`")
                    winner_lines.append(f"{winner.mention}")
                except disnake.Forbidden:
                    logger.warning("Could not DM winner %s, code held for manual delivery", winner.id)
                    winner_lines.append(f"{winner.mention} (DMs closed — an admin needs to deliver the code manually)")

            embed = disnake.Embed(
                title="Giveaway Winner!" if num_winners == 1 else "Giveaway Winners!",
                description="\n".join(winner_lines),
            )
            embed.add_field(name="Prize", value=prize.name, inline=False)
            if num_winners < giveaway.winner_count:
                embed.add_field(
                    name="Note",
                    value=f"Only {len(pool)} entrant(s) for {giveaway.winner_count} requested winner(s).",
                    inline=False,
                )
            await channel.send(embed=embed)

            giveaway.status = STATUS_FINISHED


def setup(bot: commands.InteractionBot):
    bot.add_cog(Giveaways(bot))
