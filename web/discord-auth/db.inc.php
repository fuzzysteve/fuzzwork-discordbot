<?php
require __DIR__ . '/secret.php';
$dbh = new PDO('mysql:host=localhost;dbname=discordbot', 'discordbot', $db_password);
