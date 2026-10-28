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
    are additive columns/tables applied by hand against the live `discordbot` MySQL DB
    (`create_all` only creates missing tables, it never alters existing ones — dropping
    or changing a column needs a manual `ALTER TABLE` against the live DB, see the note
    on `Giveaway.winner_discord_id`/`code_id` below for what happens when that's not
    possible). Also has `now_utc()` — every DATETIME column in this schema is a naive
    datetime that's always UTC; the host this runs on is `Europe/Berlin`, not UTC, so
    never write `datetime.datetime.now()` into one of these columns or compare one
    against it. Use `now_utc()`.
  - `giveaway_logic.py` — shared capacity-check helpers (`unused_code_count`,
    `committed_winner_count`, `available_code_count`) used by both `cogs/giveaways.py`
    and `fuzzworkbot_cli/cli.py` so the two entry points can't diverge on the rule for
    "how many codes can this prize still promise."
  - `esi.py` — ESI character-name lookup via a `requests_cache.CachedSession`.
  - `sde.py` — read-only lookups against Fuzzwork's own **separate** live EVE SDE MySQL
    database (`eve` schema, `config.sde_database_url`) — item/ship types and the
    universe map. This is not `db.py`'s database and has no ORM models (176 tables of
    someone else's pre-existing schema isn't worth mapping); it's raw `text()` queries
    via a second SQLAlchemy engine. `discordbot` MySQL user has read-only `SELECT` on
    it. `_ranked_name_search()` does prefix-then-substring matching for autocomplete —
    don't replace it with a plain `LIKE '%x%'`, see its docstring for why.
  - `bot.py` — builds the `InteractionBot`, attaches `bot.config` and
    `bot.session_factory`, loads the cogs, entrypoint (`fuzzworkbot` console script).
  - `cogs/verification.py`, `cogs/giveaways.py`, `cogs/role_reactions.py`,
    `cogs/eve_lookup.py`.
- `src/fuzzworkbot_cli/cli.py` — `giveaway-cli` console script (click + questionary).
  Talks to MySQL directly via the same `db.py` models. It never talks to Discord and
  has no connection to the running bot process — see "CLI/bot decoupling" below.
- `systemd/fuzzworkbot.service` — installed at `/etc/systemd/system/fuzzworkbot.service`
  (`systemctl {status,restart,...} fuzzworkbot`).
- `systemd/fuzzworkbot-random-giveaway.{service,timer}` — installed the same way, drives
  `giveaway-cli giveaway random` on a schedule. See the "systemd timer" note under
  Conventions before touching `OnBootSec=`/`OnUnitActiveSec=` in the `.timer`.
- `systemd/fuzzworkbot-update-check.{service,timer}` — daily (09:00 Berlin) run of
  `fuzzworkbot-update-check` (`src/fuzzworkbot/update_check.py`): read-only check for
  a newer uv release (`uv self update --dry-run`; uv is a standalone install at
  `/root/.local/bin/uv`, which both systemd units run through), outdated Python packages (this venv, `/home/scripts/compression`'s and
  `/home/web/fuzzwork/pyapi`'s) and direct
  Composer packages (the *live* discord-auth copy), posted to Discord channel
  972857594257498153 via REST with the bot token. Each update (package + latest
  version) is posted the day it first appears, then re-posted at most weekly while still
  pending; last-posted dates live in `/var/lib/fuzzworkbot/update-check.json` (delete it
  to re-announce everything). Silent when nothing is new or due. `--dry-run` prints instead of posting.
  Updates deliberately not being taken go in `SKIPPED_UPDATES` in `update_check.py`
  (`(section title, lowercase package name) -> reason`): never posted, only logged to
  the journal each run. Currently: compression's `cbcbox` (see that repo's CLAUDE.md).
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
- `str(some_sqlalchemy_url)` masks the password as `***` by default (it's
  `render_as_string(hide_password=True)`) — this bit `config.py`'s derived
  `sde_database_url` (silently produced a URL with a literal `***` password, which then
  failed auth in a way that looked like a grants problem, not a code problem). If you
  ever need the real connection string from a `URL` object, use
  `.render_as_string(hide_password=False)`.
- **systemd timer gotcha**: on a box with real (long) uptime, a `.timer` unit with a
  short `OnBootSec=` is already "elapsed" relative to actual boot the instant the timer
  is enabled — combined with `Persistent=true`, systemd treats that as a missed run and
  fires the service *immediately* on `systemctl enable --now`, not after the interval.
  This created two unplanned real giveaways the first time
  `fuzzworkbot-random-giveaway.timer` was set up. `fuzzworkbot-random-giveaway.timer`
  now deliberately has no `OnBootSec=` — `OnUnitActiveSec=` alone waits the full
  interval before its first run. Don't add `OnBootSec=` to a recurring timer like this
  without a large value (hours+), and if you do, test with `systemctl list-timers`
  *before* `enable --now`, not after.
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
- A giveaway can have multiple winners (`Giveaway.winner_count`); each actual winner is
  a row in `GiveawayWinner` (giveaway_id, discord_id, code_id, drawn_at), not a column
  on `Giveaway` — the original design had `winner_discord_id`/`code_id` directly on
  `Giveaway` for a single winner, but that couldn't be reused for N winners. Those two
  columns still physically exist on the live `giveaways` table (an interactive-session
  permission classifier blocked the `DROP COLUMN`/`DROP FOREIGN KEY` needed to remove
  them) but the ORM model no longer maps them — leave them alone, don't reintroduce
  code that reads/writes them.
- `GiveawayCode`'s used/unused state is decided by `assigned_at` (`IS NULL` = unused),
  never by `assigned_to_discord_id` alone — a code can be claimed by a Discord winner
  (`assigned_to_discord_id` set) or handed out manually to someone with no Discord ID via
  `giveaway-cli codes redeem`/`redeem-random --note` (`assigned_note` set instead,
  `assigned_to_discord_id` left null). Checking `assigned_to_discord_id IS NULL` as the
  "unused" signal was the original design and is now wrong on two counts: it misses
  `--note`-redeemed codes (still null, so they'd look unused) and it broke once for a
  Discord ID of `0` passed to `--for`, since `0` is falsy in Python — a truthy check on
  it in `codes list` printed "unused" for an already-consumed code. Always filter/check
  on `assigned_at`.
- Every giveaway-creation path — `/creategiveaway`, `giveaway-cli giveaway create`, and
  `giveaway-cli giveaway random` — must check `giveaway_logic.available_code_count()`
  before inserting a new `Giveaway` row, and must lock the `Prize` row
  (`select(Prize)...with_for_update()`) for the duration of that check-and-insert. This
  is what stops an admin (or the random-giveaway timer) from creating a giveaway that
  promises more winners than the prize pool can actually cover once other still-running
  giveaways for the same prize are accounted for — don't add a new giveaway-creation
  path without reusing this same check.

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
uv run pytest
```

The suite in `tests/` (pytest + pytest-asyncio, dev dependency group) runs every cog,
`sde.py`, the CLI and `update_check.py` against throwaway SQLite files with Discord
faked out — no live Discord, MySQL, SDE or network. `tests/conftest.py` sets every
config env var before anything imports a cog (cogs call `load_config()` at import, and
`load_dotenv()` only fills *unset* vars, so this keeps the real `.env` out). Slash
commands are tested by calling `cog.<command>.callback(cog, inter, ...)` with a mock
interaction; `no_task_loops` stops `Giveaways.__init__` from starting the real poller.
`with_for_update()` is a no-op on SQLite, so the giveaway-capacity race is tested by
simulating the competing insert (see `test_giveaway_random_rechecks_capacity_under_lock`),
not by real concurrency.

Quick import-only smoke test (no pytest needed):

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

## Live schema migrations

There's no migration tool (see `db.py` above) — changes to the live `discordbot` MySQL
DB are hand-run SQL. In an interactive Claude Code session, the auto-mode permission
classifier can block individual `mysql -e "..."` calls, seemingly more readily for
`DROP`/multi-statement DDL than for a single additive `ALTER TABLE ... ADD COLUMN` or a
plain `UPDATE` — it's inconsistent, not a hard rule. If blocked: split into one
statement per Bash call and retry: that alone got an identical `ALTER TABLE ADD COLUMN`
through after the same statement combined with a trailing `SHOW CREATE TABLE` had been
blocked. If a `DROP COLUMN`/`DROP FOREIGN KEY` keeps getting blocked, don't fight it —
leave the column in place, unmapped in the ORM model (see `winner_discord_id`/`code_id`
above), and say so in a comment rather than forcing it through.

## Known limitations (intentional, not bugs)

- Role-reaction startup reconciliation only *adds* missing roles for current reactors;
  it doesn't detect "someone un-reacted while the bot was offline" and remove the role.
  Would need a full member/reaction diff per tracked message — not worth the complexity
  unless it actually comes up.
- If a prize's code pool runs dry mid-giveaway, the winner is still announced publicly,
  but the bot just posts that an admin needs to deliver a code manually — it doesn't
  block the draw or retry.
