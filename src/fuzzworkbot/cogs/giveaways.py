import calendar
import datetime
import logging
import random
import sys

import disnake
from disnake import OptionChoice
from disnake.ext import commands, tasks
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from fuzzworkbot import watchdog
from fuzzworkbot.code_import import CodeFileError, import_codes, parse_code_csv, parse_code_lines
from fuzzworkbot.config import load_config
from fuzzworkbot.db import (
    ENTRY_BUTTON,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_FINISHED,
    STATUS_PENDING,
    Giveaway,
    GiveawayCode,
    GiveawayEntry,
    GiveawayWinner,
    Prize,
    now_utc,
)
from fuzzworkbot.giveaway_logic import (
    available_code_count,
    clear_giveaway_queue,
    committed_winner_count,
    unused_code_count,
    describe_queued,
    giveaway_queue,
    parse_start_time,
    queue_giveaway,
    remove_queued_giveaway,
)

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


async def autocomplete_queue(inter: disnake.ApplicationCommandInteraction, user_input: str) -> list[OptionChoice]:
    with inter.bot.session_factory() as session:
        rows = giveaway_queue(session)
        labels = [(describe_queued(row), row.id) for row in rows]
    return [OptionChoice(name=label, value=str(queue_id)) for label, queue_id in labels if user_input.lower() in label.lower()][:25]


async def autocomplete_open_giveaways(inter: disnake.ApplicationCommandInteraction, user_input: str) -> list[OptionChoice]:
    """Pending (including scheduled) and active giveaways, newest first."""
    with inter.bot.session_factory() as session:
        rows = session.execute(
            select(Giveaway.id, Giveaway.status, Giveaway.starts_at, Giveaway.ends_at, Prize.name)
            .join(Prize, Prize.id == Giveaway.prize_id)
            .where(Giveaway.status.in_((STATUS_PENDING, STATUS_ACTIVE)))
            .order_by(Giveaway.id.desc())
        ).all()
    choices = []
    for row in rows:
        g = row._mapping
        if g["status"] == STATUS_ACTIVE:
            when = f"running, ends {g['ends_at']:%Y-%m-%d %H:%M} UTC"
        elif g["starts_at"] is not None:
            when = f"scheduled for {g['starts_at']:%Y-%m-%d %H:%M} UTC"
        else:
            when = "about to post"
        label = f"#{g['id']} {g['name']} ({when})"[:100]
        if user_input.lower() in label.lower():
            choices.append(OptionChoice(name=label, value=str(g["id"])))
    return choices[:25]


# Code files are text; this is ~10k long codes, far beyond any real batch.
MAX_IMPORT_BYTES = 1024 * 1024

# custom_id of a giveaway's Enter button is ENTER_PREFIX + giveaway id. Handled by the
# on_button_click listener rather than a registered View, so buttons on messages posted
# before a restart keep working with no extra setup.
ENTER_PREFIX = "giveaway:enter:"


def _discord_timestamp(dt: datetime.datetime, style: str) -> str:
    """Discord renders <t:unix:style> in each reader's own timezone. `dt` is naive UTC."""
    return f"<t:{calendar.timegm(dt.timetuple())}:{style}>"


def giveaway_embed(
    prize_name: str,
    winner_count: int,
    ends_at: datetime.datetime,
    entrants: int | None,
    *,
    ended: bool = False,
    cancelled: bool = False,
) -> disnake.Embed:
    """The giveaway post. `entrants` is None for old reaction-entry giveaways, which have
    no stored count."""
    if cancelled:
        description = "This giveaway was cancelled. No winners will be drawn."
    elif ended:
        description = "This giveaway has ended."
    elif entrants is None:
        description = "This is a giveaway! React with any reaction to win the prize!"
    else:
        description = "Press **Enter giveaway** below for a chance to win!"
    embed = disnake.Embed(title="Giveaway!", description=description)
    embed.add_field(name="Prize", value=prize_name, inline=False)
    embed.add_field(name="Winners", value=str(winner_count), inline=True)
    if entrants is not None:
        embed.add_field(name="Entrants", value=str(entrants), inline=True)
    embed.add_field(
        name="Was due to end" if cancelled else "Ended" if ended else "Ends",
        value=f"{_discord_timestamp(ends_at, 'R')} ({_discord_timestamp(ends_at, 'f')})",
        inline=False,
    )
    return embed


def enter_button(giveaway_id: int, *, disabled: bool = False, label: str | None = None) -> disnake.ui.Button:
    return disnake.ui.Button(
        style=disnake.ButtonStyle.success,
        label=label or ("Giveaway ended" if disabled else "Enter giveaway"),
        emoji="🎉",
        custom_id=f"{ENTER_PREFIX}{giveaway_id}",
        disabled=disabled,
    )


class Giveaways(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        # giveaway id -> entrant count last shown on its post, so the poller only edits
        # posts whose count changed. In memory only: after a restart every active post is
        # refreshed once.
        self._shown_entrants: dict[int, int] = {}
        # (when, what failed) of the poller's most recent caught error, for /botstatus.
        # Caught errors are retried, so the watchdog never sees them; this does.
        self.last_poller_error: tuple[datetime.datetime, str] | None = None
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
        start_at: str = commands.Param(
            default=None,
            description="Schedule it: YYYY-MM-DD HH:MM in EVE time (UTC). Omit to start now.",
        ),
    ):
        starts_at = None
        if start_at:
            starts_at = parse_start_time(start_at, now_utc())
            if isinstance(starts_at, str):
                await inter.response.send_message(starts_at, ephemeral=True)
                return

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
                # A future starts_at makes it a scheduled giveaway; _post_pending waits
                # for it. Its codes are reserved from now, like any pending giveaway's.
                starts_at=starts_at,
            )
            session.add(giveaway)

        if starts_at:
            when = f"scheduled for {_discord_timestamp(starts_at, 'f')} ({_discord_timestamp(starts_at, 'R')})"
        else:
            when = "queued. It will post within about a minute"
        await inter.response.send_message(f"Giveaway for '{prize}' ({winners} winner(s)) {when}.", ephemeral=True)

    @commands.slash_command(
        guild_ids=[_config.guild_id],
        description="Manage running and scheduled giveaways.",
        default_member_permissions=disnake.Permissions(manage_guild=True),
    )
    async def giveaway(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @giveaway.sub_command(name="cancel", description="Cancel a scheduled or running giveaway. No winners are drawn.")
    async def giveaway_cancel(
        self,
        inter: disnake.ApplicationCommandInteraction,
        giveaway: str = commands.Param(autocomplete=autocomplete_open_giveaways),
    ):
        await inter.response.defer(ephemeral=True)
        if not giveaway.isdigit():
            await inter.edit_original_response("Pick a giveaway from the list.")
            return
        giveaway_id = int(giveaway)
        with self.bot.session_factory.begin() as session:
            # Locked so this can't interleave with the poller drawing the same giveaway:
            # _finish_one re-checks status under the same lock.
            row = session.scalar(select(Giveaway).where(Giveaway.id == giveaway_id).with_for_update())
            if row is None or row.status not in (STATUS_PENDING, STATUS_ACTIVE):
                await inter.edit_original_response("That giveaway isn't scheduled or running any more.")
                return
            was_posted = row.status == STATUS_ACTIVE
            row.status = STATUS_CANCELLED
            prize_name = session.get(Prize, row.prize_id).name
            post = (row.channel_id, row.message_id, row.winner_count, row.ends_at, row.entry_mode == ENTRY_BUTTON)
            entrants = session.scalar(select(func.count(GiveawayEntry.id)).where(GiveawayEntry.giveaway_id == giveaway_id))
            freed = row.winner_count

        self._shown_entrants.pop(giveaway_id, None)
        if was_posted:
            channel_id, message_id, winner_count, ends_at, uses_button = post
            try:
                channel = self.bot.get_channel(channel_id)
                if channel is not None:
                    await channel.get_partial_message(message_id).edit(
                        embed=giveaway_embed(
                            prize_name, winner_count, ends_at, entrants if uses_button else None, cancelled=True
                        ),
                        components=[enter_button(giveaway_id, disabled=True, label="Giveaway cancelled")]
                        if uses_button
                        else [],
                    )
            except Exception:
                logger.exception("Cancelled giveaway %s but couldn't update its post", giveaway_id)
        await inter.edit_original_response(
            f"Cancelled giveaway #{giveaway_id} ({prize_name}). Its {freed} reserved code(s) are available again."
        )

    @giveaway.sub_command(name="endnow", description="End a running giveaway now and draw its winners.")
    async def giveaway_endnow(
        self,
        inter: disnake.ApplicationCommandInteraction,
        giveaway: str = commands.Param(autocomplete=autocomplete_open_giveaways),
    ):
        await inter.response.defer(ephemeral=True)
        if not giveaway.isdigit():
            await inter.edit_original_response("Pick a giveaway from the list.")
            return
        giveaway_id = int(giveaway)
        with self.bot.session_factory.begin() as session:
            row = session.scalar(select(Giveaway).where(Giveaway.id == giveaway_id).with_for_update())
            if row is None or row.status not in (STATUS_PENDING, STATUS_ACTIVE):
                await inter.edit_original_response("That giveaway isn't running any more.")
                return
            if row.status == STATUS_PENDING:
                await inter.edit_original_response(
                    "That giveaway hasn't been posted yet, so there's nobody to draw. Use `/giveaway cancel` instead."
                )
                return
            # Past ends_at, so even if the draw below fails the poller finishes it within a minute.
            row.ends_at = now_utc()
        try:
            await self._finish_one(giveaway_id)
        except Exception:
            logger.exception("endnow: finishing giveaway %s failed; the poller will retry", giveaway_id)
            await inter.edit_original_response("Ended, but the draw hit an error. The bot will retry within a minute.")
            return
        await inter.edit_original_response(f"Giveaway #{giveaway_id} ended and its winners were drawn and announced.")

    @commands.slash_command(
        guild_ids=[_config.guild_id],
        description="Giveaway prizes and their codes.",
        default_member_permissions=disnake.Permissions(manage_guild=True),
    )
    async def prize(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @prize.sub_command(name="list", description="Every prize with its unused, available and reserved code counts.")
    async def prize_list(
        self,
        inter: disnake.ApplicationCommandInteraction,
        show_empty: bool = commands.Param(default=False, description="Also list prizes with no codes left."),
    ):
        with self.bot.session_factory() as session:
            prizes = session.scalars(select(Prize).order_by(Prize.active.desc(), Prize.name)).all()
            rows = [
                (p.name, p.active, unused_code_count(session, p.id), committed_winner_count(session, p.id))
                for p in prizes
            ]
        if not rows:
            await inter.response.send_message("No prizes yet. Add codes with `/prize import`.", ephemeral=True)
            return
        # A prize with unused codes all reserved by running giveaways isn't empty: it's
        # only hidden once every code has actually been handed out.
        shown = rows if show_empty else [r for r in rows if r[2] > 0]
        hidden = len(rows) - len(shown)
        lines = []
        for name, active, unused, reserved in shown:
            available = unused - reserved
            status = "" if active else " *(inactive)*"
            detail = f"{available} available" + (f", {reserved} reserved by giveaways" if reserved else "")
            lines.append(f"**{name}**{status} — {detail}")
        if not lines:
            lines.append("Every prize is out of codes. Add more with `/prize import`.")
        text = "\n".join(lines)
        footer = f"\n*{hidden} prize(s) with no codes left hidden; use `show_empty` to see them.*" if hidden else ""
        if len(text) + len(footer) > 2000:
            text = text[: 1980 - len(footer)] + "\n…(truncated)"
        await inter.response.send_message(text + footer, ephemeral=True)

    @prize.sub_command(
        name="import",
        description="Add codes from a file: a CSV with prize,code columns, or one code per line for one prize.",
    )
    async def prize_import(
        self,
        inter: disnake.ApplicationCommandInteraction,
        file: disnake.Attachment,
        prize: str = commands.Param(
            default=None,
            description="For a plain one-code-per-line file: the prize to add them to. Leave empty for a CSV.",
        ),
    ):
        # Private reply, and codes are never echoed back. The upload itself sits on
        # Discord's CDN behind an expiring link only this interaction knows.
        await inter.response.defer(ephemeral=True)
        if file.size > MAX_IMPORT_BYTES:
            await inter.edit_original_response(f"That file is too big (limit {MAX_IMPORT_BYTES // 1024} KB).")
            return
        try:
            text = (await file.read()).decode("utf-8-sig")
        except UnicodeDecodeError:
            await inter.edit_original_response("That file isn't UTF-8 text. Save it as CSV (UTF-8) or plain text.")
            return
        except disnake.HTTPException:
            await inter.edit_original_response("Couldn't download the file from Discord. Try again.")
            return

        try:
            if prize:
                codes = parse_code_lines(text)
                rows = [(prize, code) for code in codes]
                skipped_given = None
            else:
                parsed = parse_code_csv(text)
                rows = parsed.rows
                skipped_given = parsed.skipped_given if parsed.has_given_column else None
            if not rows:
                await inter.edit_original_response("No codes found in that file.")
                return
            with self.bot.session_factory.begin() as session:
                # A named prize must already exist; a CSV may create new ones, like the CLI.
                result = import_codes(session, rows, create_missing_prizes=not prize)
        except CodeFileError as e:
            await inter.edit_original_response(str(e))
            return

        parts = [f"Imported {result.added} code(s) into {len(result.prize_names)} prize(s)."]
        if result.created_prizes:
            parts.append(f"New prize(s): {', '.join(result.created_prizes)}.")
        if result.skipped_duplicates:
            parts.append(f"Skipped {result.skipped_duplicates} duplicate(s).")
        if skipped_given:
            parts.append(f"Skipped {skipped_given} row(s) marked as already given.")
        logger.info("prize import by %s: %s", inter.author.id, " ".join(parts))
        await inter.edit_original_response(" ".join(parts))

    @commands.slash_command(
        guild_ids=[_config.guild_id],
        description="Line up prizes for the automatic (timer) giveaways.",
        default_member_permissions=disnake.Permissions(manage_guild=True),
    )
    async def giveawayqueue(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @giveawayqueue.sub_command(name="add", description="Add a prize to the end of the automatic giveaway queue.")
    async def giveawayqueue_add(
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
            error = queue_giveaway(session, prize_row, winners, added_by_discord_id=inter.author.id)
            position = None if error else len(giveaway_queue(session))
        if error:
            await inter.response.send_message(f"Not queued: {error}", ephemeral=True)
            return
        winners_text = f"{winners} winner(s)" if winners else "the default winner count"
        await inter.response.send_message(
            f"Queued **{prize}** ({winners_text}) at position {position}.", ephemeral=True
        )

    @giveawayqueue.sub_command(name="list", description="Show the automatic giveaway queue.")
    async def giveawayqueue_list(self, inter: disnake.ApplicationCommandInteraction):
        with self.bot.session_factory() as session:
            lines = [f"{n}. {describe_queued(row)}" for n, row in enumerate(giveaway_queue(session), start=1)]
        message = "\n".join(lines) if lines else "The queue is empty, so the next automatic giveaway will be a random prize."
        await inter.response.send_message(message, ephemeral=True)

    @giveawayqueue.sub_command(name="remove", description="Remove one prize from the automatic giveaway queue.")
    async def giveawayqueue_remove(
        self,
        inter: disnake.ApplicationCommandInteraction,
        entry: str = commands.Param(autocomplete=autocomplete_queue, description="The queue entry to remove."),
    ):
        removed = False
        if entry.isdigit():
            with self.bot.session_factory.begin() as session:
                removed = remove_queued_giveaway(session, int(entry))
        await inter.response.send_message("Removed." if removed else "No such queue entry.", ephemeral=True)

    @giveawayqueue.sub_command(name="clear", description="Empty the automatic giveaway queue.")
    async def giveawayqueue_clear(self, inter: disnake.ApplicationCommandInteraction):
        with self.bot.session_factory.begin() as session:
            count = clear_giveaway_queue(session)
        await inter.response.send_message(
            f"Cleared {count} queued giveaway(s)." if count else "The queue was already empty.", ephemeral=True
        )

    @commands.Cog.listener("on_button_click")
    async def on_button_click(self, inter: disnake.MessageInteraction):
        custom_id = inter.component.custom_id or ""
        if not custom_id.startswith(ENTER_PREFIX):
            return
        try:
            giveaway_id = int(custom_id.removeprefix(ENTER_PREFIX))
        except ValueError:
            return
        await inter.response.send_message(self._enter(giveaway_id, inter.author.id), ephemeral=True)

    def _enter(self, giveaway_id: int, discord_id: int) -> str:
        try:
            with self.bot.session_factory.begin() as session:
                giveaway = session.get(Giveaway, giveaway_id)
                if giveaway is None or giveaway.status != STATUS_ACTIVE or giveaway.ends_at <= now_utc():
                    return "Sorry, this giveaway has ended."
                already = session.scalar(
                    select(GiveawayEntry.id).where(
                        GiveawayEntry.giveaway_id == giveaway_id, GiveawayEntry.discord_id == discord_id
                    )
                )
                if already is not None:
                    return "You're already entered. Good luck!"
                session.add(GiveawayEntry(giveaway_id=giveaway_id, discord_id=discord_id, entered_at=now_utc()))
        except IntegrityError:
            # A double-click raced the check above; the unique key caught the duplicate.
            return "You're already entered. Good luck!"
        return "You're entered. Good luck! Winners are DMed their code when the giveaway ends."

    @tasks.loop(minutes=1)
    async def poll(self):
        # disnake's tasks.loop only survives network-level errors — anything else
        # (a MySQL disconnect, a Discord 403/5xx) ends the loop for good while the bot
        # itself stays up. That happened in Nov 2026 and stalled every giveaway for
        # ~2.5 days, so nothing is allowed to escape from here.
        healthy = True
        for step in (self._post_pending, self._refresh_entrant_counts, self._finish_active):
            try:
                await step()
            except Exception:
                healthy = False
                logger.exception("Giveaway poller step %s failed; retrying next minute", step.__name__)
                self._note_poller_error(f"step {step.__name__}")
        if healthy:
            # Only a fully successful run counts, so a lasting DB outage also goes stale
            # and trips fuzzworkbot-watchdog.
            try:
                watchdog.write_heartbeat()
            except OSError:
                logger.exception("Could not write poller heartbeat")

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    async def _post_pending(self):
        with self.bot.session_factory() as session:
            # A pending giveaway with a future starts_at is scheduled: leave it until then.
            pending_ids = session.scalars(
                select(Giveaway.id).where(
                    Giveaway.status == STATUS_PENDING,
                    or_(Giveaway.starts_at.is_(None), Giveaway.starts_at <= now_utc()),
                )
            ).all()

        for giveaway_id in pending_ids:
            try:
                await self._post_one(giveaway_id)
            except Exception:
                logger.exception("Posting giveaway %s failed; will retry", giveaway_id)
                self._note_poller_error(f"posting giveaway #{giveaway_id}")

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
            message = await channel.send(
                embed=giveaway_embed(prize.name, giveaway.winner_count, ends_at, entrants=0),
                components=[enter_button(giveaway.id)],
            )

            giveaway.message_id = message.id
            giveaway.status = STATUS_ACTIVE
            giveaway.entry_mode = ENTRY_BUTTON
            giveaway.starts_at = now
            giveaway.ends_at = ends_at
        self._shown_entrants[giveaway_id] = 0

    async def _refresh_entrant_counts(self):
        """Edit each active button giveaway's post when its entrant count has changed.
        Done here, at most once a minute per post, rather than on every click, which
        would hit Discord's message-edit rate limit during a rush of entries."""
        with self.bot.session_factory() as session:
            rows = session.execute(
                select(
                    Giveaway.id,
                    Giveaway.channel_id,
                    Giveaway.message_id,
                    Giveaway.winner_count,
                    Giveaway.ends_at,
                    Prize.name.label("prize_name"),
                    select(func.count(GiveawayEntry.id))
                    .where(GiveawayEntry.giveaway_id == Giveaway.id)
                    .scalar_subquery()
                    .label("entrants"),
                )
                .join(Prize, Prize.id == Giveaway.prize_id)
                .where(Giveaway.status == STATUS_ACTIVE, Giveaway.entry_mode == ENTRY_BUTTON)
            ).all()

        for row in rows:
            g = row._mapping
            if self._shown_entrants.get(g["id"]) == g["entrants"]:
                continue
            try:
                channel = self.bot.get_channel(g["channel_id"])
                if channel is None:
                    continue
                await channel.get_partial_message(g["message_id"]).edit(
                    embed=giveaway_embed(g["prize_name"], g["winner_count"], g["ends_at"], g["entrants"])
                )
                self._shown_entrants[g["id"]] = g["entrants"]
            except Exception:
                logger.exception("Updating entrant count for giveaway %s failed; will retry", g["id"])
                self._note_poller_error(f"updating giveaway #{g['id']}'s entrant count")

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
                self._note_poller_error(f"finishing giveaway #{giveaway_id}")

    def _note_poller_error(self, what: str) -> None:
        exc = sys.exc_info()[1]
        detail = f"{type(exc).__name__}: {exc}" if exc else "unknown error"
        self.last_poller_error = (now_utc(), f"{what}: {detail}"[:300])

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
            uses_button = giveaway.entry_mode == ENTRY_BUTTON
            prize_name = session.get(Prize, giveaway.prize_id).name
            ends_at = giveaway.ends_at
            if uses_button:
                entrant_ids = list(
                    session.scalars(select(GiveawayEntry.discord_id).where(GiveawayEntry.giveaway_id == giveaway_id))
                )

        guild = self.bot.get_guild(guild_id)
        channel = (guild.get_channel(channel_id) if guild else None) or self.bot.get_channel(channel_id)
        if channel is None:
            logger.warning("Channel missing for giveaway %s", giveaway_id)
            self._mark_finished(giveaway_id)
            return

        # Users already in hand (reaction giveaways), so phase 3 needn't look them up.
        known_users: dict[int, disnake.abc.User] = {}
        if not uses_button:
            try:
                message = await channel.fetch_message(message_id)
            except disnake.NotFound:
                logger.warning("Giveaway message %s missing", message_id)
                self._mark_finished(giveaway_id)
                return
            for reaction in message.reactions:
                async for user in reaction.users():
                    if not user.bot:
                        known_users[user.id] = user
            entrant_ids = list(known_users)

        # Phase 2: the draw, committed before anyone is told anything.
        awards: list[tuple[int, str | None]] = []
        with self.bot.session_factory.begin() as session:
            giveaway = session.scalar(select(Giveaway).where(Giveaway.id == giveaway_id).with_for_update())
            if giveaway is None or giveaway.status != STATUS_ACTIVE:
                return
            winner_count = giveaway.winner_count
            for winner_id in random.sample(entrant_ids, min(winner_count, len(entrant_ids))):
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
                    session.add(GiveawayWinner(giveaway_id=giveaway_id, discord_id=winner_id, drawn_at=now_utc()))
                    awards.append((winner_id, None))
                    continue
                code_row.assigned_to_discord_id = winner_id
                code_row.assigned_at = now_utc()
                session.add(
                    GiveawayWinner(giveaway_id=giveaway_id, discord_id=winner_id, code_id=code_row.id, drawn_at=now_utc())
                )
                awards.append((winner_id, code_row.code))
            giveaway.status = STATUS_FINISHED

        # Phase 3: notify. Best effort only — see docstring.
        self._shown_entrants.pop(giveaway_id, None)
        if uses_button:
            try:
                await channel.get_partial_message(message_id).edit(
                    embed=giveaway_embed(prize_name, winner_count, ends_at, len(entrant_ids), ended=True),
                    components=[enter_button(giveaway_id, disabled=True)],
                )
            except Exception:
                logger.exception("Could not mark giveaway %s's post as ended", giveaway_id)

        if not awards:
            embed = disnake.Embed(title="Giveaway Ended", description=f"No one entered the giveaway for **{prize_name}**.")
        else:
            winner_lines = []
            for winner_id, code in awards:
                mention = f"<@{winner_id}>"
                if code is None:
                    winner_lines.append(f"{mention} — no code left! An admin needs to deliver one manually.")
                    continue
                try:
                    user = known_users.get(winner_id) or self.bot.get_user(winner_id) or await self.bot.fetch_user(winner_id)
                    await user.send(f"You won **{prize_name}**! Your code: `{code}`")
                    winner_lines.append(mention)
                except disnake.HTTPException as e:
                    logger.warning("Could not DM winner %s (giveaway %s): %s", winner_id, giveaway_id, e)
                    winner_lines.append(f"{mention} (couldn't DM you — an admin needs to deliver the code manually)")

            embed = disnake.Embed(
                title="Giveaway Winner!" if len(awards) == 1 else "Giveaway Winners!",
                description="\n".join(winner_lines),
            )
            embed.add_field(name="Prize", value=prize_name, inline=False)
            if len(awards) < winner_count:
                embed.add_field(
                    name="Note",
                    value=f"Only {len(entrant_ids)} entrant(s) for {winner_count} requested winner(s).",
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
