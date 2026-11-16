<?php
// Shared session setup for every entry point.
// - Own cookie name + path, so this app's session never mixes with anything else on
//   www.fuzzwork.co.uk.
// - secure/httponly: cookie only over HTTPS and never readable from JavaScript.
// - SameSite=Lax, not Strict: EVE SSO and Discord send the user back to callback*.php
//   with a cross-site top-level GET redirect. Lax still sends the cookie on that; Strict
//   would drop it, and the state check would then fail every time.
// - use_strict_mode: PHP refuses session IDs it didn't issue itself (blocks fixation
//   with a made-up ID).
ini_set('session.use_strict_mode', '1');
session_name('discordauth');
session_set_cookie_params([
    'lifetime' => 0,
    'path' => '/discord-auth/',
    'secure' => true,
    'httponly' => true,
    'samesite' => 'Lax',
]);
session_start();
