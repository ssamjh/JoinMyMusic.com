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

        $stmt = $conn->prepare("SELECT r.user_id, u.username FROM remember_tokens r JOIN users u ON r.user_id = u.id WHERE r.token = ? AND r.expires > NOW()");
        $stmt->bind_param("s", $token);
        $stmt->execute();
        $result = $stmt->get_result();

        if ($user = $result->fetch_assoc()) {
            // Token is valid and user exists, log the user in
            $_SESSION['user_id'] = $user['user_id'];
            $_SESSION['username'] = $user['username'];
            // Refresh the token
            setRememberMeCookie($user['user_id']);
            return true;
        } else {
            // Invalid token or user doesn't exist, clear the cookie
            setcookie('remember_me', '', time() - 3600, '/', '', true, true);
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

function logout()
{
    // Clear session
    session_unset();
    session_destroy();

    // Clear remember me cookie
    setcookie('remember_me', '', time() - 3600, '/', '', true, true);

    // Remove remember token from database
    if (isset($_COOKIE['remember_me'])) {
        $token = $_COOKIE['remember_me'];
        global $conn;
        $stmt = $conn->prepare("DELETE FROM remember_tokens WHERE token = ?");
        $stmt->bind_param("s", $token);
        $stmt->execute();
    }
}

function validateTurnstile($response)
{
    $url = 'https://challenges.cloudflare.com/turnstile/v0/siteverify';
    $data = [
        'secret' => TURNSTILE_SECRET_KEY,
        'response' => $response,
        'remoteip' => $_SERVER['REMOTE_ADDR']
    ];

    $options = [
        'http' => [
            'header' => "Content-type: application/x-www-form-urlencoded\r\n",
            'method' => 'POST',
            'content' => http_build_query($data)
        ]
    ];

    $context = stream_context_create($options);
    $result = file_get_contents($url, false, $context);
    return json_decode($result, true);
}

// Handle POST requests
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $action = $_POST['action'] ?? '';

    switch ($action) {
        case 'register':
            $username = $_POST['username'] ?? '';
            $email = $_POST['email'] ?? '';
            $password = $_POST['password'] ?? '';
            $turnstileResponse = $_POST['cf-turnstile-response'] ?? '';

            $turnstileResult = validateTurnstile($turnstileResponse);
            if (!$turnstileResult['success']) {
                echo json_encode(['success' => false, 'message' => 'Turnstile validation failed']);
                exit;
            }

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
            $turnstileResponse = $_POST['cf-turnstile-response'] ?? '';

            $turnstileResult = validateTurnstile($turnstileResponse);
            if (!$turnstileResult['success']) {
                echo json_encode(['success' => false, 'message' => 'Turnstile validation failed']);
                exit;
            }

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
            $turnstileResponse = $_POST['cf-turnstile-response'] ?? '';

            $turnstileResult = validateTurnstile($turnstileResponse);
            if (!$turnstileResult['success']) {
                echo json_encode(['success' => false, 'message' => 'Turnstile validation failed']);
                exit;
            }

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

            // First, check if the token is valid and not expired
            $stmt = $conn->prepare("SELECT id FROM users WHERE reset_token = ? AND reset_token_expiry > NOW()");
            $stmt->bind_param("s", $token);
            $stmt->execute();
            $result = $stmt->get_result();

            if ($result->num_rows === 0) {
                echo json_encode(['success' => false, 'message' => 'Invalid or expired reset token']);
                exit;
            }

            // If valid, update the password and invalidate the token
            $stmt = $conn->prepare("UPDATE users SET password = ?, reset_token = NULL, reset_token_expiry = NULL WHERE reset_token = ?");
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
                // Verify user still exists in the database
                $stmt = $conn->prepare("SELECT username FROM users WHERE id = ?");
                $stmt->bind_param("i", $_SESSION['user_id']);
                $stmt->execute();
                $result = $stmt->get_result();
                if ($user = $result->fetch_assoc()) {
                    echo json_encode(['loggedIn' => true, 'username' => $user['username']]);
                } else {
                    // User no longer exists, destroy session
                    session_destroy();
                    echo json_encode(['loggedIn' => false]);
                }
            } elseif (validateRememberMeCookie()) {
                // Similar verification for remember me cookie
                $stmt = $conn->prepare("SELECT username FROM users WHERE id = ?");
                $stmt->bind_param("i", $_SESSION['user_id']);
                $stmt->execute();
                $result = $stmt->get_result();
                if ($user = $result->fetch_assoc()) {
                    echo json_encode(['loggedIn' => true, 'username' => $user['username']]);
                } else {
                    // User no longer exists, clear cookie
                    setcookie('remember_me', '', time() - 3600, '/', '', true, true);
                    echo json_encode(['loggedIn' => false]);
                }
            } else {
                echo json_encode(['loggedIn' => false]);
            }
            break;

        case 'reset_password':
            $token = $_GET['token'] ?? '';
            if (empty($token)) {
                echo "Invalid or missing token";
            } else {
                // Check if the token is valid and not expired
                $stmt = $conn->prepare("SELECT id FROM users WHERE reset_token = ? AND reset_token_expiry > NOW()");
                $stmt->bind_param("s", $token);
                $stmt->execute();
                $result = $stmt->get_result();

                if ($result->num_rows === 0) {
                    echo "Invalid or expired reset token. Please request a new password reset.";
                } else {
                    // Serve a basic HTML page with the password reset form
                    echo '<!DOCTYPE html>
                        <html lang="en">
                        <head>
                            <meta charset="UTF-8">
                            <meta name="viewport" content="width=device-width, initial-scale=1.0">
                            <title>Reset Password</title>
                        </head>
                        <body>
                            <h2>Reset Your Password</h2>
                            <form id="resetPasswordForm">
                                <input type="hidden" name="action" value="reset_password">
                                <input type="hidden" name="token" value="' . htmlspecialchars($token) . '">
                                <label for="new_password">New Password:</label>
                                <input type="password" name="new_password" id="new_password" required>
                                <button type="submit">Reset Password</button>
                            </form>
                            <script>
                                document.getElementById("resetPasswordForm").addEventListener("submit", function(e) {
                                    e.preventDefault();
                                    fetch("account.php", {
                                        method: "POST",
                                        body: new FormData(this)
                                    })
                                    .then(response => response.json())
                                    .then(data => {
                                        if (data.success) {
                                            alert("Password reset successful. You can now log in with your new password.");
                                            window.location.href = "./";
                                        } else {
                                            alert("Error: " + data.message);
                                        }
                                    })
                                    .catch(error => {
                                        console.error("Error:", error);
                                        alert("An error occurred. Please try again.");
                                    });
                                });
                            </script>
                        </body>
                        </html>';
                }
            }
            exit; // Ensure no further processing of this request

        default:
            echo json_encode(['success' => false, 'message' => 'Invalid action']);
    }

    $conn->close();
    exit;
}
?>