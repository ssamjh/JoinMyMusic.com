<?php
session_start();
header('Content-Type: application/json');

// Check if the user is logged in
if (!isset($_SESSION['user_id'])) {
    echo json_encode(['error' => 'Unauthorized. Please log in.']);
    exit;
}

// Redis connection setup
$redis = new Redis();
$redis->connect('127.0.0.1', 6379);

$action = $_POST['action'] ?? '';
$clientIP = $_SERVER['REMOTE_ADDR'];
function makeRequest($url, $method = 'POST', $data = null)
{
    $ch = curl_init($url);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_CUSTOMREQUEST, $method);
    if ($data) {
        curl_setopt($ch, CURLOPT_POSTFIELDS, http_build_query($data));
    }
    $response = curl_exec($ch);
    $error = curl_error($ch);
    curl_close($ch);

    if ($error) {
        return json_encode(['error' => 'Curl error: ' . $error]);
    }
    return $response;
}

function checkRateLimit($key, $limit, $period)
{
    global $redis, $clientIP;
    $current = $redis->get($key) ?: 0;
    if ($current >= $limit) {
        return false;
    }
    $redis->incr($key);
    $redis->expire($key, $period);
    return true;
}

switch ($action) {
    case 'search':
        $searchKey = "search_limit:{$_SESSION['user_id']}";
        if (!checkRateLimit($searchKey, 10, 180)) {
            echo json_encode(['error' => 'Oops you are searching too much. Wait a bit and try again.']);
            exit;
        }

        $query = $_POST['query'] ?? '';
        if (empty($query)) {
            echo json_encode(['error' => 'No search text provided.']);
            exit;
        }
        $url = "http://172.16.2.27:24879/search/" . urlencode($query);
        echo makeRequest($url);
        break;

    case 'addToQueue':
        $addKey = "add_limit:{$_SESSION['user_id']}";
        if (!checkRateLimit($addKey, 5, 1800)) {
            echo json_encode(['error' => 'Slow down on the requests there bud. Try again soon.']);
            exit;
        }

        $uri = $_POST['uri'] ?? '';
        if (empty($uri)) {
            echo json_encode(['error' => 'No URI provided']);
            exit;
        }
        $url = "http://172.16.2.27:24879/player/addToQueue";
        makeRequest($url, 'POST', ['uri' => $uri]);

        echo json_encode(['success' => true, 'message' => 'Thanks, your request has been sent!']);
        break;

    default:
        echo json_encode(['error' => 'Invalid action']);
}
?>