from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fuzzworkbot.db import STATUS_ACTIVE, STATUS_PENDING, Giveaway, GiveawayCode


def unused_code_count(session: Session, prize_id: int) -> int:
    return (
        session.scalar(
            select(func.count(GiveawayCode.id)).where(
                GiveawayCode.prize_id == prize_id,
                GiveawayCode.assigned_to_discord_id.is_(None),
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
