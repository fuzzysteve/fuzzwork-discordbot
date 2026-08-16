<?php

session_start();

require __DIR__ . '/secret.php';

$stateid = bin2hex(random_bytes(16));

$_SESSION['stateid'] = $stateid;

header('Location: https://login.eveonline.com/v2/oauth/authorize?response_type=code&redirect_uri='.urlencode('https://www.fuzzwork.co.uk/discord-auth/callback.php').'&client_id='.$eve_clientid.'&state='.$stateid);
