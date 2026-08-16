<?php

session_start();

require 'vendor/autoload.php';
require __DIR__ . '/secret.php';

use GuzzleHttp\Client;

$client = new Client();

$code=$_GET['code'] ?? null;
$state=$_GET['state'] ?? null;

// CSRF check: state must match what callback.php stored before sending the user to Discord.
if ($state === null || !isset($_SESSION['discordstate']) || !hash_equals($_SESSION['discordstate'], $state)) {
    print "There was a problem verifying the auth request. Please start over at https://www.fuzzwork.co.uk/discord-auth/";
    exit();
}
unset($_SESSION['discordstate']);

$body=['grant_type'=>'authorization_code','code'=>$code,'client_id'=>$discord_clientid,'client_secret'=>$discord_secret,"redirect_uri"=>'https://www.fuzzwork.co.uk/discord-auth/callback2.php'];


$response = $client->request('POST', 'https://discordapp.com/api/v9/oauth2/token', ['form_params'=>$body]);

$json=json_decode($response->getBody());

$headers=['Authorization'=>'Bearer '.$json->access_token];

$response = $client->request('GET', 'https://discordapp.com/api/v9/users/@me', ['headers'=>$headers]);

$json=json_decode($response->getBody());

$username=$json->username;
$discriminator=$json->discriminator;
$discordid=$json->id;



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
    $insertstmt->execute(array(":discordid"=>$discordid,":eveid"=> $_SESSION['characterid']));

    print "The Auth system should now know who you are. please go ask it to authenticate you again with /authme. ";

}
else
{
    // Don't echo character/Discord identity details to the browser on failure — log them instead.
    print "There was a problem with the auth code determining who you are. Sorry about that. Let Steve know.";
    error_log(sprintf(
        "discord-auth callback2 failure: characterid=%s username=%s discriminator=%s discordid=%s",
        $_SESSION['characterid'] ?? 'null',
        $username,
        $discriminator,
        $discordid
    ));
}
