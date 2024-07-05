<?php
require_once 'config.php';
session_start();

// Initialize database connection
$conn = new mysqli(DB_HOST, DB_USER, DB_PASS, DB_NAME);
if ($conn->connect_error) {
    die("Connection failed: " . $conn->connect_error);
}

// Function to generate a secure random token
function generateToken($length = 32)
{
    return bin2hex(random_bytes($length));
}

// Function to set a remember me cookie
function setRememberMeCookie($user_id)
{
    $token = generateToken();
    $expires = time() + (365 * 24 * 60 * 60); // 365 days

    // Store the token in the database
    global $conn;
    $stmt = $conn->prepare("INSERT INTO remember_tokens (user_id, token, expires) VALUES (?, ?, FROM_UNIXTIME(?))");
    $stmt->bind_param("isi", $user_id, $token, $expires);
    $stmt->execute();

    // Set the cookie
    setcookie('remember_me', $token, $expires, '/', '', true, true);
}

// Function to validate remember me cookie
function validateRememberMeCookie()
{
    if (isset($_COOKIE['remember_me'])) {
        $token = $_COOKIE['remember_me'];
        global $conn;

        $stmt = $conn->prepare("SELECT user_id FROM remember_tokens WHERE token = ? AND expires > NOW()");
        $stmt->bind_param("s", $token);
        $stmt->execute();
        $result = $stmt->get_result();

        if ($user = $result->fetch_assoc()) {
            // Token is valid, log the user in
            $_SESSION['user_id'] = $user['user_id'];
            // Refresh the token
            setRememberMeCookie($user['user_id']);
            return true;
        }
    }
    return false;
}

// Function to send password reset email using Brevo API
function sendPasswordResetEmail($username, $email, $resetToken)
{
    $url = 'https://api.brevo.com/v3/smtp/email';
    $data = array(
        'sender' => array('name' => FROM_NAME, 'email' => FROM_EMAIL),
        'to' => array(array('email' => $email)),
        'subject' => 'Password Reset Request',
        'htmlContent' => '<html><head></head><body><p>Hello ' . $username . ',</p><p>Click the link below to reset your password:</p><p><a href="' . SITE_URL . '/account.php?action=reset_password&token=' . $resetToken . '">Reset Password</a></p></body></html>'
    );

    $ch = curl_init($url);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_POST, true);
    curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($data));
    curl_setopt(
        $ch,
        CURLOPT_HTTPHEADER,
        array(
            'accept: application/json',
            'api-key: ' . BREVO_API_KEY,
            'content-type: application/json'
        )
    );

    $response = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);

    return $httpCode == 201;
}

// Function to validate input
function validateInput($input, $type)
{
    switch ($type) {
        case 'username':
            return preg_match('/^[a-zA-Z0-9_]{3,20}$/', $input);
        case 'email':
            return filter_var($input, FILTER_VALIDATE_EMAIL);
        case 'password':
            return strlen($input) >= 8;
        default:
            return false;
    }
}

// Handle POST requests
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $action = $_POST['action'] ?? '';

    switch ($action) {
        case 'register':
            $username = $_POST['username'] ?? '';
            $email = $_POST['email'] ?? '';
            $password = $_POST['password'] ?? '';

            if (!validateInput($username, 'username') || !validateInput($email, 'email') || !validateInput($password, 'password')) {
                echo json_encode(['success' => false, 'message' => 'Invalid input']);
                exit;
            }

            $username = $conn->real_escape_string($username);
            $email = $conn->real_escape_string($email);
            $password = password_hash($password, PASSWORD_DEFAULT);

            $stmt = $conn->prepare("INSERT INTO users (username, email, password) VALUES (?, ?, ?)");
            $stmt->bind_param("sss", $username, $email, $password);

            if ($stmt->execute()) {
                echo json_encode(['success' => true, 'message' => 'Account created successfully']);
            } else {
                echo json_encode(['success' => false, 'message' => 'Error creating account']);
            }
            break;

        case 'login':
            $username = $_POST['username'] ?? '';
            $password = $_POST['password'] ?? '';
            $remember_me = isset($_POST['remember_me']) ? true : false;

            if (!validateInput($username, 'username')) {
                echo json_encode(['success' => false, 'message' => 'Invalid username']);
                exit;
            }

            $username = $conn->real_escape_string($username);

            $stmt = $conn->prepare("SELECT id, username, password FROM users WHERE username = ?");
            $stmt->bind_param("s", $username);
            $stmt->execute();
            $result = $stmt->get_result();

            if ($user = $result->fetch_assoc()) {
                if (password_verify($password, $user['password'])) {
                    $_SESSION['user_id'] = $user['id'];
                    $_SESSION['username'] = $user['username'];

                    if ($remember_me) {
                        setRememberMeCookie($user['id']);
                    }

                    echo json_encode(['success' => true, 'message' => 'Login successful', 'username' => $user['username']]);
                } else {
                    echo json_encode(['success' => false, 'message' => 'Invalid credentials']);
                }
            } else {
                echo json_encode(['success' => false, 'message' => 'User not found']);
            }
            break;

        case 'logout':
            session_destroy();
            setcookie('remember_me', '', time() - 3600, '/', '', true, true); // Expire the remember me cookie
            echo json_encode(['success' => true, 'message' => 'Logged out successfully']);
            break;

        case 'reset_password_request':
            $username = $_POST['username'] ?? '';

            if (!validateInput($username, 'username')) {
                echo json_encode(['success' => false, 'message' => 'Invalid username']);
                exit;
            }

            $username = $conn->real_escape_string($username);

            // Check if the username exists
            $stmt = $conn->prepare("SELECT email FROM users WHERE username = ?");
            $stmt->bind_param("s", $username);
            $stmt->execute();
            $result = $stmt->get_result();

            if ($user = $result->fetch_assoc()) {
                $email = $user['email'];
                $resetToken = generateToken();

                $stmt = $conn->prepare("UPDATE users SET reset_token = ?, reset_token_expiry = DATE_ADD(NOW(), INTERVAL 1 HOUR) WHERE username = ?");
                $stmt->bind_param("ss", $resetToken, $username);

                if ($stmt->execute()) {
                    if (sendPasswordResetEmail($username, $email, $resetToken)) {
                        echo json_encode(['success' => true, 'message' => 'Password reset email sent']);
                    } else {
                        echo json_encode(['success' => false, 'message' => 'Failed to send reset email']);
                    }
                } else {
                    echo json_encode(['success' => false, 'message' => 'Error processing password reset request']);
                }
            } else {
                echo json_encode(['success' => false, 'message' => 'Username not found']);
            }
            break;

        case 'reset_password':
            $token = $_POST['token'] ?? '';
            $newPassword = $_POST['new_password'] ?? '';

            if (!validateInput($newPassword, 'password')) {
                echo json_encode(['success' => false, 'message' => 'Invalid password']);
                exit;
            }

            $token = $conn->real_escape_string($token);
            $newPassword = password_hash($newPassword, PASSWORD_DEFAULT);

            $stmt = $conn->prepare("UPDATE users SET password = ?, reset_token = NULL, reset_token_expiry = NULL WHERE reset_token = ? AND reset_token_expiry > NOW()");
            $stmt->bind_param("ss", $newPassword, $token);

            if ($stmt->execute()) {
                echo json_encode(['success' => true, 'message' => 'Password reset successful']);
            } else {
                echo json_encode(['success' => false, 'message' => 'Error resetting password']);
            }
            break;

        default:
            echo json_encode(['success' => false, 'message' => 'Invalid action']);
    }

    $conn->close();
    exit;
}

// Handle GET requests
if ($_SERVER['REQUEST_METHOD'] === 'GET') {
    $action = $_GET['action'] ?? '';

    switch ($action) {
        case 'check_login':
            if (isset($_SESSION['user_id'])) {
                echo json_encode(['loggedIn' => true, 'username' => $_SESSION['username']]);
            } elseif (validateRememberMeCookie()) {
                // If the session is not set, but the remember me cookie is valid
                $stmt = $conn->prepare("SELECT username FROM users WHERE id = ?");
                $stmt->bind_param("i", $_SESSION['user_id']);
                $stmt->execute();
                $result = $stmt->get_result();
                $user = $result->fetch_assoc();
                echo json_encode(['loggedIn' => true, 'username' => $user['username']]);
            } else {
                echo json_encode(['loggedIn' => false]);
            }
            break;

        case 'reset_password':
            $token = $_GET['token'] ?? '';
            if (empty($token)) {
                echo json_encode(['success' => false, 'message' => 'Invalid or missing token']);
            } else {
                // Return JSON with the token, so the frontend can handle displaying the form
                echo json_encode(['success' => true, 'token' => $token]);
            }
            break;

        default:
            echo json_encode(['success' => false, 'message' => 'Invalid action']);
    }

    $conn->close();
    exit;
}
?>