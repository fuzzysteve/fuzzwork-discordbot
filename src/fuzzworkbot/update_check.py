"""Daily dependency check: reports outdated Python packages (this repo's uv venv, the
compression optimizer's and fuzzwork pyapi's) and outdated direct Composer packages (the live discord-auth PHP app) to a Discord channel.

Read-only — it never upgrades anything. Posts via Discord's REST API with the bot
token, no gateway connection, so it works whether or not the bot process is up.
Driven by systemd/fuzzworkbot-update-check.{service,timer}.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import click
import requests

from fuzzworkbot.config import load_config

DISCORD_API_BASE = "https://discord.com/api/v10"
DEFAULT_CHANNEL_ID = 972857594257498153
REPO_DIR = Path(__file__).resolve().parents[2]
LIVE_PHP_APP_DIR = Path("/home/web/fuzzwork/htdocs/discord-auth")
COMPRESSION_DIR = Path("/home/scripts/compression")
PYAPI_DIR = Path("/home/web/fuzzwork/pyapi")
DISCORD_MESSAGE_LIMIT = 2000


def _python_outdated(project_dir: Path) -> list[tuple[str, str, str]]:
    # `uv run` exports its own path as $UV; fall back to PATH for manual runs.
    uv = os.environ.get("UV") or shutil.which("uv") or "/root/.local/bin/uv"
    result = subprocess.run(
        # Explicit --python: under `uv run`, $VIRTUAL_ENV is the bot's venv and uv pip
        # would follow it regardless of cwd.
        [uv, "pip", "list", "--outdated", "--format", "json",
         "--python", str(project_dir / ".venv")],
        cwd=project_dir, capture_output=True, text=True, check=True, timeout=120,
    )
    return [(p["name"], p["version"], p["latest_version"]) for p in json.loads(result.stdout)]


def _composer_outdated() -> list[tuple[str, str, str]]:
    # Runs as root under systemd; --no-plugins/--no-scripts keep that safe for a
    # read-only `outdated` query, which is all COMPOSER_ALLOW_SUPERUSER permits here.
    result = subprocess.run(
        ["composer", "outdated", "--direct", "--format=json", "--no-plugins", "--no-scripts",
         f"--working-dir={LIVE_PHP_APP_DIR}"],
        env={**os.environ, "COMPOSER_ALLOW_SUPERUSER": "1"},
        capture_output=True, text=True, check=True, timeout=120,
    )
    return [(p["name"], p["version"], p["latest"]) for p in json.loads(result.stdout)["installed"]]


def _section(title: str, how_to_update: str, check) -> list[str]:
    try:
        outdated = check()
    except Exception as e:  # report the failure instead of silently skipping the section
        return [f"**{title}** — check failed: `{type(e).__name__}: {e}`"[:500]]
    if not outdated:
        return []
    lines = [f"**{title}** — {len(outdated)} outdated (`{how_to_update}`)", "```"]
    lines += [f"{name:<28} {current:>12} -> {latest}" for name, current, latest in outdated]
    lines.append("```")
    return lines


def _post(token: str, channel_id: int, content: str) -> None:
    if len(content) > DISCORD_MESSAGE_LIMIT:
        content = content[: DISCORD_MESSAGE_LIMIT - 20] + "\n```\n…(truncated)"
    response = requests.post(
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
        headers={"Authorization": f"Bot {token}"},
        json={"content": content, "allowed_mentions": {"parse": []}},
        timeout=10,
    )
    response.raise_for_status()


@click.command(help="Check for outdated Python/Composer dependencies and post them to Discord.")
@click.option("--channel-id", type=int, default=DEFAULT_CHANNEL_ID, show_default=True)
@click.option("--dry-run", is_flag=True, help="Print the message instead of posting it.")
def main(channel_id: int, dry_run: bool):
    lines = _section(
        "Python (bot)", "uv lock --upgrade && uv sync", lambda: _python_outdated(REPO_DIR)
    )
    lines += _section(
        "Python (compression)",
        f"cd {COMPRESSION_DIR} && uv lock --upgrade && uv sync && systemctl restart eve-compression",
        lambda: _python_outdated(COMPRESSION_DIR),
    )
    lines += _section(
        "Python (pyapi)",
        f"cd {PYAPI_DIR} && uv lock --upgrade && uv sync && systemctl restart fuzzwork-pyapi",
        lambda: _python_outdated(PYAPI_DIR),
    )
    lines += _section(
        "PHP (discord-auth, live)", f"cd {LIVE_PHP_APP_DIR} && composer update", _composer_outdated
    )
    if not lines:
        click.echo("Everything up to date; nothing posted.")
        return

    content = "\n".join([":package: **Dependency updates available**", *lines])
    if dry_run:
        click.echo(content)
        return
    _post(load_config().discord_bot_token, channel_id, content)
    click.echo(f"Posted update report to channel {channel_id}.")


if __name__ == "__main__":
    sys.exit(main())
