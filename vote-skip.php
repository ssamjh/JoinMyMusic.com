<?php
require_once 'config.php';

function logDebug($message)
{
    $logFile = __DIR__ . '/debug.log';
    $timestamp = date('Y-m-d H:i:s');
    file_put_contents($logFile, "[$timestamp] $message\n", FILE_APPEND);
}

// Set up Redis connection
$redis = getRedisInstance();

// Get client IP and user agent
$clientIP = $_SERVER['REMOTE_ADDR'];
$userAgent = $_SERVER['HTTP_USER_AGENT'] ?? 'Unknown';

// Get UUID and song ID from POST data
$uuid = $_POST['uuid'] ?? null;
$songId = $_POST['songid'] ?? null;

logDebug("Request received. UUID: $uuid, Song ID: $songId, IP: $clientIP, User-Agent: $userAgent");

// Check Redis connection
if (!$redis) {
    logDebug("Failed to connect to Redis");
    http_response_code(500);
    exit(json_encode(['error' => 'Internal server error: Redis connection failed']));
}

// Check if the UUID is in the active listeners set
if (!$uuid) {
    logDebug("Error: UUID not provided");
    http_response_code(400);
    exit(json_encode(['error' => 'UUID not provided']));
}
if (!$redis->sIsMember('active_listeners', $uuid)) {
    logDebug("Error: Invalid or inactive listener. UUID: $uuid");
    http_response_code(400);
    exit(json_encode(['error' => 'Invalid or inactive listener']));
}

// Validate song ID
if (!$songId) {
    logDebug("Error: Song ID not provided");
    http_response_code(400);
    exit(json_encode(['error' => 'Invalid song ID']));
}

function fetchData($url)
{
    $ch = curl_init($url);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
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
    logDebug("Raw data from metadata API: '" . $rawData . "'");

    if (empty($rawData)) {
        logDebug("Received empty response from metadata API");
        throw new Exception("Received empty response from metadata API");
    }

    $metadata = json_decode($rawData, true);

    if ($metadata === null && json_last_error() !== JSON_ERROR_NONE) {
        logDebug("Error decoding JSON: " . json_last_error_msg());
        logDebug("Raw data causing JSON error: " . $rawData);
        throw new Exception("Failed to decode JSON response from metadata API");
    }

    logDebug("Decoded metadata: " . json_encode($metadata));

    if (!isset($metadata['current']['songid']) || empty($metadata['current']['songid'])) {
        logDebug("No current song ID in metadata");
        throw new Exception("No current song ID in metadata");
    }

    $currentSongId = $metadata['current']['songid'];
    logDebug("Current song ID from metadata: " . $currentSongId);
} catch (Exception $e) {
    logDebug("Error fetching or processing metadata: " . $e->getMessage());
    http_response_code(500);
    exit(json_encode(['error' => 'Failed to fetch or process metadata: ' . $e->getMessage()]));
}

// Check if the voted song is currently playing
if ($songId !== $currentSongId) {
    logDebug("Error: Voted song does not match currently playing song. Voted: $songId, Current: $currentSongId");
    http_response_code(400);
    exit(json_encode(['error' => 'Voted song does not match currently playing song']));
}
// Get total number of active listeners
$totalListeners = $redis->sCard('active_listeners');
logDebug("Total active listeners: $totalListeners");

if ($totalListeners === 0) {
    logDebug("Error: No active listeners");
    http_response_code(400);
    exit(json_encode(['error' => 'No active listeners']));
}

// Record the vote
$voteKey = "vote_skip:{$songId}";
$redis->sAdd($voteKey, $uuid);
$redis->expire($voteKey, 300); // Expire votes after 5 minutes

// Count votes
$voteCount = $redis->sCard($voteKey);
logDebug("Votes for song $songId: $voteCount");

// Calculate if skip conditions are met
$skipConditionsMet = ($voteCount >= 2) && ($voteCount >= ceil($totalListeners / 2));
logDebug("Skip conditions met: " . ($skipConditionsMet ? "Yes" : "No"));

if ($skipConditionsMet) {
    // Perform the skip action
    $skipUrl = LIBRESPOT_API_URL . "/player/next";
    try {
        $response = fetchData($skipUrl, 'POST');
        // Clear the votes for this song
        $redis->del($voteKey);

        logDebug("Song skipped successfully. Song ID: $songId");
        echo json_encode([
            'success' => true,
            'message' => 'Song skipped successfully',
            'votes' => $voteCount,
            'totalListeners' => $totalListeners
        ]);
    } catch (Exception $e) {
        logDebug("Error skipping song: " . $e->getMessage());
        http_response_code(500);
        exit(json_encode(['error' => 'Failed to skip the song']));
    }
} else {
    $votesNeeded = max(2, ceil($totalListeners / 2)) - $voteCount;
    logDebug("Vote recorded. Votes: $voteCount, Total Listeners: $totalListeners, Votes Needed: $votesNeeded");
    echo json_encode([
        'success' => true,
        'message' => 'Vote recorded',
        'votes' => $voteCount,
        'totalListeners' => $totalListeners,
        'votesNeeded' => $votesNeeded
    ]);
}