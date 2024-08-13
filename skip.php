<?php
require_once 'config.php';

// Set up Redis connection
$redis = getRedisInstance();

// Check if it's a stats request
if (isset($_SERVER['QUERY_STRING']) && $_SERVER['QUERY_STRING'] === 'stats') {
    handleStatsRequest($redis);
    exit;
}

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

// Function to get the count of active listeners
function getActiveListenersCount($redis)
{
    $now = time();
    $allListeners = $redis->sMembers('active_listeners');
    $activeCount = 0;
    foreach ($allListeners as $uuid) {
        $listenerKey = "listener:$uuid";
        $lastSeen = $redis->hGet($listenerKey, 'last_seen');

        if ($lastSeen !== false && $now - $lastSeen <= 60) {
            $activeCount++;
        }
    }
    return $activeCount;
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

function fetchCurrentSongId($redis)
{
    $playerUrl = LIBRESPOT_API_URL . "/player/current";
    $cacheKey = 'spotify_data_' . md5($playerUrl);

    $cachedData = $redis->get($cacheKey);

    if ($cachedData !== false) {
        $data = json_decode($cachedData, true);
        if (isset($data['current']['songid']) && !empty($data['current']['songid'])) {
            return $data['current']['songid'];
        }
    }

    // If we couldn't get the song ID from Redis, fall back to the metadata.php method
    try {
        $metadataUrl = "https://joinmymusic.com/metadata.php";
        $rawData = fetchData($metadataUrl);
        $metadata = json_decode($rawData, true);

        if (!isset($metadata['current']['songid']) || empty($metadata['current']['songid'])) {
            throw new Exception("No current song ID in metadata");
        }

        return $metadata['current']['songid'];
    } catch (Exception $e) {
        throw new Exception('Failed to fetch or process metadata: ' . $e->getMessage());
    }
}

// Fetch current song ID
try {
    $currentSongId = fetchCurrentSongId($redis);
} catch (Exception $e) {
    http_response_code(500);
    exit(json_encode(['error' => $e->getMessage()]));
}

// Check if the voted song is currently playing
if ($songId !== $currentSongId) {
    http_response_code(400);
    exit(json_encode(['error' => 'Voted song does not match currently playing song']));
}

// Get total number of active listeners
$totalListeners = getActiveListenersCount($redis);

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
            'message' => 'Song skipped successfully'
        ]);
    } catch (Exception $e) {
        http_response_code(500);
        exit(json_encode(['error' => 'Failed to skip the song']));
    }
} else {
    echo json_encode([
        'success' => true,
        'message' => 'Vote recorded'
    ]);
}

function handleStatsRequest($redis)
{
    try {
        $currentSongId = fetchCurrentSongId($redis);
    } catch (Exception $e) {
        http_response_code(500);
        exit(json_encode(['error' => $e->getMessage()]));
    }

    // Get vote count for the current song
    $voteKey = "vote_skip:{$currentSongId}";
    $voteCount = $redis->sCard($voteKey);

    // Get total number of active listeners
    $totalListeners = getActiveListenersCount($redis);

    // Calculate total votes needed (not remaining votes)
    $votesNeeded = max(2, ceil($totalListeners / 2));

    // Prepare and send the response
    $response = [
        'song' => $currentSongId,
        'count' => $voteCount,
        'needed' => $votesNeeded
    ];

    header('Content-Type: application/json');
    echo json_encode($response);
}