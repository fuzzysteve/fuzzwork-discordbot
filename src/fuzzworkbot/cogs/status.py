"""/botstatus: one admin view of the bot's health and what's coming up.

Everything here is read-only: the running process, the database, the watchdog and
dependency-check state files, the repo's .git files and `systemctl list-timers`.
"""

import asyncio
import calendar
import datetime
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

import disnake
from disnake.ext import commands
from sqlalchemy import func, select

from fuzzworkbot import update_check, watchdog
from fuzzworkbot.config import load_config
from fuzzworkbot.db import STATUS_ACTIVE, STATUS_PENDING, ENTRY_BUTTON, Giveaway, GiveawayEntry, Prize, UserLookup, now_utc
from fuzzworkbot.giveaway_logic import committed_winner_count, giveaway_queue, unused_code_count

logger = logging.getLogger(__name__)

_config = load_config()

LOW_STOCK_THRESHOLD = 3
REPO_DIR = Path(__file__).resolve().parents[3]
RANDOM_GIVEAWAY_TIMER = "fuzzworkbot-random-giveaway.timer"
MAX_LISTED = 8  # per embed field list (giveaways, low-stock prizes), then "+N more"


def _ts(dt: datetime.datetime, style: str = "R") -> str:
    """Discord timestamp (rendered in the reader's timezone). `dt` is naive UTC."""
    return f"<t:{calendar.timegm(dt.timetuple())}:{style}>"


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m" if minutes else f"{seconds}s"


def _listed(lines: list[str]) -> str:
    if len(lines) > MAX_LISTED:
        return "\n".join(lines[:MAX_LISTED] + [f"…and {len(lines) - MAX_LISTED} more"])
    return "\n".join(lines)


def read_commit(repo_dir: Path) -> str | None:
    """Short hash of HEAD, read from the .git files directly: running `git` as the bot's
    user fails, since git refuses a repo owned by another user."""
    git = repo_dir / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head[:7]
        ref = head.removeprefix("ref: ")
        loose = git / ref
        if loose.exists():
            return loose.read_text().strip()[:7]
        for line in (git / "packed-refs").read_text().splitlines():
            if line.endswith(f" {ref}"):
                return line.split(" ", 1)[0][:7]
    except OSError:
        pass
    return None


def read_pending_updates(state_file: Path) -> tuple[dict[str, int], datetime.datetime | None]:
    """From the dependency check's state file: pending updates per section, and when the
    check last ran (the file's mtime, naive UTC). Each run rewrites the file with only the
    updates still pending, so its keys are exactly what's outstanding."""
    try:
        state = json.loads(state_file.read_text())
        mtime = datetime.datetime.fromtimestamp(state_file.stat().st_mtime, datetime.timezone.utc).replace(tzinfo=None)
    except (OSError, ValueError):
        return {}, None
    per_section: dict[str, int] = {}
    for key in state:
        section = key.split("|", 1)[0]
        per_section[section] = per_section.get(section, 0) + 1
    return per_section, mtime


async def next_timer_run(unit: str) -> datetime.datetime | None:
    """Next elapse of a systemd timer (naive UTC), via `systemctl list-timers` (read-only,
    works unprivileged). None if it can't be read or isn't scheduled."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "list-timers", unit, "--all", "--output=json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        timers = json.loads(out or b"[]")
        next_us = timers[0].get("next") if timers else None
    except (OSError, ValueError, IndexError, asyncio.TimeoutError):
        return None
    if not next_us:
        return None
    return datetime.datetime.fromtimestamp(next_us / 1_000_000, datetime.timezone.utc).replace(tzinfo=None)


@dataclass
class StatusData:
    now: datetime.datetime
    started_at: datetime.datetime
    commit: str | None
    latency_s: float | None
    heartbeat: datetime.datetime | None
    last_poller_error: tuple[datetime.datetime, str] | None
    watchdog_alerting: bool
    running: list[tuple[int, str, datetime.datetime, int | None]]  # id, prize, ends_at, entrants (None: reactions)
    scheduled: list[tuple[int, str, datetime.datetime]]  # id, prize, starts_at
    about_to_post: int
    next_timer: datetime.datetime | None
    queue: list[str]  # describe_queued-style labels, next-up first
    prizes_with_codes: int
    codes_available: int
    codes_reserved: int
    low_stock: list[tuple[str, int]]  # active prizes with 0 < unused and available <= threshold
    empty_prizes: int
    linked_members: int
    pending_updates: dict[str, int] = field(default_factory=dict)
    updates_checked_at: datetime.datetime | None = None


def gather_db(session) -> dict:
    now = now_utc()
    running = []
    for g, prize_name in session.execute(
        select(Giveaway, Prize.name).join(Prize, Prize.id == Giveaway.prize_id)
        .where(Giveaway.status == STATUS_ACTIVE).order_by(Giveaway.ends_at)
    ).all():
        entrants = (
            session.scalar(select(func.count(GiveawayEntry.id)).where(GiveawayEntry.giveaway_id == g.id))
            if g.entry_mode == ENTRY_BUTTON
            else None
        )
        running.append((g.id, prize_name, g.ends_at, entrants))

    pending = session.execute(
        select(Giveaway.id, Giveaway.starts_at, Prize.name).join(Prize, Prize.id == Giveaway.prize_id)
        .where(Giveaway.status == STATUS_PENDING).order_by(Giveaway.starts_at)
    ).all()
    scheduled = [(r.id, r.name, r.starts_at) for r in pending if r.starts_at is not None and r.starts_at > now]

    with_codes = available_total = reserved_total = empty = 0
    low_stock = []
    for prize in session.scalars(select(Prize).order_by(Prize.name)):
        unused = unused_code_count(session, prize.id)
        if unused == 0:
            empty += 1
            continue
        reserved = committed_winner_count(session, prize.id)
        available = max(unused - reserved, 0)
        with_codes += 1
        available_total += available
        reserved_total += min(reserved, unused)
        if prize.active and available <= LOW_STOCK_THRESHOLD:
            low_stock.append((prize.name, available))

    queue = []
    for row in giveaway_queue(session):
        winners = f", {row.winner_count} winners" if row.winner_count else ""
        queue.append(f"{row.prize.name}{winners}")

    return dict(
        running=running,
        scheduled=scheduled,
        about_to_post=len(pending) - len(scheduled),
        queue=queue,
        prizes_with_codes=with_codes,
        codes_available=available_total,
        codes_reserved=reserved_total,
        low_stock=sorted(low_stock, key=lambda p: (p[1], p[0])),
        empty_prizes=empty,
        linked_members=session.scalar(select(func.count(UserLookup.id))) or 0,
    )


def build_embed(d: StatusData) -> disnake.Embed:
    healthy = (
        d.heartbeat is not None
        and d.now - d.heartbeat <= watchdog.STALE_AFTER
        and not d.watchdog_alerting
    )
    embed = disnake.Embed(
        title="Bot status",
        colour=disnake.Colour.green() if healthy else disnake.Colour.red(),
    )

    latency = f"{d.latency_s * 1000:.0f} ms" if d.latency_s is not None else "not connected"
    embed.add_field(
        name="Bot",
        value=f"Up {_duration((d.now - d.started_at).total_seconds())} · commit `{d.commit or '?'}` · gateway {latency}",
        inline=False,
    )

    if d.heartbeat is None:
        poller = "**No heartbeat recorded**"
    else:
        age = (d.now - d.heartbeat).total_seconds()
        poller = f"Last good run {_duration(age)} ago" + (" **(stale)**" if age > watchdog.STALE_AFTER.total_seconds() else "")
    if d.last_poller_error:
        when, what = d.last_poller_error
        poller += f"\nLast error {_ts(when)}: {what}"
    else:
        poller += "\nNo errors since start"
    poller += "\nWatchdog: " + ("**alert open**" if d.watchdog_alerting else "OK")
    embed.add_field(name="Poller", value=poller, inline=False)

    giveaway_lines = [
        f"#{gid} {name}, ends {_ts(ends)}" + (f" ({n} entrants)" if n is not None else " (reactions)")
        for gid, name, ends, n in d.running
    ]
    giveaway_lines += [f"#{gid} {name}, starts {_ts(starts, 'f')}" for gid, name, starts in d.scheduled]
    if d.about_to_post:
        giveaway_lines.append(f"{d.about_to_post} about to post")
    embed.add_field(
        name=f"Giveaways ({len(d.running)} running, {len(d.scheduled)} scheduled)",
        value=_listed(giveaway_lines) or "None",
        inline=False,
    )

    timer = f"Next random giveaway {_ts(d.next_timer)}" if d.next_timer else "Next random giveaway: **timer not scheduled or unreadable**"
    timer += f"\nQueue: {d.queue[0]}" + (f" (+{len(d.queue) - 1} more)" if len(d.queue) > 1 else "") if d.queue else "\nQueue empty (random pick)"
    embed.add_field(name="Timer", value=timer, inline=False)

    prizes = (
        f"{d.prizes_with_codes} with codes · {d.codes_available} codes available · {d.codes_reserved} reserved"
        f"\n{d.empty_prizes} empty"
    )
    if d.low_stock:
        prizes += f"\nLow stock (≤{LOW_STOCK_THRESHOLD} available):\n" + _listed([f"{name}: {n} left" for name, n in d.low_stock])
    embed.add_field(name="Prizes", value=prizes, inline=False)

    embed.add_field(name="Verified members", value=str(d.linked_members), inline=True)

    if d.updates_checked_at is None:
        updates = "No dependency report yet"
    elif not d.pending_updates:
        updates = f"None pending (checked {_ts(d.updates_checked_at)})"
    else:
        total = sum(d.pending_updates.values())
        updates = f"{total} pending (checked {_ts(d.updates_checked_at)})\n" + "\n".join(
            f"{section}: {n}" for section, n in sorted(d.pending_updates.items())
        )
    embed.add_field(name="Dependency updates", value=updates, inline=True)
    return embed


class Status(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self.started_at = now_utc()

    @commands.slash_command(
        guild_ids=[_config.guild_id],
        description="Bot health, upcoming giveaways, code stock and pending updates.",
        default_member_permissions=disnake.Permissions(manage_guild=True),
    )
    async def botstatus(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)
        with self.bot.session_factory() as session:
            db = gather_db(session)
        giveaways_cog = self.bot.get_cog("Giveaways")
        latency = getattr(self.bot, "latency", None)
        pending_updates, checked_at = read_pending_updates(update_check.STATE_FILE)
        data = StatusData(
            now=now_utc(),
            started_at=self.started_at,
            commit=read_commit(REPO_DIR),
            latency_s=latency if isinstance(latency, float) and math.isfinite(latency) else None,
            heartbeat=watchdog.read_heartbeat(),
            last_poller_error=getattr(giveaways_cog, "last_poller_error", None),
            watchdog_alerting=watchdog.is_alerting(),
            next_timer=await next_timer_run(RANDOM_GIVEAWAY_TIMER),
            pending_updates=pending_updates,
            updates_checked_at=checked_at,
            **db,
        )
        await inter.edit_original_response(embed=build_embed(data))


def setup(bot: commands.InteractionBot):
    bot.add_cog(Status(bot))
