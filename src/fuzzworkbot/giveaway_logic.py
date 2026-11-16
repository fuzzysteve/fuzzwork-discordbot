import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fuzzworkbot.db import (
    STATUS_ACTIVE,
    STATUS_PENDING,
    Giveaway,
    GiveawayCode,
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
