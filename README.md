# Fuzzwork Discord Bot

Discord bot for the Fuzzwork EVE Online community: EVE character verification, prize
giveaways, and reaction-based role assignment. Python 3.12+, [disnake](https://docs.disnake.dev/),
SQLAlchemy 2.0, MySQL, managed with [uv](https://docs.astral.sh/uv/).

## Features

- **`/authme`, `/auththem`** — looks up the caller's (or another user's) linked EVE
  character, assigns a "verified" role, and syncs their nickname to their EVE character
  name. The actual EVE SSO + Discord OAuth login happens in a separate companion PHP
  app — see [EVE verification](#eve-verification) below.
- **`/creategiveaway`** (and `giveaway-cli giveaway create`) — reaction giveaways drawn
  from pre-loaded prize/code pools, with one or more winners. The posted message states
  the end time in UTC; React to enter. After the configured duration, that many random
  reactors are picked, each DM'd their own code, and the winners announced publicly.
  Creating a giveaway checks that enough codes actually exist for the requested winner
  count — accounting for codes already promised to other still-running giveaways for
  the same prize — so you can't over-promise a prize pool.
- **Role reactions** — react to an existing message (e.g. in `#acl-management`) to gain
  a role; remove the reaction to lose it. Configured entirely via the CLI, no redeploy
  needed.

## Requirements

- Python 3.12+ and `uv`
- A MySQL server reachable at `DATABASE_URL`, with a `discordbot` database the
  configured user can create tables in (tables are created automatically on first run)
- A Discord bot application with:
  - **Server Members Intent** enabled (Developer Portal → Bot → Privileged Gateway
    Intents)
  - Invited with the `bot` + `applications.commands` scopes and these permissions:
    View Channels, Send Messages, Embed Links, Read Message History, Manage Roles,
    Manage Nicknames
  - Its own role positioned **above** any role it needs to grant/remove (verification
    role, role-reaction roles) in Server Settings → Roles

## Setup

```bash
cd /home/discordbot
cp .env.example .env   # then fill in real values, see below
uv sync
uv run fuzzworkbot     # foreground run, Ctrl-C to stop — good for first-time testing
```

`.env` keys:

| Key | Notes |
|---|---|
| `DISCORD_BOT_TOKEN` | From the Developer Portal. Rotate if it was ever exposed. |
| `GUILD_ID` | The single guild this bot operates in (right-click server icon → Copy Server ID, needs Developer Mode on). |
| `VALIDATED_ROLE_ID` | Role granted by `/authme`. Copy Role ID from Server Settings → Roles. |
| `DATABASE_URL` | SQLAlchemy URL, e.g. `mysql+pymysql://discordbot:<password>@localhost/discordbot`. |
| `ESI_USER_AGENT` | Sent on ESI requests per CCP's third-party dev guidelines — include a contact. |

## Running as a service

```bash
sudo cp systemd/fuzzworkbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fuzzworkbot

systemctl status fuzzworkbot
journalctl -u fuzzworkbot -f
```

`Restart=on-failure` is set, so the bot comes back up if it crashes.

## Admin CLI (`giveaway-cli`)

Run from `/home/discordbot` (needs the same `.env`/`DATABASE_URL` as the bot — it talks
to MySQL directly, it does **not** go through the running bot process):

```bash
uv run giveaway-cli prize add                       # interactive: name, description
uv run giveaway-cli prize list                       # shows unused / available / reserved counts

uv run giveaway-cli codes load "PLEX x2"             # paste codes, blank line to finish
uv run giveaway-cli codes load "PLEX x2" --file codes.txt
uv run giveaway-cli codes import-csv export.csv      # bulk-import multiple prizes; needs
                                                      # 'prize'/'code' header columns, order
                                                      # doesn't matter, optional 'given' column
uv run giveaway-cli codes list "PLEX x2"             # shows each code and who (if anyone) got it

uv run giveaway-cli giveaway create                  # interactive: pick prize, duration, winners, channel
uv run giveaway-cli giveaway list                    # shows winner(s) for finished giveaways

uv run giveaway-cli role-menu add \
  --guild-id <id> --channel-id <id> --message-id <id> \
  --emoji "🛡️" --role-id <id>
uv run giveaway-cli role-menu list
uv run giveaway-cli role-menu remove --message-id <id> --emoji "🛡️"
```

Channel/message/role/guild IDs all come from Discord's right-click "Copy ID" menus
(requires Developer Mode: User Settings → Advanced → Developer Mode).

### Running it directly + bash completion

`giveaway-cli` is a self-contained executable (its shebang points at the project's own
venv Python), so it doesn't strictly need `uv run` — that's only there so it works
before you've put anything on `PATH`. To run it as a bare `giveaway-cli` command (and to
get tab-completion, which only hooks into the literal command name):

```bash
sudo ln -s /home/discordbot/.venv/bin/giveaway-cli /usr/local/bin/giveaway-cli
echo 'eval "$(_GIVEAWAY_CLI_COMPLETE=bash_source giveaway-cli)"' >> ~/.bashrc
source ~/.bashrc
```

This completes command/subcommand names and flags (`giveaway-cli <TAB>`, `giveaway-cli
role-menu <TAB>`, `--role-<TAB>`, ...). It does not complete dynamic values like actual
prize names — that would need custom Click completion callbacks, not currently wired up.

## How giveaways work

A `Giveaway` row moves through `pending → active → finished`. Both `/creategiveaway`
and `giveaway-cli giveaway create` just insert a `pending` row — a background poller in
the bot (`cogs/giveaways.py`, runs every minute) is what actually posts the message and
later closes it out:

1. **Pending → active**: posts the giveaway embed to the channel — prize, winner count,
   and the end time spelled out in UTC — records the message ID, sets `starts_at`/`ends_at`.
2. **Active, past `ends_at`**: collects everyone who reacted, randomly draws up to
   `winner_count` distinct reactors (fewer if there weren't enough entrants), and for
   each winner claims one unused code from that prize's pool, DMs them their own code,
   and records the win in `giveaway_winners`. Posts one public announcement listing all
   winners (without codes). If a DM fails (winner has DMs closed) or the prize
   unexpectedly runs out of codes, that winner's line says so and an admin needs to
   deliver the code by hand — this shouldn't normally happen, since both `/creategiveaway`
   and `giveaway-cli giveaway create` check capacity before ever creating the giveaway.

All timestamps (`created_at`/`starts_at`/`ends_at`) are naive datetimes that are always
UTC (`fuzzworkbot.db.now_utc()`) — never mix in server-local time here, the box this
runs on is not UTC (`Europe/Berlin`).

### Giveaway capacity checks

Every `GiveawayCode` row for a prize is "unused" (`assigned_to_discord_id IS NULL`)
until a giveaway finish claims it — but a `pending`/`active` giveaway has already
*promised* `winner_count` of them before it finishes. `fuzzworkbot.giveaway_logic`
tracks this:

- `unused_code_count` — raw count of unclaimed codes for a prize.
- `committed_winner_count` — sum of `winner_count` across every `pending`/`active`
  giveaway for that prize (codes they'll need but haven't claimed yet).
- `available_code_count` — `unused - committed`; this is the number actually safe to
  promise to a *new* giveaway, and what both creation paths check against.

Creating a giveaway locks the `Prize` row (`SELECT ... FOR UPDATE`) for the duration of
that check-and-insert, so two admins creating giveaways for the same prize at the same
moment can't both read the same "available" number and both succeed.

## EVE verification

The bot itself never talks to EVE SSO or does OAuth — that's handled by a companion PHP
app at `/home/web/fuzzwork/htdocs/discord-auth/` (served at
`https://www.fuzzwork.co.uk/discord-auth/`), which chains an EVE SSO login into a
Discord OAuth login and writes the resulting `discordid` → `eveid` link into the
`userlookup` table in the same `discordbot` MySQL database. `/authme` just reads that
table, resolves the character name via ESI, and assigns the role. Its source lives in
this repo too, under `web/discord-auth/` — but that's a version-controlled *copy*, not
a symlink to the live path, so changes need to be manually copied to the server to take
effect. See `web/discord-auth/README.md` for details/setup, including `secret.php`.

## Role reactions

`role_reactions` rows map `(message_id, emoji) → role_id`. The bot listens for raw
reaction add/remove events (works even if the message isn't in its cache) and toggles
the role live. On startup it also reconciles: for every tracked message, it grants the
mapped role to anyone currently reacted who's missing it — this self-heals for
reactions added while the bot was offline. It does **not** retroactively remove roles
from people who un-reacted while the bot was down; that would need a full
member/reaction diff and hasn't been needed so far.

## Database schema

- `userlookup(id, discordid, eveid)` — written by the PHP auth app, read by `/authme`.
- `prizes(id, name, description, active, created_at)`
- `giveaway_codes(id, prize_id, code, assigned_to_discord_id, assigned_at, created_at)`
- `giveaways(id, prize_id, guild_id, channel_id, message_id, created_by_discord_id, duration_hours, status, created_at, starts_at, ends_at, winner_count)` —
  also still has unused `winner_discord_id`/`code_id` columns left over from the
  single-winner design (see `db.py`'s comment; dropping them live was judged too risky
  to force through, they're just dead columns now, don't resurrect them)
- `giveaway_winners(id, giveaway_id, discord_id, code_id, drawn_at)` — one row per
  winner per giveaway; `code_id` is null only in the (shouldn't-happen) case where the
  prize ran out of codes at draw time
- `role_reactions(id, guild_id, channel_id, message_id, emoji, role_id, created_at)`

## Security

- `.env` holds live credentials — it's gitignored and should stay `chmod 600`.
- `legacy/` holds the pre-rewrite bot files and old venv, kept only as a reference for
  what used to exist. They had a hardcoded bot token and DB password in plaintext;
  those must be treated as compromised regardless of whether this rewrite is deployed.
  Safe to delete `legacy/` once you're confident you don't need to cross-check anything.

## Troubleshooting

- **Slash commands not showing up**: they're guild-scoped to `GUILD_ID` and can take a
  few minutes to sync after the bot first starts or a command changes.
- **Role/nickname changes silently not happening**: check `journalctl -u fuzzworkbot`
  for `Can't set role or nick` / `Could not add role` warnings — almost always a role
  hierarchy or missing-permission issue (see Requirements above).
- **Giveaway not posting**: the poller runs once a minute, so allow up to ~60s; check
  the logs for `Channel %s not found` warnings if it never appears.
- **"Can't draw N winner(s)" when creating a giveaway**: expected — that prize doesn't
  have enough unused codes once you account for other `pending`/`active` giveaways
  already promised codes from the same pool. Load more codes, lower the winner count,
  or wait for the other giveaway(s) to finish. See "Giveaway capacity checks" above.
