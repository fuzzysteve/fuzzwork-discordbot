from sqlalchemy import select

from fuzzworkbot.db import STATUS_ACTIVE, STATUS_FINISHED, STATUS_PENDING, GiveawayCode, now_utc
from fuzzworkbot.giveaway_logic import available_code_count, committed_winner_count, unused_code_count


def _assign(session_factory, code, **fields):
    with session_factory.begin() as session:
        row = session.scalar(select(GiveawayCode).where(GiveawayCode.code == code))
        for key, value in fields.items():
            setattr(row, key, value)


def test_unused_count_is_decided_by_assigned_at(session_factory, make_prize):
    prize_id = make_prize(codes=["A", "B", "C", "D"])
    # Note-redeemed: no Discord ID, but used.
    _assign(session_factory, "A", assigned_note="forum: someone", assigned_at=now_utc())
    # Discord ID 0 is falsy — must still count as used (past bug).
    _assign(session_factory, "B", assigned_to_discord_id=0, assigned_at=now_utc())
    with session_factory() as session:
        assert unused_code_count(session, prize_id) == 2


def test_unused_count_is_per_prize(session_factory, make_prize):
    a = make_prize(name="A", codes=["1", "2"])
    b = make_prize(name="B", codes=["3"])
    with session_factory() as session:
        assert unused_code_count(session, a) == 2
        assert unused_code_count(session, b) == 1


def test_committed_counts_pending_and_active_only(session_factory, make_prize, make_giveaway):
    prize_id = make_prize(codes=["A"])
    make_giveaway(prize_id, status=STATUS_PENDING, winner_count=2)
    make_giveaway(prize_id, status=STATUS_ACTIVE, winner_count=3)
    make_giveaway(prize_id, status=STATUS_FINISHED, winner_count=10)
    with session_factory() as session:
        assert committed_winner_count(session, prize_id) == 5


def test_committed_is_zero_with_no_giveaways(session_factory, make_prize):
    prize_id = make_prize()
    with session_factory() as session:
        assert committed_winner_count(session, prize_id) == 0


def test_available_subtracts_running_giveaways(session_factory, make_prize, make_giveaway):
    prize_id = make_prize(codes=["A", "B", "C", "D", "E"])
    make_giveaway(prize_id, status=STATUS_ACTIVE, winner_count=2)
    make_giveaway(prize_id, status=STATUS_FINISHED, winner_count=2)
    with session_factory() as session:
        assert available_code_count(session, prize_id) == 3
