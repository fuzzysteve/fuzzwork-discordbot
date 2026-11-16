"""giveaway-cli against a scratch SQLite DB (DATABASE_URL is pointed at it by the
db_url fixture). Interactive questionary paths aren't covered — every command here is
run with the flags that skip the prompts."""

import pytest
from click.testing import CliRunner
from sqlalchemy import select

from fuzzworkbot.db import STATUS_ACTIVE, STATUS_FINISHED, Giveaway, GiveawayCode, Prize, RoleReaction
from fuzzworkbot_cli import cli


@pytest.fixture(autouse=True)
def no_discord(monkeypatch):
    monkeypatch.setattr(cli, "_resolve_username", lambda user_id, token, cache: f"user{user_id}")


@pytest.fixture
def run(session_factory):
    runner = CliRunner()

    def _run(*args, ok=True):
        result = runner.invoke(cli.main, list(args))
        assert (result.exit_code == 0) == ok, result.output
        return result

    return _run


def _codes(session_factory):
    with session_factory() as session:
        return {c.code: c for c in session.scalars(select(GiveawayCode))}


def test_prize_add_and_reject_duplicate(run, session_factory):
    run("prize", "add", "--name", "PLEX x2", "--description", "")
    result = run("prize", "add", "--name", "PLEX x2", "--description", "", ok=False)
    assert "already exists" in result.output
    with session_factory() as session:
        prize = session.scalar(select(Prize))
    assert prize.name == "PLEX x2" and prize.description is None


def test_prize_list_shows_reserved_codes(run, make_prize, make_giveaway):
    prize_id = make_prize(name="PLEX", codes=["A", "B", "C"])
    make_giveaway(prize_id, status=STATUS_ACTIVE, winner_count=2)
    assert "3 unused (1 available, 2 reserved by running giveaways)" in run("prize", "list").output


def test_codes_load_skips_blanks_and_duplicates(run, session_factory, make_prize, tmp_path):
    make_prize(name="PLEX", codes=["A"])
    codes_file = tmp_path / "codes.txt"
    codes_file.write_text("A\n B \n\nC\nC\n")

    result = run("codes", "load", "PLEX", "--file", str(codes_file))

    assert "Loaded 2 new code(s)" in result.output and "2 duplicate(s)" in result.output
    assert set(_codes(session_factory)) == {"A", "B", "C"}


def test_codes_load_unknown_prize(run, tmp_path):
    codes_file = tmp_path / "codes.txt"
    codes_file.write_text("A\n")
    run("codes", "load", "Nope", "--file", str(codes_file), ok=False)


def test_import_csv(run, session_factory, make_prize, tmp_path):
    make_prize(name="PLEX", codes=["A"])
    csv_file = tmp_path / "import.csv"
    csv_file.write_text(
        "Code,PRIZE,Given\n"
        "A,PLEX,\n"  # duplicate of existing
        "B,PLEX,\n"
        "C,PLEX,yes\n"  # already given out
        "D,Skin,\n"  # new prize
        ",Skin,\n"  # blank code
    )

    result = run("codes", "import-csv", str(csv_file))

    assert "Imported 2 code(s) across 2 prize(s) (1 new prize(s) created, 1 duplicate code(s) skipped, " \
           "1 already-given row(s) skipped)." in result.output
    codes = _codes(session_factory)
    assert set(codes) == {"A", "B", "D"}


def test_import_csv_requires_columns(run, tmp_path):
    csv_file = tmp_path / "bad.csv"
    csv_file.write_text("name,value\nx,y\n")
    assert "must have 'prize' and 'code'" in run("codes", "import-csv", str(csv_file), ok=False).output


@pytest.mark.parametrize(
    "flags, message",
    [
        (["--for", "0"], "not 0"),
        (["--for", "5", "--note", "x"], "exactly one"),
        (["--note", "  "], "can't be blank"),
    ],
)
def test_redeem_validates_target(run, make_prize, flags, message):
    make_prize(name="PLEX", codes=["A"])
    assert message in run("codes", "redeem", "PLEX", *flags, ok=False).output


def test_redeem_with_note_marks_code_used(run, session_factory, make_prize):
    make_prize(name="PLEX", codes=["A"])

    result = run("codes", "redeem", "PLEX", "--note", "forum: JohnDoe")

    assert "Redeemed 'PLEX' code for forum: JohnDoe: A" in result.output
    code = _codes(session_factory)["A"]
    assert code.assigned_at is not None
    assert code.assigned_note == "forum: JohnDoe" and code.assigned_to_discord_id is None
    assert "No unused codes left" in run("codes", "redeem", "PLEX", "--note", "again", ok=False).output


def test_redeem_random_only_uses_active_prizes(run, session_factory, make_prize):
    make_prize(name="Old", codes=["X"], active=False)
    make_prize(name="PLEX", codes=["A"])

    run("codes", "redeem-random", "--for", "123")

    codes = _codes(session_factory)
    assert codes["A"].assigned_to_discord_id == 123
    assert codes["X"].assigned_at is None
    run("codes", "redeem-random", "--for", "123", ok=False)


def test_codes_list_status(run, make_prize):
    make_prize(name="PLEX", codes=["A", "B", "C"])
    run("codes", "redeem", "PLEX", "--note", "forum guy")
    run("codes", "redeem", "PLEX", "--for", "77")
    lines = run("codes", "list", "PLEX").output.splitlines()
    assert sum("unused" in line for line in lines) == 1
    assert any("given to forum guy" in line for line in lines)
    assert any("given to 77 (user77)" in line for line in lines)


def test_giveaway_random_creates_pending_giveaway(run, session_factory, make_prize):
    make_prize(name="PLEX", codes=["A", "B"])

    run("giveaway", "random", "--winners", "2", "--channel-id", "42", "--duration-hours", "6")

    with session_factory() as session:
        giveaway = session.scalar(select(Giveaway))
    assert (giveaway.winner_count, giveaway.channel_id, giveaway.duration_hours) == (2, 42, 6)
    assert giveaway.guild_id == 1000  # GUILD_ID from config


def test_giveaway_random_respects_capacity(run, session_factory, make_prize, make_giveaway):
    prize_id = make_prize(name="PLEX", codes=["A", "B"])
    make_giveaway(prize_id, status=STATUS_ACTIVE, winner_count=2)
    make_prize(name="Old", codes=["X", "Y"], active=False)

    result = run("giveaway", "random", "--winners", "1", ok=False)

    assert "Nothing created" in result.output
    with session_factory() as session:
        assert len(session.scalars(select(Giveaway)).all()) == 1


def test_giveaway_random_rechecks_capacity_under_lock(run, session_factory, make_prize, make_giveaway, monkeypatch):
    # Simulate a race: another giveaway claims the prize's only code between the
    # unlocked eligibility scan and the locked insert.
    prize_id = make_prize(name="PLEX", codes=["A"])

    def choose_then_race(eligible_ids):
        make_giveaway(prize_id, winner_count=1)
        return eligible_ids[0]

    monkeypatch.setattr(cli.random, "choice", choose_then_race)

    assert "no longer has 1 available code(s)" in run("giveaway", "random", ok=False).output
    with session_factory() as session:
        assert len(session.scalars(select(Giveaway)).all()) == 1


def test_giveaway_winners(run, session_factory, make_prize, make_giveaway):
    from fuzzworkbot.db import GiveawayWinner, now_utc

    prize_id = make_prize(name="PLEX", codes=["A"])
    giveaway_id = make_giveaway(prize_id, status=STATUS_FINISHED, winner_count=2)
    with session_factory.begin() as session:
        code_id = session.scalar(select(GiveawayCode.id))
        session.add(GiveawayWinner(giveaway_id=giveaway_id, discord_id=10, code_id=code_id, drawn_at=now_utc()))
        session.add(GiveawayWinner(giveaway_id=giveaway_id, discord_id=11, code_id=None, drawn_at=now_utc()))

    output = run("giveaway", "winners", str(giveaway_id)).output

    assert "10 (user10): A" in output
    assert "11 (user11): [NO CODE — deliver manually]" in output
    run("giveaway", "winners", "999", ok=False)


def test_role_menu_add_update_list_remove(run, session_factory):
    base = ["role-menu", "add", "--guild-id", "1", "--channel-id", "2", "--message-id", "3", "--emoji", "👍"]
    assert "Added" in run(*base, "--role-id", "4").output
    assert "Updated" in run(*base, "--role-id", "5").output
    # A different emoji on the same message is its own mapping.
    run(*base[:-1], "🎉", "--role-id", "6")

    with session_factory() as session:
        mappings = {r.emoji: r.role_id for r in session.scalars(select(RoleReaction))}
    assert mappings == {"👍": 5, "🎉": 6}
    assert "👍 -> role 5" in run("role-menu", "list").output

    run("role-menu", "remove", "--message-id", "3", "--emoji", "👍")
    run("role-menu", "remove", "--message-id", "3", "--emoji", "👍", ok=False)


# --- giveaway queue --------------------------------------------------------------


def _queue(session_factory):
    from fuzzworkbot.db import QueuedGiveaway

    with session_factory() as session:
        return [(q.prize_id, q.winner_count) for q in session.scalars(select(QueuedGiveaway).order_by(QueuedGiveaway.id))]


def test_giveaway_queue_add_list_remove_clear(run, session_factory, make_prize):
    from fuzzworkbot.db import QueuedGiveaway

    plex = make_prize(name="PLEX", codes=["A", "B", "C"])
    skin = make_prize(name="Skin", codes=["X"])
    assert "Queue is empty" in run("giveaway", "queue", "list").output

    run("giveaway", "queue", "add", "PLEX", "--winners", "2")
    output = run("giveaway", "queue", "add", "Skin").output
    assert "1. #" in output and "PLEX (2 winner(s))" in output and "2. #" in output and "Skin (default winners)" in output
    assert _queue(session_factory) == [(plex, 2), (skin, None)]

    with session_factory() as session:
        first_id = session.scalar(select(QueuedGiveaway.id).order_by(QueuedGiveaway.id))
    run("giveaway", "queue", "remove", str(first_id))
    assert _queue(session_factory) == [(skin, None)]
    run("giveaway", "queue", "remove", str(first_id), ok=False)

    assert "Cleared 1" in run("giveaway", "queue", "clear").output
    assert _queue(session_factory) == []


def test_giveaway_queue_add_counts_already_queued(run, session_factory, make_prize):
    make_prize(name="PLEX", codes=["A", "B"])
    make_prize(name="Old", codes=["X"], active=False)
    run("giveaway", "queue", "add", "PLEX")
    run("giveaway", "queue", "add", "PLEX")
    # Two codes, two queued: a third can't be covered.
    assert "2 already queued" in run("giveaway", "queue", "add", "PLEX", ok=False).output
    assert "inactive" in run("giveaway", "queue", "add", "Old", ok=False).output
    run("giveaway", "queue", "add", "Nope", ok=False)
    assert len(_queue(session_factory)) == 2


def test_giveaway_random_uses_queue_in_order(run, session_factory, make_prize, monkeypatch):
    make_prize(name="PLEX", codes=["A", "B", "C"])
    skin = make_prize(name="Skin", codes=["X", "Y"])
    plex2 = make_prize(name="PLEX2", codes=["Z"])
    run("giveaway", "queue", "add", "Skin", "--winners", "2")
    run("giveaway", "queue", "add", "PLEX2")
    monkeypatch.setattr(cli.random, "choice", lambda ids: pytest.fail("should not pick at random"))

    assert "created for 'Skin'" in run("giveaway", "random").output
    assert "created for 'PLEX2'" in run("giveaway", "random").output

    with session_factory() as session:
        made = [(g.prize_id, g.winner_count) for g in session.scalars(select(Giveaway).order_by(Giveaway.id))]
    assert made == [(skin, 2), (plex2, 1)]
    assert _queue(session_factory) == []


def test_giveaway_random_skips_queued_that_cant_run(run, session_factory, make_prize, make_giveaway):
    skin = make_prize(name="Skin", codes=["X"])
    plex = make_prize(name="PLEX", codes=["A"])
    run("giveaway", "queue", "add", "Skin")
    run("giveaway", "queue", "add", "PLEX")
    make_giveaway(skin, status=STATUS_ACTIVE)  # uses up Skin's only code after it was queued

    result = run("giveaway", "random")

    assert "Skipping queued" in result.output and "created for 'PLEX'" in result.output
    assert _queue(session_factory) == [(skin, None)]  # kept for a later run


def test_giveaway_random_falls_back_to_random_when_queue_cant_run(run, session_factory, make_prize, make_giveaway):
    skin = make_prize(name="Skin", codes=["X"])
    make_prize(name="PLEX", codes=["A"])
    run("giveaway", "queue", "add", "Skin")
    make_giveaway(skin, status=STATUS_ACTIVE)

    assert "Random giveaway queued for 'PLEX'" in run("giveaway", "random").output
    assert _queue(session_factory) == [(skin, None)]


@pytest.mark.parametrize("option", ["--winners", "--duration-hours", "--channel-id"])
def test_giveaway_random_rejects_non_positive(run, session_factory, make_prize, option):
    make_prize(name="PLEX", codes=["A"])
    run("giveaway", "random", option, "0", ok=False)
    with session_factory() as session:
        assert session.scalar(select(Giveaway)) is None


@pytest.mark.parametrize("value, ok", [("24", True), (" 3 ", True), ("0", False), ("-1", False), ("abc", False), ("", False)])
def test_positive_int_validator(value, ok):
    assert (cli._positive_int(value) is True) == ok
