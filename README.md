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
  from pre-loaded prize/code pools. React to the posted message; after the configured
  duration, a random reactor is picked, DM'd their code, and announced publicly.
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
uv run giveaway-cli prize list

uv run giveaway-cli codes load "PLEX x2"             # paste codes, blank line to finish
uv run giveaway-cli codes load "PLEX x2" --file codes.txt

uv run giveaway-cli giveaway create                  # interactive: pick prize, duration, channel
uv run giveaway-cli giveaway list

uv run giveaway-cli role-menu add \
  --guild-id <id> --channel-id <id> --message-id <id> \
  --emoji "🛡️" --role-id <id>
uv run giveaway-cli role-menu list
uv run giveaway-cli role-menu remove --message-id <id> --emoji "🛡️"
```

Channel/message/role/guild IDs all come from Discord's right-click "Copy ID" menus
(requires Developer Mode: User Settings → Advanced → Developer Mode).

## How giveaways work

A `Giveaway` row moves through `pending → active → finished`. Both `/creategiveaway`
and `giveaway-cli giveaway create` just insert a `pending` row — a background poller in
the bot (`cogs/giveaways.py`, runs every minute) is what actually posts the message and
later closes it out:

1. **Pending → active**: posts the giveaway embed to the channel, records the message
   ID, sets `starts_at`/`ends_at`.
2. **Active, past `ends_at`**: collects everyone who reacted, picks a random winner,
   claims one unused code from that prize's pool, DMs the winner the code, and posts a
   public winner announcement (without the code). If the DM fails (winner has DMs
   closed) or the prize has run out of codes, it says so publicly and an admin needs to
   deliver the code by hand.

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
- `giveaways(id, prize_id, guild_id, channel_id, message_id, created_by_discord_id, duration_hours, status, created_at, starts_at, ends_at, winner_discord_id, code_id)`
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
