<?php
session_start();

function auth_error($error_message)
{
    print "There's been an error";
    error_log($error_message);
    exit();
}

require __DIR__ . '/secret.php';

$useragent="Fuzzwork Auth agent 1.0";

$code=$_GET['code'] ?? null;
$state=$_GET['state'] ?? null;

// CSRF check: state must match what index.php stored before sending the user to EVE SSO.
if ($state === null || !isset($_SESSION['stateid']) || !hash_equals($_SESSION['stateid'], $state)) {
    auth_error('EVE SSO state mismatch');
}
unset($_SESSION['stateid']);

//Do the initial check.
$url='https://login.eveonline.com/v2/oauth/token';
$header='Authorization: Basic '.base64_encode($eve_clientid.':'.$eve_secret);
$fields=array(
            'grant_type' => 'authorization_code',
            'code' => $code
        );
$fields_string=http_build_query($fields);
$ch = curl_init();
curl_setopt($ch, CURLOPT_URL, $url);
curl_setopt($ch, CURLOPT_USERAGENT, $useragent);
curl_setopt($ch, CURLOPT_HTTPHEADER, array($header));
curl_setopt($ch, CURLOPT_POST, true);
curl_setopt($ch, CURLOPT_POSTFIELDS, $fields_string);
curl_setopt($ch, CURLOPT_RETURNTRANSFER, 1);
curl_setopt($ch, CURLOPT_SSL_VERIFYPEER, true);
curl_setopt($ch, CURLOPT_SSL_VERIFYHOST, 2);
$result = curl_exec($ch);

if ($result===false) {
    auth_error(curl_error($ch));
}
curl_close($ch);
$response=json_decode($result);
$auth_token=$response->access_token;
$jwtexplode=json_decode(base64_decode(str_replace('_', '/', str_replace('-','+',explode('.',$auth_token )[1]))));
$charactername=$jwtexplode->name;
$owner=$jwtexplode->owner;
$characterid=explode(":",$jwtexplode->sub)[2];

$_SESSION['characterid']=$characterid;
$_SESSION['charactername']=$charactername;

// Fresh random state for the Discord leg (was previously a hardcoded constant).
$discordstate=bin2hex(random_bytes(16));
$_SESSION['discordstate']=$discordstate;

$discordcallback="https://www.fuzzwork.co.uk/discord-auth/callback2.php";

header('Location: https://discordapp.com/api/oauth2/authorize?response_type=code&client_id='.$discord_clientid.'&scope=identify&state='.$discordstate.'&redirect_uri='.urlencode($discordcallback));
