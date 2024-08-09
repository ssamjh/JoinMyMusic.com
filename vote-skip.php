<?php
require_once 'config.php';

// Set up Redis connection
$redis = getRedisInstance();

// Get client IP and user agent
$clientIP = $_SERVER['REMOTE_ADDR'];
$userAgent = $_SERVER['HTTP_USER_AGENT'] ?? 'Unknown';

// Get UUID and song ID from POST data
$uuid = $_POST['uuid'] ?? null;
$songId = $_POST['songid'] ?? null;

// Check Redis connection
if (!$redis) {
    http_response_code(500);
    exit(json_encode(['error' => 'Internal server error: Redis connection failed']));
}

// Check if the UUID is in the active listeners set
if (!$uuid) {
    http_response_code(400);
    exit(json_encode(['error' => 'UUID not provided']));
}
if (!$redis->sIsMember('active_listeners', $uuid)) {
    http_response_code(400);
    exit(json_encode(['error' => 'Invalid or inactive listener']));
}

// Validate song ID
if (!$songId) {
    http_response_code(400);
    exit(json_encode(['error' => 'Invalid song ID']));
}

function fetchData($url, $method = 'GET')
{
    $ch = curl_init($url);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    if ($method === 'POST') {
        curl_setopt($ch, CURLOPT_POST, true);
    }
    $response = curl_exec($ch);
    if (curl_errno($ch)) {
        throw new Exception(curl_error($ch));
    }
    curl_close($ch);
    return $response;
}

// Fetch current track information from metadata.php
try {
    $metadataUrl = "https://joinmymusic.com/metadata.php";
    $rawData = fetchData($metadataUrl);
    $metadata = json_decode($rawData, true);

    if (!isset($metadata['current']['songid']) || empty($metadata['current']['songid'])) {
        throw new Exception("No current song ID in metadata");
    }

    $currentSongId = $metadata['current']['songid'];
} catch (Exception $e) {
    http_response_code(500);
    exit(json_encode(['error' => 'Failed to fetch or process metadata: ' . $e->getMessage()]));
}

// Check if the voted song is currently playing
if ($songId !== $currentSongId) {
    http_response_code(400);
    exit(json_encode(['error' => 'Voted song does not match currently playing song']));
}

// Get total number of active listeners
$totalListeners = $redis->sCard('active_listeners');

if ($totalListeners === 0) {
    http_response_code(400);
    exit(json_encode(['error' => 'No active listeners']));
}

// Record the vote
$voteKey = "vote_skip:{$songId}";
$redis->sAdd($voteKey, $uuid);
$redis->expire($voteKey, 300); // Expire votes after 5 minutes

// Count votes
$voteCount = $redis->sCard($voteKey);

// Calculate if skip conditions are met
$skipConditionsMet = ($voteCount >= 2) && ($voteCount >= ceil($totalListeners / 2));

if ($skipConditionsMet) {
    // Perform the skip action
    $skipUrl = LIBRESPOT_API_URL . "/player/next";
    try {
        $response = fetchData($skipUrl, 'POST');
        // Clear the votes for this song
        $redis->del($voteKey);

        echo json_encode([
            'success' => true,
            'message' => 'Song skipped successfully',
            'votes' => $voteCount,
            'totalListeners' => $totalListeners
        ]);
    } catch (Exception $e) {
        http_response_code(500);
        exit(json_encode(['error' => 'Failed to skip the song']));
    }
} else {
    $votesNeeded = max(2, ceil($totalListeners / 2)) - $voteCount;
    echo json_encode([
        'success' => true,
        'message' => 'Vote recorded',
        'votes' => $voteCount,
        'totalListeners' => $totalListeners,
        'votesNeeded' => $votesNeeded
    ]);
}