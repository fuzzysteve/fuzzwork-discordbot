# discord-auth

Web app served at `https://www.fuzzwork.co.uk/discord-auth/` that links a Discord
account to an EVE Online character, for the Fuzzwork Discord bot's `/authme` command
(see `../../` for the bot, which reads the table this app writes).

**This directory is a version-controlled copy.** The live deployment is
`/home/web/fuzzwork/htdocs/discord-auth/` on the server, which is a separate copy on
disk (not a symlink to this repo) — after editing files here, they still need to be
copied back to the live path to actually take effect, and vice versa. Keep the two in
sync by hand.

## Flow

Three-step OAuth chain, each step redirecting into the next:

1. **`index.php`** — generates a random `state`, stores it in session, redirects to EVE
   SSO's authorize endpoint.
2. **`callback.php`** — EVE SSO redirects back here with `code` + `state`. Verifies
   `state` against session (CSRF check), exchanges `code` for a token, decodes the JWT
   to get the character ID/name, stashes those in session, then generates a *new*
   random `state` and redirects into Discord's OAuth authorize endpoint (`identify`
   scope only).
3. **`callback2.php`** — Discord redirects back here with its own `code` + `state`.
   Verifies that state, exchanges the code, fetches the Discord user via
   `GET /users/@me`, and — if this Discord ID isn't already linked — inserts
   `(discordid, eveid)` into the `userlookup` table in the `discordbot` MySQL database.
   Tells the user to go run `/authme` in Discord.

If a Discord ID is already linked, `callback2.php` refuses to re-link (prints "We
already know who you are." and exits) rather than overwriting the existing character.

## Files

- `index.php`, `callback.php`, `callback2.php` — the flow above.
- `db.inc.php` — opens the PDO connection to the `discordbot` database.
- `secret.php` — **not committed** (gitignored), holds `$eve_clientid`/`$eve_secret`,
  `$discord_clientid`/`$discord_secret`, and `$db_password`. Every other file
  `require`s it. `secret.php.example` shows the shape with placeholder values — copy it
  to `secret.php` and fill in real ones. See below.
- `vendor/` (Composer, `guzzlehttp/guzzle`) — used only by `callback2.php` for the
  Discord OAuth/API calls; `callback.php` uses raw cURL for the EVE SSO calls instead
  (this asymmetry is pre-existing, not a bug to fix opportunistically).

## Setting up `secret.php`

Copy the example and fill in real values — `secret.php` itself is gitignored, every
other file `require`s it:

```bash
cp secret.php.example secret.php
```

```php
<?php
$eve_clientid = '...';      // EVE SSO application client ID
$eve_secret = '...';        // EVE SSO application client secret
$discord_clientid = '...';  // Discord application client ID
$discord_secret = '...';    // Discord application OAuth2 client secret
$db_password = '...';       // MySQL password for the `discordbot` user
```

The EVE SSO app's redirect URI must be exactly
`https://www.fuzzwork.co.uk/discord-auth/callback.php`; the Discord app's must be
exactly `https://www.fuzzwork.co.uk/discord-auth/callback2.php`, and the Discord app
needs the `identify` scope available under OAuth2.

If any of these secrets were ever exposed in plaintext (they were, in earlier versions
of these files, which is why they're external now), rotate them via the respective
developer portal and MySQL, then update `secret.php` — don't just copy the old values
back in.
