<?php
require_once 'config.php';
// Redis connection setup
$redis = getRedisInstance();

function fetchData($url, $method = 'GET')
{
    $ch = curl_init($url);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_CUSTOMREQUEST, $method);
    $response = curl_exec($ch);
    if (curl_errno($ch)) {
        throw new Exception(curl_error($ch));
    }
    curl_close($ch);
    return $response;
}

// Ensure that no content has been output before this point
if (ob_get_level())
    ob_end_clean();

// Set the content type to JSON
header('Content-Type: application/json');

try {
    $metadataUrl = LIBRESPOT_API_URL . "/metadata";
    // Generate a unique cache key based on the URL
    $cacheKey = 'spotify_metadata_' . md5($metadataUrl);

    // Try to get data from Redis cache
    $cachedData = $redis->get($cacheKey);

    if ($cachedData === false) {
        // If not in cache, fetch the data
        $rawData = fetchData($metadataUrl);
        $formattedData = json_decode($rawData, true);

        // Cache the formatted data for 10 seconds
        $redis->setex($cacheKey, 10, json_encode($formattedData));
    } else {
        // If in cache, use the cached data
        $formattedData = json_decode($cachedData, true);
    }

    echo json_encode($formattedData);
} catch (Exception $e) {
    // Instead of showing an error, return empty values
    $emptyData = [
        'current' => [
            'album' => '',
            'albumid' => '',
            'artist' => [['id' => '', 'name' => '']],
            'cover' => '',
            'song' => '',
            'songid' => ''
        ]
    ];
    echo json_encode($emptyData);
}
?>