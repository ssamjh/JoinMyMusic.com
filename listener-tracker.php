<?php
require_once 'config.php';

$redis = getRedisInstance();

// Get client IP and user agent
$clientIP = $_SERVER['REMOTE_ADDR'];
$userAgent = $_SERVER['HTTP_USER_AGENT'] ?? 'Unknown';

// Get name and UUID from POST data
$name = isset($_POST['name']) ? $_POST['name'] : null;
$uuid = isset($_POST['uuid']) ? $_POST['uuid'] : null;

// Validate UUID
if (!$uuid || !preg_match('/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/', $uuid)) {
    http_response_code(400); // Bad Request
    exit('Invalid UUID');
}

// Update listener information in Redis
$listenerKey = "listener:$uuid";
$listenerData = [
    'uuid' => $uuid,
    'ip' => $clientIP,
    'user_agent' => $userAgent,
    'name' => $name,
    'last_seen' => time(),
];

// Store listener data
$redis->hMSet($listenerKey, $listenerData);

// Add to active listeners set
$redis->sAdd('active_listeners', $uuid);

// Set expiration for both the listener data and the active listeners set
$redis->expire($listenerKey, 60); // Expire after 1 minute of inactivity
$redis->expire('active_listeners', 60);

// No response needed
http_response_code(204); // No Content