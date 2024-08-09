<?php
header('Content-Type: application/json');

require_once 'config.php';

// Redis connection setup
$redis = getRedisInstance();

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

function approveRequest($uri)
{
    $url = LIBRESPOT_API_URL . "/player/addToQueue";
    $data = ['uri' => $uri];
    return makeRequest($url, 'POST', $data);
}

switch ($action) {
    case 'search':
        $searchKey = "search_limit:{$clientIP}";
        if (!checkRateLimit($searchKey, 10, 180)) {
            echo json_encode(['error' => 'Oops you are searching too much. Wait a bit and try again.']);
            exit;
        }

        $query = $_POST['query'] ?? '';
        if (empty($query)) {
            echo json_encode(['error' => 'No search text provided.']);
            exit;
        }
        $url = LIBRESPOT_API_URL . "/search/" . urlencode($query);
        echo makeRequest($url);
        break;

    case 'addToQueue':
        $addKey = "add_limit:{$clientIP}";
        if (!checkRateLimit($addKey, 10, 1800)) {
            echo json_encode(['error' => 'Slow down on the requests there bud. Try again soon.']);
            exit;
        }

        $uri = $_POST['uri'] ?? '';
        $name = $_POST['name'] ?? '';
        if (empty($uri)) {
            echo json_encode(['error' => 'No URI provided']);
            exit;
        }
        if (empty($name)) {
            echo json_encode(['error' => 'Please provide your name']);
            exit;
        }

        // Sanitize the name
        $sanitizedName = filter_var($name, FILTER_SANITIZE_STRING);

        // Create request data
        $requestData = json_encode([
            'uri' => $uri,
            'ip' => $clientIP,
            'timestamp' => time(),
            'name' => $sanitizedName
        ]);

        // Check if auto-approve is enabled
        $autoApprove = $redis->get('auto_approve') ?: '0';

        if ($autoApprove === '1') {
            // Automatically approve the request
            $result = approveRequest($uri);
            if ($result === false) {
                echo json_encode(['error' => 'Failed to add to queue']);
                exit;
            }
            // Store the approved request in the approved_requests list
            $redis->rPush('approved_requests', $requestData);
            echo json_encode(['success' => true, 'message' => 'Your request has been automatically approved and added to the queue!']);
        } else {
            // Store the request in Redis for manual approval
            $redis->rPush('requests', $requestData);
            echo json_encode(['success' => true, 'message' => 'Thanks, your request has been added to the queue for approval!']);
        }
        break;

    default:
        echo json_encode(['error' => 'Invalid action']);
}
?>