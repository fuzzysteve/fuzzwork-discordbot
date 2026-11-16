from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fuzzworkbot.db import (
    NEXT_GIVEAWAY_ID,
    STATUS_ACTIVE,
    STATUS_PENDING,
    Giveaway,
    GiveawayCode,
    NextGiveaway,
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


def get_next_giveaway(session: Session) -> NextGiveaway | None:
    return session.get(NextGiveaway, NEXT_GIVEAWAY_ID)


def set_next_giveaway(
    session: Session, prize: Prize, winner_count: int | None, set_by_discord_id: int | None = None
) -> str | None:
    """Make `prize` the next random giveaway, replacing any existing choice. Returns an
    error message (and changes nothing) if the prize couldn't cover it right now;
    otherwise None. Must be called inside a write transaction. Shared by the CLI and
    /nextgiveaway so both apply the same rule."""
    if not prize.active:
        return f"'{prize.name}' is inactive."
    needed = winner_count or 1
    available = available_code_count(session, prize.id)
    if needed > available:
        return f"'{prize.name}' only has {available} code(s) available right now, not {needed}."

    row = get_next_giveaway(session)
    if row is None:
        row = NextGiveaway(id=NEXT_GIVEAWAY_ID)
        session.add(row)
    row.prize_id = prize.id
    row.winner_count = winner_count
    row.set_by_discord_id = set_by_discord_id
    row.set_at = now_utc()
    return None


def clear_next_giveaway(session: Session) -> bool:
    """Remove the next-giveaway choice. Returns whether there was one."""
    row = get_next_giveaway(session)
    if row is None:
        return False
    session.delete(row)
    return True
