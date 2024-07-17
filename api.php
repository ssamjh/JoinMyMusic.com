<?php
header('Content-Type: application/json');
require_once 'config.php';

// Authentication check
if (!isset($_GET['auth']) || $_GET['auth'] !== AUTH_KEY) {
    http_response_code(403);
    die(json_encode(['error' => 'Access denied']));
}

// Redis connection setup
$redis = getRedisInstance();

// Function to approve a request
function approveRequest($uri)
{
    $url = LIBRESPOT_API_URL . "/player/addToQueue";
    $data = ['uri' => $uri];
    $ch = curl_init();
    curl_setopt($ch, CURLOPT_URL, $url);
    curl_setopt($ch, CURLOPT_POST, 1);
    curl_setopt($ch, CURLOPT_POSTFIELDS, http_build_query($data));
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    $response = curl_exec($ch);
    curl_close($ch);
    return $response !== false;
}

$action = $_GET['action'] ?? '';

switch ($action) {
    case 'getAutoApproveStatus':
        $status = $redis->get('auto_approve') ?: '0';
        echo json_encode(['autoApprove' => $status === '1']);
        break;

    case 'getPendingRequestsCount':
        $count = $redis->lLen('requests');
        echo json_encode(['pendingRequests' => $count]);
        break;

    case 'getTopRequest':
        $request = $redis->lIndex('requests', 0);
        if ($request) {
            $requestData = json_decode($request, true);
            echo json_encode($requestData);
        } else {
            echo json_encode(['message' => 'No pending requests']);
        }
        break;

    case 'approveTopRequest':
        $request = $redis->lIndex('requests', 0);
        if ($request) {
            $requestData = json_decode($request, true);
            $result = approveRequest($requestData['uri']);
            if ($result) {
                $redis->lPop('requests');
                echo json_encode(['success' => true, 'message' => 'Top request approved and removed from queue']);
            } else {
                echo json_encode(['success' => false, 'message' => 'Failed to approve top request']);
            }
        } else {
            echo json_encode(['message' => 'No pending requests to approve']);
        }
        break;

    default:
        echo json_encode(['error' => 'Invalid action']);
}
?>