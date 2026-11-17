import datetime
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fuzzworkbot.db import (
    ENTRY_BUTTON,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_FINISHED,
    STATUS_PENDING,
    Giveaway,
    GiveawayCode,
    GiveawayEntry,
    GiveawayExclusion,
    GiveawayWinner,
    QueuedGiveaway,
    Prize,
    now_utc,
)


def unused_code_count(session: Session, prize_id: int) -> int:
    return (
        session.scalar(
            select(func.count(GiveawayCode.id)).where(
                GiveawayCode.prize_id == prize_id,
                GiveawayCode.assigned_at.is_(None),
            )
        )
        or 0
    )


def committed_winner_count(session: Session, prize_id: int) -> int:
    """Winners already promised by giveaways for this prize that haven't finished yet
    (pending: not posted; active: posted, still collecting reactions) — these haven't
    claimed a code row yet (that happens lazily when each giveaway finishes), so they
    have to be subtracted from the raw unused-code count by hand."""
    return (
        session.scalar(
            select(func.coalesce(func.sum(Giveaway.winner_count), 0)).where(
                Giveaway.prize_id == prize_id,
                Giveaway.status.in_((STATUS_PENDING, STATUS_ACTIVE)),
            )
        )
        or 0
    )


def available_code_count(session: Session, prize_id: int) -> int:
    """Unused codes not already spoken for by a still-running giveaway for this prize.
    This is the number that's actually safe to promise to a *new* giveaway."""
    return unused_code_count(session, prize_id) - committed_winner_count(session, prize_id)



START_TIME_FORMAT = "%Y-%m-%d %H:%M"
MAX_SCHEDULE_AHEAD = datetime.timedelta(days=90)


def parse_start_time(text: str, now: datetime.datetime) -> datetime.datetime | str:
    """A scheduled start for /creategiveaway or `giveaway create`: "YYYY-MM-DD HH:MM" in
    EVE time (UTC), returned as a naive UTC datetime like every DATETIME column here.
    Returns an error message instead if it's malformed, not in the future, or too far
    ahead. A scheduled giveaway is just a pending row with a future starts_at; the
    poller posts it once that time arrives."""
    try:
        start = datetime.datetime.strptime(text.strip(), START_TIME_FORMAT)
    except ValueError:
        return "Start time must look like 2026-11-21 18:00 (EVE time, which is UTC)."
    if start <= now:
        return "Start time must be in the future (EVE time, which is UTC)."
    if start - now > MAX_SCHEDULE_AHEAD:
        return f"Start time can be at most {MAX_SCHEDULE_AHEAD.days} days ahead."
    return start


def queued_winner_total(session: Session, prize_id: int) -> int:
    """Winners already queued for this prize (an entry without a winner count counts as 1,
    the random run's default)."""
    return (
        session.scalar(
            select(func.coalesce(func.sum(func.coalesce(QueuedGiveaway.winner_count, 1)), 0)).where(
                QueuedGiveaway.prize_id == prize_id
            )
        )
        or 0
    )


def queue_giveaway(
    session: Session, prize: Prize, winner_count: int | None, added_by_discord_id: int | None = None
) -> str | None:
    """Append `prize` to the giveaway queue. Returns an error message (and adds nothing) if
    the prize couldn't cover it plus everything already queued for it; otherwise None.
    Must be called inside a write transaction. Shared by the CLI and /giveawayqueue so both
    apply the same rule."""
    if not prize.active:
        return f"'{prize.name}' is inactive."
    already = queued_winner_total(session, prize.id)
    needed = (winner_count or 1) + already
    available = available_code_count(session, prize.id)
    if needed > available:
        queued = f" ({already} already queued)" if already else ""
        return f"'{prize.name}' only has {available} code(s) available, not {needed}{queued}."
    session.add(
        QueuedGiveaway(
            prize_id=prize.id, winner_count=winner_count, added_by_discord_id=added_by_discord_id, added_at=now_utc()
        )
    )
    return None


def giveaway_queue(session: Session) -> list[QueuedGiveaway]:
    return list(session.scalars(select(QueuedGiveaway).order_by(QueuedGiveaway.id)))


def remove_queued_giveaway(session: Session, queue_id: int) -> bool:
    row = session.get(QueuedGiveaway, queue_id)
    if row is None:
        return False
    session.delete(row)
    return True


def clear_giveaway_queue(session: Session) -> int:
    rows = giveaway_queue(session)
    for row in rows:
        session.delete(row)
    return len(rows)


def describe_queued(row: QueuedGiveaway) -> str:
    winners = f"{row.winner_count} winner(s)" if row.winner_count else "default winners"
    return f"#{row.id} {row.prize.name} ({winners})"


# --- entrants and kicking ---------------------------------------------------------
# Shared by `giveaway-cli giveaway entrants|kick|unkick` and `/giveaway entrants|kick|
# unkick`. Kick and unkick lock the Giveaway row, as _finish_one's draw does, so a kick
# can't interleave with a draw: either it lands first and the draw (which re-reads
# entries under that lock) never sees the entrant, or the draw lands first and the kick
# is refused as already drawn.


@dataclass
class EntrantList:
    giveaway_id: int
    prize_name: str
    status: str
    ends_at: datetime.datetime | None
    stored: bool  # False for reaction giveaways: their entrants were never recorded
    entrants: list[tuple[int, datetime.datetime]]  # (discord_id, entered_at), oldest first
    winners: set[int]
    kicked: list[GiveawayExclusion]


def latest_giveaway_id(session: Session) -> int | None:
    return session.scalar(select(func.max(Giveaway.id)))


def entrant_list(session: Session, giveaway_id: int) -> EntrantList | None:
    giveaway = session.get(Giveaway, giveaway_id)
    if giveaway is None:
        return None
    return EntrantList(
        giveaway_id=giveaway.id,
        prize_name=session.get(Prize, giveaway.prize_id).name,
        status=giveaway.status,
        ends_at=giveaway.ends_at,
        # Pending giveaways haven't been posted yet; once posted they're button ones.
        stored=giveaway.entry_mode == ENTRY_BUTTON or giveaway.status == STATUS_PENDING,
        entrants=[
            (e.discord_id, e.entered_at)
            for e in session.scalars(
                select(GiveawayEntry).where(GiveawayEntry.giveaway_id == giveaway_id).order_by(GiveawayEntry.entered_at)
            )
        ],
        winners=set(session.scalars(select(GiveawayWinner.discord_id).where(GiveawayWinner.giveaway_id == giveaway_id))),
        kicked=list(
            session.scalars(
                select(GiveawayExclusion).where(GiveawayExclusion.giveaway_id == giveaway_id).order_by(GiveawayExclusion.excluded_at)
            )
        ),
    )


def _lock_open_giveaway(session: Session, giveaway_id: int) -> Giveaway | str:
    giveaway = session.scalar(select(Giveaway).where(Giveaway.id == giveaway_id).with_for_update())
    if giveaway is None:
        return f"No giveaway #{giveaway_id}."
    if giveaway.status == STATUS_FINISHED:
        return f"Giveaway #{giveaway_id} has already been drawn."
    if giveaway.status == STATUS_CANCELLED:
        return f"Giveaway #{giveaway_id} was cancelled."
    if giveaway.status == STATUS_ACTIVE and giveaway.entry_mode != ENTRY_BUTTON:
        return f"Giveaway #{giveaway_id} is entered by reactions; remove their reaction on its post instead."
    return giveaway


def kick_entrant(
    session: Session,
    giveaway_id: int,
    discord_id: int,
    who: str,
    *,
    reason: str | None = None,
    kicked_by_discord_id: int | None = None,
) -> tuple[bool, str]:
    """Delete `discord_id`'s entry and block them from re-entering. `who` names them in
    the message. Must be called inside a write transaction. Returns (changed, message)."""
    giveaway = _lock_open_giveaway(session, giveaway_id)
    if isinstance(giveaway, str):
        return False, giveaway
    entry = session.scalar(
        select(GiveawayEntry).where(GiveawayEntry.giveaway_id == giveaway_id, GiveawayEntry.discord_id == discord_id)
    )
    blocked = session.scalar(
        select(GiveawayExclusion).where(GiveawayExclusion.giveaway_id == giveaway_id, GiveawayExclusion.discord_id == discord_id)
    )
    if entry is None and blocked is not None:
        return False, f"{who} is already kicked from giveaway #{giveaway_id}."
    if entry is not None:
        session.delete(entry)
    if blocked is None:
        session.add(
            GiveawayExclusion(
                giveaway_id=giveaway_id,
                discord_id=discord_id,
                reason=reason,
                excluded_by_discord_id=kicked_by_discord_id,
                excluded_at=now_utc(),
            )
        )
    if entry is None:
        return True, f"{who} hadn't entered giveaway #{giveaway_id}; they're now blocked from entering it."
    return True, f"Removed {who} from giveaway #{giveaway_id}; they can't re-enter it."


def unkick_entrant(session: Session, giveaway_id: int, discord_id: int, who: str) -> tuple[bool, str]:
    """Lift a kick, so `discord_id` can press Enter again (their old entry isn't
    restored). Must be called inside a write transaction. Returns (changed, message)."""
    giveaway = _lock_open_giveaway(session, giveaway_id)
    if isinstance(giveaway, str):
        return False, giveaway
    blocked = session.scalar(
        select(GiveawayExclusion).where(GiveawayExclusion.giveaway_id == giveaway_id, GiveawayExclusion.discord_id == discord_id)
    )
    if blocked is None:
        return False, f"{who} isn't kicked from giveaway #{giveaway_id}."
    session.delete(blocked)
    return True, f"{who} can enter giveaway #{giveaway_id} again (their old entry isn't restored)."


def is_excluded(session: Session, giveaway_id: int, discord_id: int) -> bool:
    return (
        session.scalar(
            select(GiveawayExclusion.id).where(
                GiveawayExclusion.giveaway_id == giveaway_id, GiveawayExclusion.discord_id == discord_id
            )
        )
        is not None
    )
