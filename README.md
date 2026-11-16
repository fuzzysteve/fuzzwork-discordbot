# Fuzzwork Discord Bot

Discord bot for the Fuzzwork EVE Online community: EVE character verification, prize
giveaways, and reaction-based role assignment. Python 3.12+, [disnake](https://docs.disnake.dev/),
SQLAlchemy 2.0, MySQL, managed with [uv](https://docs.astral.sh/uv/).

## Features

- **`/authme`, `/auththem`** — looks up the caller's (or another user's) linked EVE
  character, assigns a "verified" role, and syncs their nickname to their EVE character
  name. The actual EVE SSO + Discord OAuth login happens in a separate companion PHP
  app — see [EVE verification](#eve-verification) below.
- **`/creategiveaway`** (and `giveaway-cli giveaway create`, or `giveaway-cli giveaway
  random` for a fully unattended one — see [Automated random
  giveaways](#automated-random-giveaways-optional)) — reaction giveaways drawn
  from pre-loaded prize/code pools, with one or more winners. The posted message states
  the end time in UTC; React to enter. After the configured duration, that many random
  reactors are picked, each DM'd their own code, and the winners announced publicly.
  Creating a giveaway checks that enough codes actually exist for the requested winner
  count — accounting for codes already promised to other still-running giveaways for
  the same prize — so you can't over-promise a prize pool.
- **Role reactions** — react to an existing message (e.g. in `#acl-management`) to gain
  a role; remove the reaction to lose it. Configured entirely via the CLI, no redeploy
  needed.
- **`/type`, `/system`** — EVE item/ship and solar system lookups by name (with
  autocomplete), read from Fuzzwork's own live SDE database. See
  [EVE data lookups](#eve-data-lookups) below.

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
| `SDE_DATABASE_URL` | Optional. Defaults to `DATABASE_URL` with the schema swapped to `eve` (same host/user/password) — only set this if the SDE ever lives somewhere else. See [EVE data lookups](#eve-data-lookups). |

## Running as a service

The bot, the random-giveaway timer and the watchdog run as a dedicated system user,
`fuzzworkbot`, under a systemd sandbox (read-only filesystem except
`/var/lib/fuzzworkbot` and `/var/cache/fuzzworkbot`, no privileges). They run the venv
directly rather than through `uv run`, so after changing dependencies run `uv sync`
before restarting. Only the dependency-update check still runs as root; see its unit
for why. One-time setup:

```bash
sudo useradd --system --user-group --no-create-home --home-dir /var/lib/fuzzworkbot \
  --shell /usr/sbin/nologin --comment "Fuzzwork Discord bot" fuzzworkbot
sudo chown root:fuzzworkbot .env && sudo chmod 640 .env   # readable by the bot, not writable
```

```bash
sudo cp systemd/fuzzworkbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fuzzworkbot

systemctl status fuzzworkbot
journalctl -u fuzzworkbot -f
```

`Restart=on-failure` is set, so the bot comes back up if it crashes.

### Automated random giveaways (optional)

A systemd timer that periodically runs `giveaway-cli giveaway random` (see below) —
picks a random active prize with enough available codes and queues a giveaway for it,
unattended:

```bash
sudo cp systemd/fuzzworkbot-random-giveaway.service /etc/systemd/system/
sudo cp systemd/fuzzworkbot-random-giveaway.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fuzzworkbot-random-giveaway.timer

systemctl list-timers fuzzworkbot-random-giveaway.timer   # confirm the next scheduled run
journalctl -u fuzzworkbot-random-giveaway.service -f       # each run's output
```

The timer fires every 48h (`OnUnitActiveSec=48h`, matching `giveaway random`'s own
default `--duration-hours`, so a new one starts roughly as the previous wraps up) —
**deliberately with no `OnBootSec=`**. A short `OnBootSec` on a box with real uptime is
already "elapsed" the moment the timer is enabled, and combined with `Persistent=true`
that makes systemd treat it as a missed run and fire *immediately* on enable — this
created two unplanned real giveaways the first time this was set up. Don't add
`OnBootSec=` back without a large value; `OnUnitActiveSec` alone correctly waits the
full interval before its first run.

To choose what the timer runs create instead of leaving them random, queue prizes with
`/giveawayqueue add` in Discord (Manage Server permission) or `giveaway-cli giveaway
queue add`. Each run uses the first queued prize that can run and removes it from the
queue. A queued prize that's inactive or short of codes when the timer fires is skipped
and stays queued (the warning shows in the journal). With nothing runnable queued, the
run picks a random prize.

### Poller watchdog (optional)

Alerts in the admin channel (972857594257498153) if the bot's giveaway poller hasn't
completed a run in 10 minutes, and again once it recovers:

```bash
sudo cp systemd/fuzzworkbot-watchdog.service /etc/systemd/system/
sudo cp systemd/fuzzworkbot-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fuzzworkbot-watchdog.timer
uv run fuzzworkbot-watchdog --dry-run   # what it would post right now
```

## Discord commands

- `/authme`, `/auththem` — EVE character verification (`/auththem` replies privately).
- `/creategiveaway` (Manage Server) — queue a one-off giveaway, now or at a set
  `start_at` (EVE time/UTC, `YYYY-MM-DD HH:MM`, up to 90 days ahead; codes are reserved
  from when you create it). Members enter with the post's **Enter giveaway** button; the
  post shows a live countdown and entrant count.
- `/giveaway cancel|endnow` (Manage Server) — cancel a scheduled or running giveaway (no
  winners, its reserved codes are freed), or end a running one now and draw immediately.
- `/prize list|import` (Manage Server) — code counts per prize (available / reserved;
  prizes with no codes left are hidden unless `show_empty`),
  and importing codes from an attached file: a CSV with `prize`,`code` (and optional
  `given`) columns like `codes import-csv`, or one code per line plus the `prize` option
  like `codes load`. Replies are private and never show codes.
- `/giveawayqueue add|list|remove|clear` (Manage Server) — the timer's prize queue.
- `/rolemenu create|add|remove|list` (Manage Roles) — reaction role menus. `create`
  posts a menu; `add` takes the menu (pick from the list, or paste any message's link),
  an emoji and a role. You can only add roles below your own highest role, and below
  the bot's.
- `/type`, `/system` — EVE item and solar system lookups.

## Admin CLI (`giveaway-cli`)

Run from `/home/discordbot` (needs the same `.env`/`DATABASE_URL` as the bot — it talks
to MySQL directly, it does **not** go through the running bot process):

```bash
uv run giveaway-cli prize add                       # interactive: name, description
uv run giveaway-cli prize list                       # shows unused / available / reserved counts;
                                                      # prizes with no codes left are hidden, --all shows them

uv run giveaway-cli codes load "PLEX x2"             # paste codes, blank line to finish
uv run giveaway-cli codes load "PLEX x2" --file codes.txt
uv run giveaway-cli codes import-csv export.csv      # bulk-import multiple prizes; needs
                                                      # 'prize'/'code' header columns, order
                                                      # doesn't matter, optional 'given' column
uv run giveaway-cli codes list "PLEX x2"             # shows each code and who (if anyone) got it
uv run giveaway-cli codes redeem                     # menu-driven: pick prize, pick recipient
                                                      # type (Discord user / free-text note), pulls
                                                      # a random unused code for that prize and
                                                      # marks it used — for handing a code out
                                                      # manually, outside a giveaway
uv run giveaway-cli codes redeem-random               # same, but the prize is also picked at
                                                      # random from active prizes
uv run giveaway-cli codes redeem "PLEX x2" --for <discord-id>       # non-interactive form, for
                                                      # scripting — skips the menus; exactly one
uv run giveaway-cli codes redeem "PLEX x2" --note "forum: JohnDoe"  # of --for/--note

uv run giveaway-cli giveaway create                  # interactive: pick prize, duration, winners, channel,
                                                      # optional start time (UTC) to schedule it
uv run giveaway-cli giveaway random                  # non-interactive: random eligible prize,
                                                      # defaults --duration-hours=48 --winners=1
                                                      # --channel-id=825889984120356894; for cron/timers
uv run giveaway-cli giveaway queue add "PLEX x2"     # queue a prize for the timer's `giveaway random`
                                                      # runs, in order; optional --winners N; omit
                                                      # PRIZE for a menu. Same as /giveawayqueue add
uv run giveaway-cli giveaway queue list               # next-up first, with #ids
uv run giveaway-cli giveaway queue remove <id>        # drop one entry
uv run giveaway-cli giveaway queue clear              # back to random picks
uv run giveaway-cli giveaway list                    # shows winner(s) for finished giveaways
uv run giveaway-cli giveaway winners <id>             # winners + their actual codes for one giveaway, CLI-only

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

1. **Pending → active**: posts the giveaway embed to the channel (prize, winner count,
   entrant count, and a countdown that Discord shows in each reader's own timezone) with
   an **Enter giveaway** button, records the message ID, sets `starts_at`/`ends_at`.
   Pressing the button records a `giveaway_entries` row and confirms privately; the
   poller refreshes the entrant count on the post each minute when it's changed.
2. **Active, past `ends_at`**: draws up to `winner_count` distinct entrants (fewer if
   there weren't enough), disables the button, and for
   each winner claims one unused code from that prize's pool, DMs them their own code,
   and records the win in `giveaway_winners`. Posts one public announcement listing all
   winners (without codes). Giveaways posted before the button existed are still entered
   and drawn by reactions. If a DM fails (winner has DMs closed) or the prize
   unexpectedly runs out of codes, that winner's line says so and an admin needs to
   deliver the code by hand — this shouldn't normally happen, since both `/creategiveaway`
   and `giveaway-cli giveaway create` check capacity before ever creating the giveaway.

All timestamps (`created_at`/`starts_at`/`ends_at`) are naive datetimes that are always
UTC (`fuzzworkbot.db.now_utc()`) — never mix in server-local time here, the box this
runs on is not UTC (`Europe/Berlin`).

### Giveaway capacity checks

Every `GiveawayCode` row for a prize is "unused" (`assigned_at IS NULL`)
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

## EVE data lookups

`/type <name>` and `/system <name>` (`cogs/eve_lookup.py`, `sde.py`) read from
Fuzzwork's own live SDE database — the `eve` MySQL schema on the same server, already
used by many fuzzwork.co.uk tools (`invTypes`, `mapDenormalize`, etc.), **not** this
bot's own `discordbot` database. The `discordbot` MySQL user has been granted read-only
(`SELECT`) access to it; `SDE_DATABASE_URL` defaults to `DATABASE_URL` with the schema
swapped, so no separate credential is needed unless the SDE ever moves elsewhere.

Both commands are rate-limited to once per 30 seconds per user
(`@commands.cooldown(1, 30, commands.BucketType.user)`); hitting it gets an ephemeral
"Slow down" reply instead of a raw error.

- `/type` — looks up any published item/ship by exact name (autocomplete suggests
  matches as you type — prefix matches first, then substring matches, so typing "Rif"
  surfaces "Rifter" instead of 25 unrelated items that merely contain "rif"). Shows
  group/category, mass/volume/cargo. If the item is a ship (category `Ship`), also
  shows a curated set of dogma attributes grouped like a fitting tool would (slots,
  hardpoints, CPU/powergrid, tank, capacitor, speed, targeting, drones) — see
  `sde.SHIP_ATTRIBUTE_GROUPS` for exactly which attributes and how they're grouped;
  there are 100+ raw dogma attributes per ship and most are internal/irrelevant, so this
  is deliberately a curated subset, not a dump.
- `/system <name>` — looks up a solar system by exact name and replies with two embeds:
  1. the system's own details: `solarSystemID`/`constellationID`/`regionID` resolved to
     names (via `mapSolarSystems`/`mapConstellations`/`mapRegions`, not left as bare
     numbers), plus `itemID`/`typeID`/`groupID`/`orbitID`/`radius`/`security`. `x`/`y`/`z`
     and `celestialIndex`/`orbitIndex` are deliberately left out — raw coordinates
     without the rest of the position math aren't useful in a summary card. Also
     **Neighbours** — every system one stargate jump away, resolved through `mapJumps`
     (stargate → its far-end stargate → that stargate's system). Empty for systems with
     no stargates (most wormhole/J-space systems).
  2. its **contents** — every other `mapDenormalize` row sharing that `solarSystemID`
     (sun, planets, moons, stations, stargates, belts, ...), grouped by type with a
     count, e.g. `Moon (33)`, `Station (18)`. A busy system's full name list can exceed
     Discord's 1024-char field limit (Jita has 68 mapDenormalize rows total) — each
     group's field is truncated with a "… and N more" suffix rather than erroring.

## Role reactions

`role_reactions` rows map `(message_id, emoji) → role_id`. Set them up with `/rolemenu`
in Discord or `giveaway-cli role-menu`. Menus posted by `/rolemenu create` also get a
`role_menus` row, so the bot keeps the menu's role list up to date. The bot listens for raw
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
- `giveaways(id, prize_id, guild_id, channel_id, message_id, created_by_discord_id, duration_hours, status, created_at, starts_at, ends_at, winner_count, entry_mode)` —
  also still has unused `winner_discord_id`/`code_id` columns left over from the
  single-winner design (see `db.py`'s comment; dropping them live was judged too risky
  to force through, they're just dead columns now, don't resurrect them)
- `giveaway_winners(id, giveaway_id, discord_id, code_id, drawn_at)` — one row per
  winner per giveaway; `code_id` is null only in the (shouldn't-happen) case where the
  prize ran out of codes at draw time
- `giveaway_entries(id, giveaway_id, discord_id, entered_at)` — button entries, unique
  per giveaway and user
- `giveaway_queue(id, prize_id, winner_count, added_by_discord_id, added_at)` — prizes
  queued for the timer, used in id order
- `role_reactions(id, guild_id, channel_id, message_id, emoji, role_id, created_at)`
- `role_menus(message_id, guild_id, channel_id, title, description, created_by_discord_id, created_at)`
  — menus the bot posted via `/rolemenu create`

## Security

- `.env` holds live credentials — it's gitignored and should stay `root:fuzzworkbot`,
  mode `640`: readable by the bot's user, writable only by root, unreadable by anyone
  else. (`load_dotenv()` fails outright on a `.env` it can't read, even though systemd
  has already passed the values in, so the bot's group must be able to read it.)
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
