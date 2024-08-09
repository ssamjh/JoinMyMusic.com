<?php
require_once 'config.php';

// Authentication check for both GET and POST requests
if ((!isset($_GET['auth']) && !isset($_POST['auth'])) || ($_GET['auth'] !== AUTH_KEY && $_POST['auth'] !== AUTH_KEY)) {
    http_response_code(403);
    die('Access denied');
}


$redis = getRedisInstance();

// Function to get all active listeners
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

// Get active listeners
$activeListeners = getActiveListeners($redis);

// HTML output
?>
<!DOCTYPE html>
<html lang="en">

<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Redis Debug Page</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            line-height: 1.6;
            padding: 20px;
        }

        h1 {
            color: #333;
        }

        table {
            border-collapse: collapse;
            width: 100%;
        }

        th,
        td {
            border: 1px solid #ddd;
            padding: 8px;
            text-align: left;
        }

        th {
            background-color: #f2f2f2;
        }

        tr:nth-child(even) {
            background-color: #f9f9f9;
        }
    </style>
</head>

<body>
    <h1>Redis Debug Page</h1>
    <h2>Active Listeners</h2>
    <?php if (empty($activeListeners)): ?>
        <p>No active listeners found.</p>
    <?php else: ?>
        <table>
            <tr>
                <th>UUID</th>
                <th>IP</th>
                <th>Name</th>
                <th>User Agent</th>
                <th>Last Seen</th>
            </tr>
            <?php foreach ($activeListeners as $listener): ?>
                <tr>
                    <td><?php echo htmlspecialchars($listener['uuid']); ?></td>
                    <td><?php echo htmlspecialchars($listener['ip']); ?></td>
                    <td><?php echo htmlspecialchars($listener['name'] ?? 'N/A'); ?></td>
                    <td><?php echo htmlspecialchars($listener['user_agent']); ?></td>
                    <td><?php echo date('Y-m-d H:i:s', $listener['last_seen']); ?></td>
                </tr>
            <?php endforeach; ?>
        </table>
    <?php endif; ?>
</body>

</html>