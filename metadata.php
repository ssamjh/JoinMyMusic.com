<?php
// Redis connection setup
$redis = new Redis();
$redis->connect('127.0.0.1', 6379); // Adjust host and port as needed

function fetchData($url, $method = 'POST')
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

function getSpotifyData($baseUrl, $trackId)
{
    $webApiUrl = $baseUrl . "/web-api/v1/tracks/" . $trackId;
    return json_decode(fetchData($webApiUrl, 'GET'), true);
}

function transformData($data, $baseUrl)
{
    $original = json_decode($data, true);

    // Check if nothing is playing
    if (!isset($original['track']) || empty($original['track'])) {
        return [
            'current' => [
                'album' => '',
                'albumid' => '',
                'artist' => [['id' => '', 'name' => '']],
                'cover' => '',
                'song' => '',
                'songid' => ''
            ]
        ];
    }

    $track = $original['track'];
    $trackUri = $original['current'];
    $trackId = explode(':', $trackUri)[2];
    // Fetch Spotify data
    $spotifyData = getSpotifyData($baseUrl, $trackId);
    $spotifyAlbumId = $spotifyData['album']['id'] ?? '';
    $spotifyArtistId = $spotifyData['artists'][0]['id'] ?? '';

    $transformed = [
        'current' => [
            'album' => $track['album']['name'] ?? '',
            'albumid' => $spotifyAlbumId,
            'artist' => array_map(function ($artist) use ($spotifyArtistId) {
                return [
                    'id' => $spotifyArtistId,
                    'name' => $artist['name'] ?? ''
                ];
            }, $track['artist'] ?? []),
            'cover' => isset($track['album']['coverGroup']['image'][0]['fileId'])
                ? "https://i.scdn.co/image/" . strtolower($track['album']['coverGroup']['image'][0]['fileId'])
                : '',
            'song' => $track['name'] ?? '',
            'songid' => $trackId
        ]
    ];
    return $transformed;
}

// Ensure that no content has been output before this point
if (ob_get_level())
    ob_end_clean();

// Set the content type to JSON
header('Content-Type: application/json');

try {
    $baseUrl = "http://172.16.2.27:24879";
    $playerUrl = $baseUrl . "/player/current";

    // Generate a unique cache key based on the URL
    $cacheKey = 'spotify_data_' . md5($playerUrl);

    // Try to get data from Redis cache
    $cachedData = $redis->get($cacheKey);

    if ($cachedData === false) {
        // If not in cache, fetch and transform the data
        $rawData = fetchData($playerUrl);
        $formattedData = transformData($rawData, $baseUrl);

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