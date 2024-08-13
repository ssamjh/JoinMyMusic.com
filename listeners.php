<?php
require_once 'config.php';

$redis = getRedisInstance();

// Check if the stats parameter is set
if (isset($_GET['stats'])) {
    // Perform active cleanup of expired listeners
    $now = time();
    $allListeners = $redis->sMembers('active_listeners');
    $activeCount = 0;

    foreach ($allListeners as $uuid) {
        $listenerKey = "listener:$uuid";
        $lastSeen = $redis->hGet($listenerKey, 'last_seen');
        
        if ($lastSeen !== false && $now - $lastSeen <= 60) {
            $activeCount++;
        } else {
            // Remove expired listener
            $redis->sRem('active_listeners', $uuid);
            $redis->del($listenerKey);
        }
    }

    // Return the active listener count
    header('Content-Type: application/json');
    echo json_encode(['listeners' => $activeCount]);
    exit;
}

// If not returning count, get client IP and user agent
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

// Sanitize name function
function sanitizeName($name)
{
    // Remove all HTML tags
    $name = strip_tags($name);

    // Remove all non-printable characters
    $name = preg_replace('/[\x00-\x1F\x7F-\xFF]/', '', $name);

    // Remove potential JavaScript events
    $name = preg_replace('/on\w+\s*=\s*(?:(?:"|\')[^"\']*(?:"|\')|[^\s>])+/i', '', $name);

    // Remove excessive whitespace
    $name = preg_replace('/\s+/', ' ', $name);

    // Trim whitespace from the beginning and end
    $name = trim($name);

    // Convert special characters to HTML entities
    $name = htmlspecialchars($name, ENT_QUOTES | ENT_HTML5, 'UTF-8');

    // Limit the length of the name
    $name = substr($name, 0, 50);

    return $name;
}

// Sanitize the name
$sanitizedName = $name ? sanitizeName($name) : null;

// Update listener information in Redis
$listenerKey = "listener:$uuid";
$listenerData = [
    'uuid' => $uuid,
    'ip' => $clientIP,
    'user_agent' => $userAgent,
    'name' => $sanitizedName,
    'last_seen' => time(),
];

// Store listener data
$redis->hMSet($listenerKey, $listenerData);

// Add to active listeners set
$redis->sAdd('active_listeners', $uuid);

// Set expiration for both the listener data and the active listeners set
$expirationTime = 60; // 1 minute
$redis->expire($listenerKey, $expirationTime);
$redis->expire('active_listeners', $expirationTime);

// No response needed for tracking updates
http_response_code(204); // No Content