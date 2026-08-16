import csv
import sys

import click
import questionary
import requests
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from fuzzworkbot.config import Config, load_config
from fuzzworkbot.db import Giveaway, GiveawayCode, Prize, RoleReaction, make_session_factory

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
def prize_list():
    session_factory, _ = _connect()
    with session_factory() as session:
        prizes = session.scalars(select(Prize).order_by(Prize.name)).all()
        if not prizes:
            click.echo("No prizes yet.")
            return
        for p in prizes:
            unused = session.scalar(
                select(func.count(GiveawayCode.id)).where(
                    GiveawayCode.prize_id == p.id,
                    GiveawayCode.assigned_to_discord_id.is_(None),
                )
            )
            status = "active" if p.active else "inactive"
            click.echo(f"{p.id:>4}  {p.name:<30} {status:<8} {unused} unused code(s)")


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
        prize_id = prize_row.id

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
        existing = set(session.scalars(select(GiveawayCode.code).where(GiveawayCode.prize_id == prize_id)))
        added = 0
        for code in codes_to_add:
            if code in existing:
                continue
            session.add(GiveawayCode(prize_id=prize_id, code=code))
            existing.add(code)
            added += 1

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
        if c.assigned_to_discord_id:
            username = _resolve_username(c.assigned_to_discord_id, config.discord_bot_token, username_cache)
            status = f"given to {c.assigned_to_discord_id} ({username}) at {c.assigned_at}"
        else:
            status = "unused"
        click.echo(f"{c.code:<30} {status}")


GIVEN_TRUTHY = {"yes", "true", "1", "y", "x", "given"}


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
        reader = csv.DictReader(f)
        columns = {(name or "").strip().lower(): name for name in (reader.fieldnames or [])}

        if "prize" not in columns or "code" not in columns:
            click.echo(
                "CSV must have 'prize' and 'code' columns in its header row (case-insensitive). "
                f"Found: {list(reader.fieldnames or [])}",
                err=True,
            )
            sys.exit(1)

        given_column = columns.get("given")

        rows: list[tuple[str, str]] = []
        skipped_given = 0
        for csv_row in reader:
            prize_name = (csv_row.get(columns["prize"]) or "").strip()
            code = (csv_row.get(columns["code"]) or "").strip()
            if not prize_name or not code:
                continue
            if given_column and (csv_row.get(given_column) or "").strip().lower() in GIVEN_TRUTHY:
                skipped_given += 1
                continue
            rows.append((prize_name, code))

    if not rows:
        click.echo("No importable rows found (after skipping blanks/already-given).")
        return

    session_factory, _ = _connect()
    created_prizes = 0
    added_codes = 0
    skipped_dupes = 0

    with session_factory.begin() as session:
        prize_ids: dict[str, int] = {}
        existing_codes: dict[int, set[str]] = {}

        for prize_name, code in rows:
            if prize_name not in prize_ids:
                prize_row = session.scalar(select(Prize).where(Prize.name == prize_name))
                if prize_row is None:
                    prize_row = Prize(name=prize_name)
                    session.add(prize_row)
                    session.flush()
                    created_prizes += 1
                prize_ids[prize_name] = prize_row.id
                existing_codes[prize_row.id] = set(
                    session.scalars(select(GiveawayCode.code).where(GiveawayCode.prize_id == prize_row.id))
                )

            prize_id = prize_ids[prize_name]
            if code in existing_codes[prize_id]:
                skipped_dupes += 1
                continue

            session.add(GiveawayCode(prize_id=prize_id, code=code))
            existing_codes[prize_id].add(code)
            added_codes += 1

    summary = (
        f"Imported {added_codes} code(s) across {len(prize_ids)} prize(s) "
        f"({created_prizes} new prize(s) created, {skipped_dupes} duplicate code(s) skipped"
    )
    if given_column:
        summary += f", {skipped_given} already-given row(s) skipped"
    click.echo(summary + ").")


@main.group()
def giveaway():
    """Manage giveaways."""


@giveaway.command("create")
def giveaway_create():
    session_factory, config = _connect()

    with session_factory() as session:
        stmt = (
            select(Prize.id, Prize.name, func.count(GiveawayCode.id))
            .join(GiveawayCode, GiveawayCode.prize_id == Prize.id)
            .where(Prize.active.is_(True), GiveawayCode.assigned_to_discord_id.is_(None))
            .group_by(Prize.id, Prize.name)
        )
        rows = session.execute(stmt).all()

    if not rows:
        click.echo("No active prizes with unused codes. Load some codes first.", err=True)
        sys.exit(1)

    prize_id = questionary.select(
        "Select prize pool:",
        choices=[
            questionary.Choice(title=f"{name} ({count} left)", value=prize_id) for prize_id, name, count in rows
        ],
    ).ask()
    if prize_id is None:
        return

    duration_hours = questionary.text("Duration (hours):", default="24").ask()
    channel_id = questionary.text("Channel ID:").ask()
    guild_id = questionary.text("Guild ID:", default=str(config.guild_id)).ask()
    if not duration_hours or not channel_id or not guild_id:
        click.echo("Cancelled.")
        return

    with session_factory.begin() as session:
        session.add(
            Giveaway(
                prize_id=prize_id,
                guild_id=int(guild_id),
                channel_id=int(channel_id),
                duration_hours=int(duration_hours),
            )
        )

    click.echo("Giveaway queued. The bot will post it within about a minute.")


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
            if g.winner_discord_id:
                username = _resolve_username(g.winner_discord_id, config.discord_bot_token, username_cache)
                detail = f"winner={g.winner_discord_id} ({username})"
            else:
                detail = f"channel={g.channel_id} ends_at={g.ends_at}"
            click.echo(f"{g.id:>4}  {g.status:<9} prize={prize_name:<25} {detail}")


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
