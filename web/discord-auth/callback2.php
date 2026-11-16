<?php

require __DIR__ . '/session.inc.php';

require 'vendor/autoload.php';
require __DIR__ . '/secret.php';

use GuzzleHttp\Client;
use GuzzleHttp\Exception\GuzzleException;

const START_OVER = "Please start over at https://www.fuzzwork.co.uk/discord-auth/";

function fail_and_exit($public_message, $log_message)
{
    print $public_message;
    error_log("discord-auth callback2: ".$log_message);
    exit();
}

$code=$_GET['code'] ?? null;
$state=$_GET['state'] ?? null;

// CSRF check: state must match what callback.php stored before sending the user to Discord.
if ($state === null || !isset($_SESSION['discordstate']) || !hash_equals($_SESSION['discordstate'], $state)) {
    print "There was a problem verifying the auth request. ".START_OVER;
    exit();
}
unset($_SESSION['discordstate']);

if (!is_string($code) || $code === '') {
    // Discord sends ?error=access_denied (no code) when the user clicks Cancel.
    fail_and_exit("Discord didn't authorise the request. ".START_OVER, "no code, error=".($_GET['error'] ?? 'none'));
}

$client = new Client(['timeout' => 10]);
try {
    $body=['grant_type'=>'authorization_code','code'=>$code,'client_id'=>$discord_clientid,'client_secret'=>$discord_secret,"redirect_uri"=>'https://www.fuzzwork.co.uk/discord-auth/callback2.php'];
    $response = $client->request('POST', 'https://discord.com/api/v10/oauth2/token', ['form_params'=>$body]);
    $token=json_decode($response->getBody());

    $response = $client->request('GET', 'https://discord.com/api/v10/users/@me', ['headers'=>['Authorization'=>'Bearer '.($token->access_token ?? '')]]);
    $user=json_decode($response->getBody());
} catch (GuzzleException $e) {
    // Usually a reused/expired code (user hit back or refresh). Log the class and status
    // only; the exception message can include response bodies.
    $status = method_exists($e, 'getResponse') && $e->getResponse() ? $e->getResponse()->getStatusCode() : 'n/a';
    fail_and_exit("There was a problem talking to Discord. ".START_OVER, get_class($e)." status=".$status);
}

$username=$user->username ?? null;
$discordid=$user->id ?? null;

require("db.inc.php");

if (is_numeric($_SESSION['characterid'] ?? null) and is_numeric($discordid)) {

    $sql="select * from userlookup where discordid=:discordid";
    $checkstmt = $dbh->prepare($sql);
    $checkstmt->execute(array(":discordid"=>$discordid));
    if ($row= $checkstmt->fetchObject()) {
        print "We already know who you are.";
        exit();
    }

    $insertsql="insert into userlookup(discordid,eveid) values (:discordid,:eveid)";
    $insertstmt = $dbh->prepare($insertsql);
    try {
        $insertstmt->execute(array(":discordid"=>$discordid,":eveid"=> $_SESSION['characterid']));
    } catch (PDOException $e) {
        // 23000 = the unique key on userlookup.discordid: a second request for the same
        // Discord account (double-click, second tab) won the race between check and insert.
        if ($e->getCode() == '23000') {
            print "We already know who you are.";
            exit();
        }
        throw $e;
    }

    print "The Auth system should now know who you are. please go ask it to authenticate you again with /authme. ";

}
else
{
    // Don't echo character/Discord identity details to the browser on failure — log them instead.
    fail_and_exit(
        "There was a problem with the auth code determining who you are. Sorry about that. Let Steve know.",
        sprintf("failure: characterid=%s username=%s discordid=%s", $_SESSION['characterid'] ?? 'null', $username ?? 'null', $discordid ?? 'null')
    );
}
