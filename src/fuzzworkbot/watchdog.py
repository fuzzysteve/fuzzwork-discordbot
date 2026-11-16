"""Giveaway-poller watchdog.

The bot writes HEARTBEAT_FILE at the end of every poller run that completed without
errors (see cogs/giveaways.py). `fuzzworkbot-watchdog`, run every 5 minutes by
systemd/fuzzworkbot-watchdog.timer, posts an alert to Discord when the heartbeat is
older than STALE_AFTER, and a recovery notice once it's fresh again. It runs outside the
bot process, so it catches every way the poller can stop: the loop dying, the process
hanging or crash-looping, the bot being stopped, or a database outage that makes every
run fail.

Posts via Discord's REST API with the bot token (no gateway connection), so it works
whether or not the bot is up.
"""

import datetime
import json
import sys
from pathlib import Path

import click

from fuzzworkbot.config import load_config
from fuzzworkbot.db import now_utc

HEARTBEAT_FILE = Path("/var/lib/fuzzworkbot/poller-heartbeat")
STATE_FILE = Path("/var/lib/fuzzworkbot/watchdog.json")
# The poller runs every minute; 10 minutes rides out a slow run or a quick restart.
STALE_AFTER = datetime.timedelta(minutes=10)
# Same admin channel the daily dependency report goes to.
DEFAULT_CHANNEL_ID = 972857594257498153


def write_heartbeat() -> None:
    HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_FILE.write_text(now_utc().isoformat())


def read_heartbeat() -> datetime.datetime | None:
    """Last successful poller run (naive UTC), or None if there's no readable heartbeat."""
    try:
        return datetime.datetime.fromisoformat(HEARTBEAT_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _load_alerting() -> bool:
    try:
        return bool(json.loads(STATE_FILE.read_text()).get("alerting"))
    except (FileNotFoundError, ValueError):
        return False


def _save_alerting(alerting: bool) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"alerting": alerting}))


def _age_text(last: datetime.datetime | None, now: datetime.datetime) -> str:
    if last is None:
        return "no heartbeat recorded"
    minutes = int((now - last).total_seconds() // 60)
    return f"last successful run {last:%Y-%m-%d %H:%M} UTC, {minutes} min ago"


@click.command(help="Alert on Discord if the bot's giveaway poller has stopped running.")
@click.option("--channel-id", type=int, default=DEFAULT_CHANNEL_ID, show_default=True)
@click.option("--dry-run", is_flag=True, help="Print what would be posted instead of posting it.")
def main(channel_id: int, dry_run: bool):
    # Imported here, not at module level: update_check pulls in subprocess-heavy helpers
    # the bot process (which imports this module for write_heartbeat) doesn't need.
    from fuzzworkbot.update_check import _post

    now = now_utc()
    last = read_heartbeat()
    stale = last is None or now - last > STALE_AFTER
    alerting = _load_alerting()

    if stale and not alerting:
        message = (
            f":rotating_light: **Giveaway poller has stopped** ({_age_text(last, now)}). Giveaways won't "
            "post or finish until it's back. Check `systemctl status fuzzworkbot` and "
            "`journalctl -u fuzzworkbot`."
        )
    elif not stale and alerting:
        message = f":white_check_mark: **Giveaway poller is running again** ({_age_text(last, now)})."
    else:
        click.echo(f"Poller {'still down' if stale else 'OK'}: {_age_text(last, now)}. Nothing posted.")
        return

    if dry_run:
        click.echo(message)
        return
    _post(load_config().discord_bot_token, channel_id, message)
    _save_alerting(stale)
    click.echo(f"Posted to channel {channel_id}: {message}")


if __name__ == "__main__":
    sys.exit(main())
