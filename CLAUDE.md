# CLAUDE.md

Project-specific notes for working on the Fuzzwork Discord bot. See `README.md` for
user-facing setup/usage; this file is about how the code is structured and why, for
whoever (human or Claude) touches it next.

## Layout

- `src/fuzzworkbot/` — the bot itself.
  - `config.py` — `load_config()` reads env vars (via python-dotenv) and raises if
    anything required is missing. Call this, don't read `os.environ` directly elsewhere.
  - `db.py` — SQLAlchemy 2.0 declarative models + `make_session_factory(url)`, which
    also runs `Base.metadata.create_all()`. There's no migration tool — schema changes
    are additive columns/tables applied by hand against the live `discordbot` MySQL DB.
  - `esi.py` — ESI character-name lookup via a `requests_cache.CachedSession`.
  - `bot.py` — builds the `InteractionBot`, attaches `bot.config` and
    `bot.session_factory`, loads the cogs, entrypoint (`fuzzworkbot` console script).
  - `cogs/verification.py`, `cogs/giveaways.py`, `cogs/role_reactions.py`.
- `src/fuzzworkbot_cli/cli.py` — `giveaway-cli` console script (click + questionary).
  Talks to MySQL directly via the same `db.py` models. It never talks to Discord and
  has no connection to the running bot process — see "CLI/bot decoupling" below.
- `systemd/fuzzworkbot.service` — installed at `/etc/systemd/system/fuzzworkbot.service`
  (`systemctl {status,restart,...} fuzzworkbot`).
- `legacy/` — archived pre-rewrite Python 3.9 bot (`fuzzworkbot.py`, `swiftbot.py`) and
  its old venv. Reference only, nothing imports it. Safe to delete once confident.

- `web/discord-auth/` — the companion EVE SSO / Discord OAuth PHP app, which writes
  into the `userlookup` table in the same MySQL database this bot reads — see its own
  `README.md`. **This is a version-controlled copy, not a symlink**: the live
  deployment is `/home/web/fuzzwork/htdocs/discord-auth/` on the server, a separate
  copy on disk. Edits here don't take effect until manually copied there, and vice
  versa — if you edit one, check whether the other needs the same change.

## Conventions

- **SQLAlchemy 2.0 idioms only**: declarative `Mapped`/`mapped_column`, `select()` +
  `session.scalar(s)`/`session.get()`, and `session_factory.begin()` as a context
  manager for any write — never rely on session autobegin, and never index a `Row`
  positionally/by dict key.
- Cogs call `load_config()` again at module import time (in addition to `bot.py`
  calling it once to build the bot). This is intentional, not a mistake: disnake's
  `guild_ids=[...]` on `@commands.slash_command` needs a value at class-definition
  time, before the bot instance (and its `bot.config`) exists.
- The CLI and the bot's background loops are the only two writers of giveaway/role-menu
  state, and they coordinate purely through MySQL rows, not IPC:
  - `giveaway-cli giveaway create` and `/creategiveaway` both just insert a `pending`
    `Giveaway` row. The bot's `cogs/giveaways.py` `tasks.loop(minutes=1)` poller is what
    actually posts it and later closes it out.
  - `giveaway-cli role-menu add/remove` just edits `role_reactions` rows; the bot's raw
    reaction listeners read that table live on every reaction event.
  - Don't add a socket/HTTP bridge between the CLI and the bot for this — the DB-poll
    pattern is deliberate and keeps the CLI usable without the bot process being up.
- `Giveaway.status` is a plain `String` column with constants (`STATUS_PENDING` etc. in
  `db.py`), not a DB enum type — keep it that way for a 3-value field, avoids MySQL enum
  migration friction.

## Secrets

- Real secrets live in `.env` (this repo, gitignored, keep `chmod 600`) and in
  `/home/web/fuzzwork/htdocs/discord-auth/secret.php` (gitignored there too). Never
  write fabricated or placeholder-looking values into either of those two files
  specifically — if you don't have the real value, leave an obvious `<placeholder>`
  and say so, don't invent something that looks plausible.
- Never print the contents of `.env` or `secret.php` back into chat, logs, or commits.
- If you ever find a real token/password hardcoded in source again (like the old
  `legacy/` files had), flag it for rotation — don't just move it into a config file
  and call it done.

## Testing without live Discord/MySQL

```bash
uv sync
DISCORD_BOT_TOKEN=x GUILD_ID=1 VALIDATED_ROLE_ID=1 DATABASE_URL=sqlite:///:memory: ESI_USER_AGENT=x \
  uv run python -c "import fuzzworkbot.bot"
```

This exercises config loading, ORM model definitions/table creation, and cog imports
without needing real credentials or a Discord connection. The CLI's non-`questionary`
subcommands (`prize`, `codes`, `role-menu`) can be smoke-tested the same way against a
scratch sqlite file passed as `DATABASE_URL`; `giveaway create` needs a real TTY because
of the interactive prompts.

## Known limitations (intentional, not bugs)

- Role-reaction startup reconciliation only *adds* missing roles for current reactors;
  it doesn't detect "someone un-reacted while the bot was offline" and remove the role.
  Would need a full member/reaction diff per tracked message — not worth the complexity
  unless it actually comes up.
- If a prize's code pool runs dry mid-giveaway, the winner is still announced publicly,
  but the bot just posts that an admin needs to deliver a code manually — it doesn't
  block the draw or retry.
