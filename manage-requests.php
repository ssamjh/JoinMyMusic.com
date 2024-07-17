<?php
require_once 'config.php';

// Authentication check for both GET and POST requests
if ((!isset($_GET['auth']) && !isset($_POST['auth'])) || ($_GET['auth'] !== AUTH_KEY && $_POST['auth'] !== AUTH_KEY)) {
    http_response_code(403);
    die('Access denied');
}

// Redis connection setup
$redis = getRedisInstance();

// Function to fetch metadata from Spotify API
function fetchMetadata($uri)
{
    $url = LIBRESPOT_API_URL . "/metadata/" . urlencode($uri);
    $ch = curl_init();
    curl_setopt($ch, CURLOPT_URL, $url);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, 1);
    curl_setopt($ch, CURLOPT_POST, 1);
    curl_setopt($ch, CURLOPT_POSTFIELDS, "");  // Empty POST body
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
    $url = LIBRESPOT_API_URL . "/player/addToQueue";
    $data = ['uri' => $uri];
    $ch = curl_init();
    curl_setopt($ch, CURLOPT_URL, $url);
    curl_setopt($ch, CURLOPT_POST, 1);
    curl_setopt($ch, CURLOPT_POSTFIELDS, http_build_query($data));
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    $response = curl_exec($ch);
    curl_close($ch);
    return $response;
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
        $redis->lRem('requests', $requestJson, 0);
    } elseif (isset($postData['delete'])) {
        $requestJson = $postData['request'];
        $redis->lRem('requests', $requestJson, 0);
    }
    // Send a JSON response
    header('Content-Type: application/json');
    echo json_encode(['success' => true]);
    exit;
}

// Fetch all pending requests
$requests = $redis->lRange('requests', 0, -1);

// Function to generate HTML for requests
function generateRequestsHtml($requests)
{
    $html = '';
    foreach ($requests as $request) {
        $requestData = json_decode($request, true);
        $uri = $requestData['uri'];
        $metadata = getMetadata($uri);

        // Try to get the largest available image
        $imageUrl = '';
        if (isset($metadata['album']['coverGroup']['image'])) {
            foreach ($metadata['album']['coverGroup']['image'] as $image) {
                if ($image['size'] === 'LARGE') {
                    $imageUrl = $image['fileId'];
                    break;
                }
            }
            if (empty($imageUrl) && !empty($metadata['album']['coverGroup']['image'])) {
                $imageUrl = $metadata['album']['coverGroup']['image'][0]['fileId'];
            }
        }

        // Convert the image URL to lowercase
        $imageUrl = strtolower($imageUrl);

        // Generate artist string
        $artists = isset($metadata['artist']) ? array_map(function ($artist) {
            return htmlspecialchars($artist['name']);
        }, $metadata['artist']) : ['Unknown'];
        $artistString = implode(', ', $artists);

        $html .= '
                <div class="request-item">
    <div class="row align-items-center">
        <div class="col-3 col-sm-2">
            ' . ($imageUrl ? '<img src="https://i.scdn.co/image/' . htmlspecialchars($imageUrl) . '" alt="Album Art" class="album-art">' : '<div class="album-art bg-secondary d-flex align-items-center justify-content-center text-white">No Image</div>') . '
        </div>
        <div class="col-9 col-sm-10 song-info">
            <h4>' . htmlspecialchars($metadata['name'] ?? 'Unknown') . '</h4>
            <p>' . $artistString . ' (' . htmlspecialchars($metadata['album']['name'] ?? 'Unknown') . ')</p>
            <p>From: ' . htmlspecialchars($requestData['name'] ?? 'Anonymous') . ' - ' . htmlspecialchars($requestData['ip'] ?? 'Unknown') . '</p>
            <p><em>' . (isset($requestData['timestamp']) ? date('Y-m-d H:i:s', $requestData['timestamp']) : 'Unknown') . '</em></p>
            <form method="post" class="mt-2">
                <input type="hidden" name="request" value="' . htmlspecialchars($request) . '">
                <div class="d-flex justify-content-start">
                    <button type="submit" name="approve" class="btn btn-success btn-sm me-2">Approve</button>
                    <button type="submit" name="delete" class="btn btn-danger btn-sm">Delete</button>
                </div>
            </form>
        </div>
    </div>
</div>';
    }
    return $html;
}

// If it's an AJAX request, return only the requests HTML
if (isset($_GET['ajax'])) {
    echo generateRequestsHtml($requests);
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
        .container {
            max-width: 800px;
        }

        .request-item {
            border: 1px solid #ddd;
            padding: 15px;
            margin-bottom: 15px;
            border-radius: 8px;
        }

        .album-art {
            width: 80px;
            height: 80px;
            object-fit: cover;
            border-radius: 4px;
        }

        .song-info {
            font-size: 0.9rem;
        }

        .song-info h4 {
            font-size: 1.2rem;
            margin-bottom: 0.3rem;
        }

        .song-info p {
            margin-bottom: 0.2rem;
        }

        .btn-group {
            display: flex;
            justify-content: space-between;
            gap: 5px;
        }

        .btn-group .btn {
            flex: 1;
        }

        .btn-sm {
            padding: 0.25rem 0.5rem;
            font-size: 0.875rem;
        }
    </style>
</head>

<body class="bg">
    <div class="container py-3">
        <h2 class="text-center">Manage Requests</h2>
        <h5 class="text-center mb-4">ssamjh's Music Sync</h5>

        <div id="requests-container">
            <?php echo generateRequestsHtml($requests); ?>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"
        crossorigin="anonymous"></script>
    <script>
        const authKey = '<?php echo AUTH_KEY; ?>';

        function reloadRequests() {
            fetch(`manage-requests.php?ajax=1&auth=${authKey}`)
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

                fetch(`manage-requests.php?auth=${authKey}`, {
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
    </script>
</body>

</html>