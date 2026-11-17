import random
import sys

import click
import questionary
import requests
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from fuzzworkbot.code_import import CodeFileError, import_codes, parse_code_csv
from fuzzworkbot.config import Config, load_config
from fuzzworkbot.db import (
    STATUS_CANCELLED,
    STATUS_FINISHED,
    STATUS_PENDING,
    Giveaway,
    GiveawayCode,
    GiveawayWinner,
    QueuedGiveaway,
    Prize,
    RoleReaction,
    make_session_factory,
    now_utc,
)
from fuzzworkbot.giveaway_logic import (
    available_code_count,
    parse_start_time,
    clear_giveaway_queue,
    describe_queued,
    entrant_list,
    giveaway_queue,
    kick_entrant,
    latest_giveaway_id,
    unkick_entrant,
    queue_giveaway,
    remove_queued_giveaway,
    unused_code_count,
)

DISCORD_API_BASE = "https://discord.com/api/v10"


def _connect() -> tuple[sessionmaker, Config]:
    config = load_config()
    return make_session_factory(config.database_url), config


def _resolve_username(user_id: int, token: str, cache: dict[int, str]) -> str:
    """Best-effort Discord username lookup via REST (no gateway connection needed).
    Falls back to the raw ID string if the lookup fails for any reason."""
    if user_id not in cache:
        try:
            response = requests.get(
                f"{DISCORD_API_BASE}/users/{user_id}",
                headers={"Authorization": f"Bot {token}"},
                timeout=5,
            )
            response.raise_for_status()
            cache[user_id] = response.json().get("username", str(user_id))
        except requests.RequestException:
            cache[user_id] = str(user_id)
    return cache[user_id]


@click.group()
def main():
    """Admin CLI for the Fuzzwork Discord bot: prizes, giveaway codes, and role-reaction menus."""


@main.group()
def prize():
    """Manage giveaway prizes."""


@prize.command(
    "add",
    help="Add a new prize pool. This only registers the prize — load its actual "
    "giveaway codes afterward with 'codes load'.",
)
@click.option(
    "--name",
    prompt="Prize name (shown to members in /creategiveaway's picker, e.g. 'PLEX x2')",
    help="Short name shown to members when picking a prize, e.g. 'PLEX x2'. Must be unique.",
)
@click.option(
    "--description",
    prompt="Description (optional, admin-facing only — press Enter to skip)",
    default="",
    show_default=False,
    help="Optional longer note for admins; never shown to members.",
)
def prize_add(name: str, description: str):
    session_factory, _ = _connect()

    with session_factory() as session:
        if session.scalar(select(Prize).where(Prize.name == name)) is not None:
            click.echo(
                f"A prize named '{name}' already exists — prize names must be unique. "
                f"Pick a different name, or run `giveaway-cli codes load \"{name}\"` "
                "to add more codes to the existing one.",
                err=True,
            )
            sys.exit(1)

    with session_factory.begin() as session:
        session.add(Prize(name=name, description=description or None))

    click.echo(f"Added prize '{name}'.")
    click.echo(f'Next: load its codes with `giveaway-cli codes load "{name}"`.')


@prize.command("list")
@click.option("--all", "show_empty", is_flag=True, help="Also list prizes with no unused codes left.")
def prize_list(show_empty: bool):
    """List prizes and their code counts. Prizes with no unused codes are hidden unless --all."""
    session_factory, _ = _connect()
    with session_factory() as session:
        prizes = session.scalars(select(Prize).order_by(Prize.name)).all()
        if not prizes:
            click.echo("No prizes yet.")
            return
        hidden = 0
        for p in prizes:
            unused = unused_code_count(session, p.id)
            if unused == 0 and not show_empty:
                hidden += 1
                continue
            available = available_code_count(session, p.id)
            status = "active" if p.active else "inactive"
            if available == unused:
                click.echo(f"{p.id:>4}  {p.name:<30} {status:<8} {unused} unused code(s)")
            else:
                reserved = unused - available
                click.echo(
                    f"{p.id:>4}  {p.name:<30} {status:<8} {unused} unused "
                    f"({available} available, {reserved} reserved by running giveaways)"
                )
        if hidden:
            click.echo(f"({hidden} prize(s) with no codes left hidden; --all to show them)")


@main.group()
def codes():
    """Manage giveaway codes."""


@codes.command("load")
@click.argument("prize_name")
@click.option(
    "--file",
    "file_path",
    type=click.Path(exists=True),
    default=None,
    help="File with one code per line; omit to paste interactively.",
)
def codes_load(prize_name: str, file_path: str | None):
    session_factory, _ = _connect()
    with session_factory() as session:
        prize_row = session.scalar(select(Prize).where(Prize.name == prize_name))
        if prize_row is None:
            click.echo(f"No prize named '{prize_name}'. Add it first with 'prize add'.", err=True)
            sys.exit(1)

    if file_path:
        with open(file_path) as f:
            raw_codes = [line.strip() for line in f]
    else:
        click.echo("Paste codes, one per line. Empty line (or Ctrl-D) to finish.")
        raw_codes = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if not line.strip():
                break
            raw_codes.append(line)

    codes_to_add = [c.strip() for c in raw_codes if c.strip()]
    if not codes_to_add:
        click.echo("No codes given.")
        return

    with session_factory.begin() as session:
        added = import_codes(session, [(prize_name, code) for code in codes_to_add], create_missing_prizes=False).added

    skipped = len(codes_to_add) - added
    click.echo(f"Loaded {added} new code(s) for '{prize_name}' ({skipped} duplicate(s) skipped).")


@codes.command("list")
@click.argument("prize_name")
def codes_list(prize_name: str):
    """Show every code for a prize and who (if anyone) it's been given to."""
    session_factory, config = _connect()
    with session_factory() as session:
        prize_row = session.scalar(select(Prize).where(Prize.name == prize_name))
        if prize_row is None:
            click.echo(f"No prize named '{prize_name}'.", err=True)
            sys.exit(1)
        code_rows = session.scalars(
            select(GiveawayCode).where(GiveawayCode.prize_id == prize_row.id).order_by(GiveawayCode.id)
        ).all()

    if not code_rows:
        click.echo(f"No codes loaded for '{prize_name}'.")
        return

    username_cache: dict[int, str] = {}
    for c in code_rows:
        if c.assigned_at is None:
            status = "unused"
        elif c.assigned_to_discord_id is not None:
            username = _resolve_username(c.assigned_to_discord_id, config.discord_bot_token, username_cache)
            status = f"given to {c.assigned_to_discord_id} ({username}) at {c.assigned_at}"
        else:
            status = f"given to {c.assigned_note} at {c.assigned_at}"
        click.echo(f"{c.code:<30} {status}")


@codes.command(
    "import-csv",
    help="Bulk-import codes for multiple prizes from one CSV. Needs a header row with "
    "'prize' and 'code' columns (column order doesn't matter); an optional 'given' "
    "column, if present, causes already-given-out rows to be skipped. Missing prizes "
    "are created automatically.",
)
@click.argument("file_path", type=click.Path(exists=True))
def codes_import_csv(file_path: str):
    with open(file_path, newline="") as f:
        text = f.read()
    try:
        parsed = parse_code_csv(text)
    except CodeFileError as e:
        click.echo(str(e), err=True)
        sys.exit(1)

    if not parsed.rows:
        click.echo("No importable rows found (after skipping blanks/already-given).")
        return

    session_factory, _ = _connect()
    with session_factory.begin() as session:
        result = import_codes(session, parsed.rows, create_missing_prizes=True)

    summary = (
        f"Imported {result.added} code(s) across {len(result.prize_names)} prize(s) "
        f"({len(result.created_prizes)} new prize(s) created, {result.skipped_duplicates} duplicate code(s) skipped"
    )
    if parsed.has_given_column:
        summary += f", {parsed.skipped_given} already-given row(s) skipped"
    click.echo(summary + ").")


def _validate_redeem_target(discord_id: int | None, note: str | None) -> None:
    if (discord_id is None) == (note is None):
        click.echo("Pass exactly one of --for (a Discord user ID) or --note (a non-Discord recipient).", err=True)
        sys.exit(1)
    if discord_id is not None and discord_id <= 0:
        click.echo("--for must be a real Discord user ID (snowflake), not 0.", err=True)
        sys.exit(1)
    if note is not None and not note.strip():
        click.echo("--note can't be blank.", err=True)
        sys.exit(1)


def _describe_redeem_target(discord_id: int | None, note: str | None, token: str) -> str:
    if discord_id is not None:
        username = _resolve_username(discord_id, token, {})
        return f"{discord_id} ({username})"
    return note  # type: ignore[return-value]


def _prompt_redeem_target() -> tuple[int | None, str | None]:
    """Interactive fallback for --for/--note — used whenever neither flag was passed on
    the command line, so `codes redeem`/`redeem-random` are menu-driven by default and
    only need flags for scripted/non-interactive use."""
    target_type = questionary.select(
        "Who is this code for?",
        choices=[
            questionary.Choice(title="A Discord user (by ID)", value="discord"),
            questionary.Choice(title="Someone with no Discord ID (free-text note)", value="note"),
        ],
    ).ask()
    if target_type is None:
        click.echo("Cancelled.")
        sys.exit(1)

    if target_type == "discord":
        raw_id = questionary.text("Discord user ID:").ask()
        if not raw_id or not raw_id.strip():
            click.echo("Cancelled.")
            sys.exit(1)
        try:
            discord_id = int(raw_id.strip())
        except ValueError:
            click.echo("Discord user ID must be numeric.", err=True)
            sys.exit(1)
        return discord_id, None

    note = questionary.text("Note describing the recipient (e.g. 'forum: JohnDoe'):").ask()
    if not note or not note.strip():
        click.echo("Cancelled.")
        sys.exit(1)
    return None, note.strip()


def _prompt_prize_choice(session) -> int:
    """Interactive fallback for the prize_name argument on `codes redeem`."""
    prizes = session.scalars(select(Prize).where(Prize.active.is_(True))).all()
    choices = []
    for prize_row in prizes:
        unused = unused_code_count(session, prize_row.id)
        if unused > 0:
            choices.append(questionary.Choice(title=f"{prize_row.name} ({unused} unused)", value=prize_row.id))

    if not choices:
        click.echo("No active prizes with unused codes.", err=True)
        sys.exit(1)

    prize_id = questionary.select("Select prize:", choices=choices).ask()
    if prize_id is None:
        click.echo("Cancelled.")
        sys.exit(1)
    return prize_id


@codes.command(
    "redeem-random",
    help="Pull a random unused code from any active prize and mark it used — for handing "
    "a code to someone outside the normal giveaway-reaction flow (e.g. they won some "
    "other way). Menu-driven when run without --for/--note; pass one of them to skip "
    "the prompt for scripted use. CLI-only; does not touch Discord.",
)
@click.option("--for", "discord_id", type=int, default=None, help="Discord user ID receiving the code.")
@click.option(
    "--note",
    default=None,
    help="Free-text description of a non-Discord recipient, e.g. 'forum: JohnDoe' — use "
    "this instead of --for when there's no Discord ID.",
)
def codes_redeem_random(discord_id: int | None, note: str | None):
    if discord_id is None and note is None:
        discord_id, note = _prompt_redeem_target()
    _validate_redeem_target(discord_id, note)

    session_factory, config = _connect()

    with session_factory.begin() as session:
        active_prize_ids = session.scalars(select(Prize.id).where(Prize.active.is_(True))).all()
        candidates = session.scalars(
            select(GiveawayCode)
            .where(GiveawayCode.prize_id.in_(active_prize_ids), GiveawayCode.assigned_at.is_(None))
            .with_for_update()
        ).all()
        if not candidates:
            click.echo("No unused codes available for any active prize.", err=True)
            sys.exit(1)

        code_row = random.choice(candidates)
        code_row.assigned_to_discord_id = discord_id
        code_row.assigned_note = note
        code_row.assigned_at = now_utc()
        code_text = code_row.code
        prize_row = session.get(Prize, code_row.prize_id)
        prize_name = prize_row.name if prize_row else "?"

    target = _describe_redeem_target(discord_id, note, config.discord_bot_token)
    click.echo(f"Redeemed '{prize_name}' code for {target}: {code_text}")


@codes.command(
    "redeem",
    help="Pull a random unused code for a specific prize and mark it used — for handing "
    "a code to someone outside the normal giveaway-reaction flow (e.g. they won some "
    "other way). Menu-driven when run with no arguments; pass PRIZE_NAME and one of "
    "--for/--note to skip the prompts for scripted use. CLI-only; does not touch Discord.",
)
@click.argument("prize_name", required=False, default=None)
@click.option("--for", "discord_id", type=int, default=None, help="Discord user ID receiving the code.")
@click.option(
    "--note",
    default=None,
    help="Free-text description of a non-Discord recipient, e.g. 'forum: JohnDoe' — use "
    "this instead of --for when there's no Discord ID.",
)
def codes_redeem(prize_name: str | None, discord_id: int | None, note: str | None):
    if discord_id is None and note is None:
        discord_id, note = _prompt_redeem_target()
    _validate_redeem_target(discord_id, note)

    session_factory, config = _connect()

    # Resolve the prize in a plain (non-transactional) session first, so an
    # interactive prompt here doesn't leave a write transaction open while
    # waiting on the user.
    if prize_name is None:
        with session_factory() as session:
            prize_id = _prompt_prize_choice(session)
    else:
        with session_factory() as session:
            prize_row = session.scalar(select(Prize).where(Prize.name == prize_name))
            if prize_row is None:
                click.echo(f"No prize named '{prize_name}'.", err=True)
                sys.exit(1)
            prize_id = prize_row.id

    with session_factory.begin() as session:
        prize_row = session.get(Prize, prize_id)
        candidates = session.scalars(
            select(GiveawayCode)
            .where(GiveawayCode.prize_id == prize_id, GiveawayCode.assigned_at.is_(None))
            .with_for_update()
        ).all()
        if not candidates:
            click.echo(f"No unused codes left for '{prize_row.name}'.", err=True)
            sys.exit(1)

        code_row = random.choice(candidates)
        code_row.assigned_to_discord_id = discord_id
        code_row.assigned_note = note
        code_row.assigned_at = now_utc()
        code_text = code_row.code
        name = prize_row.name

    target = _describe_redeem_target(discord_id, note, config.discord_bot_token)
    click.echo(f"Redeemed '{name}' code for {target}: {code_text}")


@main.group()
def giveaway():
    """Manage giveaways."""


def _positive_int(value: str) -> bool | str:
    """questionary validator: True, or the error shown under the prompt."""
    return (value.strip().isdigit() and int(value) > 0) or "Enter a whole number greater than 0."


def _valid_start_time(value: str) -> bool | str:
    """questionary validator: blank (start now) or a valid future start time."""
    if not value.strip():
        return True
    result = parse_start_time(value, now_utc())
    return result if isinstance(result, str) else True


@giveaway.command("create")
def giveaway_create():
    session_factory, config = _connect()

    with session_factory() as session:
        prizes = session.scalars(select(Prize).where(Prize.active.is_(True))).all()
        choices = []
        for prize_row in prizes:
            available = available_code_count(session, prize_row.id)
            if available > 0:
                choices.append(questionary.Choice(title=f"{prize_row.name} ({available} available)", value=prize_row.id))

    if not choices:
        click.echo("No active prizes with available codes. Load some codes first.", err=True)
        sys.exit(1)

    prize_id = questionary.select("Select prize pool:", choices=choices).ask()
    if prize_id is None:
        return

    duration_hours = questionary.text("Duration (hours):", default="24", validate=_positive_int).ask()
    winners = questionary.text("Number of winners:", default="1", validate=_positive_int).ask()
    channel_id = questionary.text("Channel ID:", validate=_positive_int).ask()
    guild_id = questionary.text("Guild ID:", default=str(config.guild_id), validate=_positive_int).ask()
    start_text = questionary.text(
        "Start at, EVE time/UTC (YYYY-MM-DD HH:MM; leave blank to start now):",
        validate=_valid_start_time,
    ).ask()
    if not duration_hours or not winners or not channel_id or not guild_id or start_text is None:
        click.echo("Cancelled.")
        return
    starts_at = parse_start_time(start_text, now_utc()) if start_text.strip() else None

    with session_factory.begin() as session:
        # Lock the prize row so this can't race a concurrent /creategiveaway or
        # another CLI invocation for the same prize into over-promising codes.
        prize_row = session.scalar(select(Prize).where(Prize.id == prize_id).with_for_update())
        available = available_code_count(session, prize_row.id)
        if int(winners) > available:
            click.echo(
                f"Can't draw {winners} winner(s) — only {available} code(s) available now "
                "(someone else may have just created a giveaway for this prize).",
                err=True,
            )
            sys.exit(1)

        session.add(
            Giveaway(
                prize_id=prize_id,
                guild_id=int(guild_id),
                channel_id=int(channel_id),
                duration_hours=int(duration_hours),
                winner_count=int(winners),
                starts_at=starts_at,
            )
        )

    if starts_at:
        click.echo(f"Giveaway scheduled for {starts_at:%Y-%m-%d %H:%M} UTC. Its codes are reserved from now.")
    else:
        click.echo("Giveaway queued. The bot will post it within about a minute.")


DEFAULT_RANDOM_GIVEAWAY_CHANNEL_ID = 825889984120356894


@giveaway.command(
    "random",
    help="Create a giveaway for the first prize in the queue ('giveaway queue') that "
    "can run right now, removing it from the queue. Queued prizes that can't run yet "
    "are skipped and kept. With nothing runnable queued, picks a random active prize "
    "with enough available codes for the requested winner count. Fully "
    "non-interactive — no prompts — for cron/systemd timer use.",
)
@click.option("--duration-hours", type=click.IntRange(min=1), default=48, show_default=True)
@click.option("--winners", type=click.IntRange(min=1), default=1, show_default=True)
@click.option("--channel-id", type=click.IntRange(min=1), default=DEFAULT_RANDOM_GIVEAWAY_CHANNEL_ID, show_default=True)
@click.option("--guild-id", type=click.IntRange(min=1), default=None, help="Defaults to GUILD_ID from .env.")
def giveaway_random(duration_hours: int, winners: int, channel_id: int, guild_id: int | None):
    session_factory, config = _connect()
    guild_id = guild_id or config.guild_id

    with session_factory.begin() as session:
        for queued in session.scalars(select(QueuedGiveaway).order_by(QueuedGiveaway.id).with_for_update()).all():
            # Same lock-and-check as every other creation path (see CLAUDE.md).
            prize_row = session.scalar(select(Prize).where(Prize.id == queued.prize_id).with_for_update())
            wanted = queued.winner_count or winners
            available = available_code_count(session, prize_row.id)
            if prize_row.active and wanted <= available:
                session.add(
                    Giveaway(
                        prize_id=prize_row.id,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        duration_hours=duration_hours,
                        winner_count=wanted,
                    )
                )
                session.delete(queued)
                click.echo(
                    f"Queued giveaway #{queued.id} created for '{prize_row.name}' ({wanted} winner(s), "
                    f"{duration_hours}h) in channel {channel_id}. The bot will post it within about a minute."
                )
                return
            reason = "it's inactive" if not prize_row.active else f"it only has {available} available code(s)"
            click.echo(
                f"Skipping queued #{queued.id} '{prize_row.name}' ({wanted} winner(s)) — {reason}. "
                "Keeping it in the queue ('giveaway queue remove' to drop it).",
                err=True,
            )

    with session_factory() as session:
        prizes = session.scalars(select(Prize).where(Prize.active.is_(True))).all()
        eligible_ids = [p.id for p in prizes if available_code_count(session, p.id) >= winners]

    if not eligible_ids:
        click.echo(f"No active prize currently has {winners} available code(s). Nothing created.", err=True)
        sys.exit(1)

    chosen_id = random.choice(eligible_ids)

    with session_factory.begin() as session:
        # Lock the chosen prize row and re-check — another giveaway (CLI, /creategiveaway,
        # or another timer run) could have consumed capacity between the scan above and here.
        prize_row = session.scalar(select(Prize).where(Prize.id == chosen_id).with_for_update())
        available = available_code_count(session, prize_row.id)
        if winners > available:
            click.echo(
                f"Picked '{prize_row.name}' but it no longer has {winners} available code(s) "
                f"(now {available}) — someone else just used it. Nothing created this run.",
                err=True,
            )
            sys.exit(1)

        session.add(
            Giveaway(
                prize_id=prize_row.id,
                guild_id=guild_id,
                channel_id=channel_id,
                duration_hours=duration_hours,
                winner_count=winners,
            )
        )
        prize_name = prize_row.name

    click.echo(
        f"Random giveaway queued for '{prize_name}' ({winners} winner(s), {duration_hours}h) "
        f"in channel {channel_id}. The bot will post it within about a minute."
    )


@giveaway.group(
    "queue",
    help="Line up prizes for 'giveaway random' (the systemd timer) to use, in order, "
    "instead of random picks. Same queue as /giveawayqueue in Discord.",
)
def giveaway_queue_group():
    pass


@giveaway_queue_group.command("add")
@click.argument("prize_name", required=False, default=None)
@click.option(
    "--winners",
    type=click.IntRange(min=1),
    default=None,
    help="Winner count for that giveaway; omit to use the random run's own --winners.",
)
def giveaway_queue_add(prize_name: str | None, winners: int | None):
    """Add a prize to the end of the queue. Menu-driven when PRIZE_NAME is omitted."""
    session_factory, _ = _connect()

    if prize_name is None:
        with session_factory() as session:
            prize_id = _prompt_prize_choice(session)
    else:
        with session_factory() as session:
            prize_row = session.scalar(select(Prize).where(Prize.name == prize_name))
            if prize_row is None:
                click.echo(f"No prize named '{prize_name}'.", err=True)
                sys.exit(1)
            prize_id = prize_row.id

    with session_factory.begin() as session:
        prize_row = session.scalar(select(Prize).where(Prize.id == prize_id).with_for_update())
        error = queue_giveaway(session, prize_row, winners)
        if error:
            click.echo(f"Not queued: {error}", err=True)
            sys.exit(1)

    _print_queue(session_factory)


def _print_queue(session_factory) -> None:
    with session_factory() as session:
        rows = giveaway_queue(session)
        if not rows:
            click.echo("Queue is empty — the next random run will pick a prize at random.")
            return
        for n, row in enumerate(rows, start=1):
            click.echo(f"{n:>3}. {describe_queued(row)}  (added {row.added_at:%Y-%m-%d %H:%M} UTC)")


@giveaway_queue_group.command("list")
def giveaway_queue_list():
    """Show the queue, next-up first."""
    session_factory, _ = _connect()
    _print_queue(session_factory)


@giveaway_queue_group.command("remove")
@click.argument("queue_id", type=int)
def giveaway_queue_remove(queue_id: int):
    """Remove one entry, by the #id shown in 'giveaway queue list'."""
    session_factory, _ = _connect()
    with session_factory.begin() as session:
        removed = remove_queued_giveaway(session, queue_id)
    if not removed:
        click.echo(f"No queue entry #{queue_id}.", err=True)
        sys.exit(1)
    click.echo(f"Removed #{queue_id}.")


@giveaway_queue_group.command("clear")
def giveaway_queue_clear():
    """Empty the queue; random runs pick at random again."""
    session_factory, _ = _connect()
    with session_factory.begin() as session:
        count = clear_giveaway_queue(session)
    click.echo(f"Cleared {count} queued giveaway(s)." if count else "Queue was already empty.")


@giveaway.command("list")
def giveaway_list():
    session_factory, config = _connect()
    username_cache: dict[int, str] = {}
    with session_factory() as session:
        giveaways = session.scalars(select(Giveaway).order_by(Giveaway.created_at.desc()).limit(20)).all()
        if not giveaways:
            click.echo("No giveaways yet.")
            return
        for g in giveaways:
            prize_row = session.get(Prize, g.prize_id)
            prize_name = prize_row.name if prize_row else "?"
            if g.status == STATUS_FINISHED:
                winner_rows = session.scalars(
                    select(GiveawayWinner).where(GiveawayWinner.giveaway_id == g.id)
                ).all()
                if winner_rows:
                    names = []
                    for w in winner_rows:
                        username = _resolve_username(w.discord_id, config.discord_bot_token, username_cache)
                        suffix = "" if w.code_id else " [NO CODE]"
                        names.append(f"{w.discord_id} ({username}){suffix}")
                    detail = f"winners=[{', '.join(names)}]"
                else:
                    detail = "winners=none (no entrants)"
            elif g.status == STATUS_CANCELLED:
                detail = "cancelled, no winners drawn"
            elif g.status == STATUS_PENDING and g.starts_at is not None:
                detail = f"channel={g.channel_id} scheduled for {g.starts_at} UTC winners_wanted={g.winner_count}"
            else:
                detail = f"channel={g.channel_id} ends_at={g.ends_at} UTC winners_wanted={g.winner_count}"
            click.echo(f"{g.id:>4}  {g.status:<9} prize={prize_name:<25} {detail}")


@giveaway.command("entrants")
@click.argument("giveaway_id", type=int, required=False, default=None)
def giveaway_entrants(giveaway_id: int | None):
    """Who has entered a giveaway, oldest first (the latest giveaway if GIVEAWAY_ID is
    omitted). Winners are marked, and kicked members listed after."""
    session_factory, config = _connect()
    with session_factory() as session:
        giveaway_id = giveaway_id if giveaway_id is not None else latest_giveaway_id(session)
        listing = entrant_list(session, giveaway_id) if giveaway_id is not None else None
    if listing is None:
        click.echo("No such giveaway." if giveaway_id is not None else "No giveaways yet.", err=True)
        sys.exit(1)

    ends = f", ends {listing.ends_at} UTC" if listing.ends_at and listing.status == "active" else ""
    click.echo(f"Giveaway {listing.giveaway_id} — {listing.prize_name} ({listing.status}{ends}): {len(listing.entrants)} entrant(s)")
    if not listing.stored:
        click.echo("  Entered by reactions, so its entrants weren't recorded.")
        return
    cache: dict[int, str] = {}
    for n, (discord_id, entered) in enumerate(listing.entrants, start=1):
        name = _resolve_username(discord_id, config.discord_bot_token, cache)
        won = "  WINNER" if discord_id in listing.winners else ""
        click.echo(f"{n:>4}. {entered:%Y-%m-%d %H:%M} UTC  {name:<24} {discord_id}{won}")
    if listing.kicked:
        click.echo("  Kicked:")
        for k in listing.kicked:
            name = _resolve_username(k.discord_id, config.discord_bot_token, cache)
            reason = f" — {k.reason}" if k.reason else ""
            click.echo(f"        {k.excluded_at:%Y-%m-%d %H:%M} UTC  {name:<24} {k.discord_id}{reason}")


@giveaway.command("kick")
@click.argument("discord_id", type=click.IntRange(min=1))
@click.option("--giveaway", "giveaway_id", type=int, default=None, help="Giveaway ID; defaults to the latest.")
@click.option("--reason", default=None, help="Optional note, shown in 'giveaway entrants'.")
def giveaway_kick(discord_id: int, giveaway_id: int | None, reason: str | None):
    """Remove someone from a giveaway and block them from re-entering it."""
    session_factory, config = _connect()
    who = f"{discord_id} ({_resolve_username(discord_id, config.discord_bot_token, {})})"
    with session_factory.begin() as session:
        giveaway_id = giveaway_id if giveaway_id is not None else latest_giveaway_id(session)
        if giveaway_id is None:
            click.echo("No giveaways yet.", err=True)
            sys.exit(1)
        changed, message = kick_entrant(session, giveaway_id, discord_id, who, reason=reason)
    click.echo(message, err=not changed)
    if not changed:
        sys.exit(1)


@giveaway.command("unkick")
@click.argument("discord_id", type=click.IntRange(min=1))
@click.option("--giveaway", "giveaway_id", type=int, default=None, help="Giveaway ID; defaults to the latest.")
def giveaway_unkick(discord_id: int, giveaway_id: int | None):
    """Let someone you kicked enter a giveaway again."""
    session_factory, config = _connect()
    who = f"{discord_id} ({_resolve_username(discord_id, config.discord_bot_token, {})})"
    with session_factory.begin() as session:
        giveaway_id = giveaway_id if giveaway_id is not None else latest_giveaway_id(session)
        if giveaway_id is None:
            click.echo("No giveaways yet.", err=True)
            sys.exit(1)
        changed, message = unkick_entrant(session, giveaway_id, discord_id, who)
    click.echo(message, err=not changed)
    if not changed:
        sys.exit(1)


@giveaway.command("winners")
@click.argument("giveaway_id", type=int)
def giveaway_winners(giveaway_id: int):
    """Show the winners and their actual codes for one giveaway. CLI-only — codes are
    never surfaced through Discord itself except in the winner's own DM."""
    session_factory, config = _connect()
    with session_factory() as session:
        giveaway = session.get(Giveaway, giveaway_id)
        if giveaway is None:
            click.echo(f"No giveaway with ID {giveaway_id}.", err=True)
            sys.exit(1)

        prize_row = session.get(Prize, giveaway.prize_id)
        prize_name = prize_row.name if prize_row else "?"

        winner_rows = session.scalars(
            select(GiveawayWinner).where(GiveawayWinner.giveaway_id == giveaway_id)
        ).all()
        codes_by_id = {}
        code_ids = [w.code_id for w in winner_rows if w.code_id is not None]
        if code_ids:
            for code_row in session.scalars(select(GiveawayCode).where(GiveawayCode.id.in_(code_ids))):
                codes_by_id[code_row.id] = code_row.code

    click.echo(f"Giveaway {giveaway_id} — {prize_name} (status: {giveaway.status})")
    if not winner_rows:
        click.echo("  No winners recorded yet.")
        return

    username_cache: dict[int, str] = {}
    for w in winner_rows:
        username = _resolve_username(w.discord_id, config.discord_bot_token, username_cache)
        code = codes_by_id.get(w.code_id, "[NO CODE — deliver manually]") if w.code_id else "[NO CODE — deliver manually]"
        click.echo(f"  {w.discord_id} ({username}): {code}")


@main.group("role-menu")
def role_menu():
    """Manage reaction-role mappings for existing Discord messages."""


@role_menu.command("add")
@click.option("--guild-id", type=int, required=True)
@click.option("--channel-id", type=int, required=True)
@click.option("--message-id", type=int, required=True)
@click.option("--emoji", required=True, help="Unicode emoji, or the numeric ID of a custom guild emoji.")
@click.option("--role-id", type=int, required=True)
def role_menu_add(guild_id: int, channel_id: int, message_id: int, emoji: str, role_id: int):
    session_factory, _ = _connect()
    with session_factory.begin() as session:
        existing = session.scalar(
            select(RoleReaction).where(RoleReaction.message_id == message_id, RoleReaction.emoji == emoji)
        )
        if existing:
            existing.role_id = role_id
            existing.guild_id = guild_id
            existing.channel_id = channel_id
            click.echo("Updated existing mapping.")
        else:
            session.add(
                RoleReaction(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    message_id=message_id,
                    emoji=emoji,
                    role_id=role_id,
                )
            )
            click.echo("Added mapping.")


@role_menu.command("remove")
@click.option("--message-id", type=int, required=True)
@click.option("--emoji", required=True)
def role_menu_remove(message_id: int, emoji: str):
    session_factory, _ = _connect()
    with session_factory.begin() as session:
        existing = session.scalar(
            select(RoleReaction).where(RoleReaction.message_id == message_id, RoleReaction.emoji == emoji)
        )
        if existing is None:
            click.echo("No such mapping.", err=True)
            sys.exit(1)
        session.delete(existing)
    click.echo("Removed mapping.")


@role_menu.command("list")
def role_menu_list():
    session_factory, _ = _connect()
    with session_factory() as session:
        rows = session.scalars(select(RoleReaction).order_by(RoleReaction.message_id)).all()
        if not rows:
            click.echo("No role-reaction mappings yet.")
            return
        by_message: dict[int, list[RoleReaction]] = {}
        for row in rows:
            by_message.setdefault(row.message_id, []).append(row)
        for message_id, mappings in by_message.items():
            click.echo(f"Message {message_id} (channel {mappings[0].channel_id}):")
            for m in mappings:
                click.echo(f"  {m.emoji} -> role {m.role_id}")


if __name__ == "__main__":
    main()
