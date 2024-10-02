<?php
require_once 'config.php';

// Authentication check for both GET and POST requests
if ((!isset($_GET['auth']) && !isset($_POST['auth'])) || ($_GET['auth'] !== AUTH_KEY && $_POST['auth'] !== AUTH_KEY)) {
    http_response_code(403);
    die('Access denied');
}

// Redis connection setup
$redis = getRedisInstance();

// function to get all active listeners
function getActiveListeners($redis)
{
    $activeListeners = $redis->sMembers('active_listeners');
    $listeners = [];
    foreach ($activeListeners as $uuid) {
        $listenerKey = "listener:$uuid";
        $listenerData = $redis->hGetAll($listenerKey);
        if (!empty($listenerData)) {
            $listeners[] = $listenerData;
        }
    }
    return $listeners;
}

// Function to fetch metadata from Spotify API
function fetchMetadata($uri)
{
    $url = LIBRESPOT_API_URL . "/trackinfo?trackid=" . urlencode($uri);
    $ch = curl_init();
    curl_setopt($ch, CURLOPT_URL, $url);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, 1);
    $output = curl_exec($ch);
    curl_close($ch);
    return json_decode($output, true);
}

// Function to store metadata in Redis
function storeMetadata($uri, $metadata)
{
    global $redis;
    $redis->set("metadata:$uri", json_encode($metadata));
    $redis->expire("metadata:$uri", 3600); // Cache for 1 hour
}

// Function to get metadata (from cache or API)
function getMetadata($uri)
{
    global $redis;
    $cachedMetadata = $redis->get("metadata:$uri");
    if ($cachedMetadata) {
        return json_decode($cachedMetadata, true);
    }
    $metadata = fetchMetadata($uri);
    storeMetadata($uri, $metadata);
    return $metadata;
}

// Function to approve a request
function approveRequest($uri)
{
    $url = LIBRESPOT_API_URL . "/add?trackid=" . urlencode($uri);
    $ch = curl_init();
    curl_setopt($ch, CURLOPT_URL, $url);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    $response = curl_exec($ch);
    curl_close($ch);

    if ($response !== false) {
        global $redis;
        // Move the request from 'requests' to 'approved_requests'
        $requestJson = $redis->lPop('requests');
        if ($requestJson) {
            $redis->rPush('approved_requests', $requestJson);
        }
        return true;
    }
    return false;
}

// Handle form submissions
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $postData = json_decode(file_get_contents('php://input'), true);
    if (isset($postData['approve'])) {
        $requestJson = $postData['request'];
        $requestData = json_decode($requestJson, true);
        $uri = $requestData['uri'];
        $result = approveRequest($uri);
        if ($result === false) {
            header('Content-Type: application/json');
            echo json_encode(['success' => false, 'message' => 'Failed to add to queue']);
            exit;
        }
    } elseif (isset($postData['delete'])) {
        $requestJson = $postData['request'];
        $redis->lRem('requests', $requestJson, 0);
    } elseif (isset($postData['toggleAutoApprove'])) {
        $currentValue = $redis->get('auto_approve') ?: '0';
        $newValue = $currentValue === '1' ? '0' : '1';
        $redis->set('auto_approve', $newValue);
        header('Content-Type: application/json');
        echo json_encode(['success' => true, 'autoApprove' => $newValue]);
        exit;
    }
    // Send a JSON response
    header('Content-Type: application/json');
    echo json_encode(['success' => true]);
    exit;
}

// Fetch all pending requests
$pendingRequests = $redis->lRange('requests', 0, -1);

// Fetch approved requests (let's limit to the last 20 for performance)
$approvedRequests = array_reverse($redis->lRange('approved_requests', -20, -1));

// Get auto-approve setting
$autoApprove = $redis->get('auto_approve');
$autoApproveChecked = ($autoApprove === '1') ? 'checked' : '';

// Function to generate HTML for requests
function generateRequestsHtml($requests, $isApproved = false)
{
    $html = '';
    foreach ($requests as $request) {
        $requestData = json_decode($request, true);
        $uri = $requestData['uri'];
        $metadata = getMetadata($uri);

        if (!$metadata) {
            continue; // Skip this request if metadata couldn't be fetched
        }

        // Get the cover image URL
        $imageUrl = $metadata['cover'] ?? '';

        // Generate artist string
        $artists = isset($metadata['artist']) ? array_map(function ($artist) {
            return htmlspecialchars($artist['name']);
        }, $metadata['artist']) : ['Unknown'];
        $artistString = implode(', ', $artists);

        $html .= '
        <div class="request-item ' . ($isApproved ? 'approved' : '') . '">
            <div class="row align-items-center">
                <div class="col-3 col-sm-2">
                    ' . ($imageUrl ? '<img src="' . htmlspecialchars($imageUrl) . '" alt="Album Art" class="album-art">' : '<div class="album-art bg-secondary d-flex align-items-center justify-content-center text-white">No Image</div>') . '
                </div>
                <div class="col-9 col-sm-10 song-info">
                    <h4>' . htmlspecialchars($metadata['song'] ?? 'Unknown') . '</h4>
                    <p>' . $artistString . ' (' . htmlspecialchars($metadata['album'] ?? 'Unknown') . ')</p>
                    <p>From: ' . htmlspecialchars($requestData['name'] ?? 'Anonymous') . ' - ' . htmlspecialchars($requestData['ip'] ?? 'Unknown') . '</p>
                    <p><em>' . (isset($requestData['timestamp']) ? date('Y-m-d H:i:s', $requestData['timestamp']) : 'Unknown') . '</em></p>
                    ' . (!$isApproved ? '
                    <form method="post" class="mt-2">
                        <input type="hidden" name="request" value="' . htmlspecialchars($request) . '">
                        <div class="d-flex justify-content-start">
                            <button type="submit" name="approve" class="btn btn-success btn-sm me-2">Approve</button>
                            <button type="submit" name="delete" class="btn btn-danger btn-sm">Delete</button>
                        </div>
                    </form>
                    ' : '') . '
                </div>
            </div>
        </div>';
    }
    return $html;
}


function generateActiveListenersHtml($listeners)
{
    $html = '';
    foreach ($listeners as $listener) {
        $html .= '
        <div class="listener-item">
            <div class="row align-items-center">
                <div class="col-12 listener-info">
                    <h5>' . htmlspecialchars($listener['name'] ?? 'Anonymous') . '</h5>
                    <p><strong>IP:</strong> ' . htmlspecialchars($listener['ip']) . '</p>
                    <p><strong>User Agent:</strong> ' . htmlspecialchars($listener['user_agent']) . '</p>
                    <p><strong>Last Seen:</strong> ' . date('Y-m-d H:i:s', $listener['last_seen']) . '</p>
                </div>
            </div>
        </div>';
    }
    return $html ?: '<p>No active listeners found.</p>';
}

// Get active listeners
$activeListeners = getActiveListeners($redis);

// If it's an AJAX request, return only the requests HTML and active listeners
if (isset($_GET['ajax'])) {
    echo '<div class="row">';
    echo '<div class="col-md-6">';
    echo '<h3>Pending Requests</h3>';
    echo generateRequestsHtml($pendingRequests);
    echo '<h3>Approved Requests</h3>';
    echo generateRequestsHtml($approvedRequests, true);
    echo '</div>';
    echo '<div class="col-md-6">';
    echo '<h3>Active Listeners</h3>';
    echo generateActiveListenersHtml($activeListeners);
    echo '</div>';
    echo '</div>';
    exit;
}
?>

<!DOCTYPE html>
<html lang="en" data-bs-theme="auto">

<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Manage Requests - ssamjh's Music Sync</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet"
        crossorigin="anonymous">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.2/css/all.min.css"
        crossorigin="anonymous">
    <script src="js/bootstrap-auto-colour.js"></script>
    <style>
        .request-item {
            border: 1px solid #ddd;
            padding: 10px;
            margin-bottom: 10px;
            border-radius: 8px;
        }

        .request-item.approved {
            background-color: rgba(0, 255, 0, 0.1);
        }

        .album-art {
            width: 60px;
            height: 60px;
            object-fit: cover;
            border-radius: 4px;
        }

        .song-info {
            font-size: 0.85rem;
        }

        .song-info h4 {
            font-size: 1rem;
            margin-bottom: 0.2rem;
        }

        .song-info p {
            margin-bottom: 0.1rem;
        }

        .btn-sm {
            padding: 0.2rem 0.4rem;
            font-size: 0.75rem;
        }

        .listener-item {
            border: 1px solid #ddd;
            padding: 8px;
            margin-bottom: 8px;
            border-radius: 8px;
            background-color: rgba(0, 123, 255, 0.1);
        }

        .listener-info {
            font-size: 0.85rem;
        }

        .listener-info h5 {
            font-size: 1rem;
            margin-bottom: 0.2rem;
        }

        .listener-info p {
            margin-bottom: 0.1rem;
        }
    </style>
</head>

<body class="bg">
    <div class="container-fluid py-3">
        <h2 class="text-center">Manage Requests</h2>
        <h5 class="text-center mb-4">ssamjh's Music Sync</h5>

        <div class="form-check form-switch mb-3">
            <input class="form-check-input" type="checkbox" id="autoApproveToggle" <?php echo $autoApproveChecked; ?>>
            <label class="form-check-label" for="autoApproveToggle">Auto-approve requests</label>
        </div>

        <div id="requests-container">
            <div class="row">
                <div class="col-md-6">
                    <h3>Pending Requests</h3>
                    <?php echo generateRequestsHtml($pendingRequests); ?>
                    <h3>Approved Requests</h3>
                    <?php echo generateRequestsHtml($approvedRequests, true); ?>
                </div>
                <div class="col-md-6">
                    <h3>Active Listeners</h3>
                    <?php echo generateActiveListenersHtml($activeListeners); ?>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"
        crossorigin="anonymous"></script>
    <script>
        const authKey = '<?php echo AUTH_KEY; ?>';

        function reloadRequests() {
            fetch(`manage.php?ajax=1&auth=${authKey}`)
                .then(response => response.text())
                .then(html => {
                    document.getElementById('requests-container').innerHTML = html;
                });
        }

        setInterval(reloadRequests, 10000); // Reload every 10 seconds

        // Update form submissions to include auth key in URL
        document.addEventListener('click', function (event) {
            if (event.target.tagName === 'BUTTON' && event.target.closest('form')) {
                const form = event.target.closest('form');
                const formData = new FormData(form);
                const data = {
                    request: formData.get('request')
                };

                if (event.target.name === 'approve') {
                    data.approve = true;
                } else if (event.target.name === 'delete') {
                    data.delete = true;
                }

                event.preventDefault();

                fetch(`manage.php?auth=${authKey}`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify(data)
                }).then(response => response.json())
                    .then(result => {
                        if (result.success) {
                            reloadRequests();
                        } else {
                            console.error('Failed to process request');
                        }
                    });
            }
        });

        // Add event listener for auto-approve toggle
        document.getElementById('autoApproveToggle').addEventListener('change', function (event) {
            fetch(`manage.php?auth=${authKey}`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ toggleAutoApprove: true })
            }).then(response => response.json())
                .then(result => {
                    if (result.success) {
                        console.log('Auto-approve setting updated');
                        // Update the checkbox state based on the server response
                        event.target.checked = result.autoApprove === '1';
                    } else {
                        console.error('Failed to update auto-approve setting');
                        // Revert the checkbox state if the update failed
                        event.target.checked = !event.target.checked;
                    }
                });
        });
    </script>
</body>

</html>