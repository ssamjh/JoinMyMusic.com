<?php
require_once 'config.php';

// Create connection
$conn = new mysqli(DB_HOST, DB_USER, DB_PASS);

// Check connection
if ($conn->connect_error) {
    die("Connection failed: " . $conn->connect_error);
}

// Create database
$sql = "CREATE DATABASE IF NOT EXISTS " . DB_NAME;
if ($conn->query($sql) === TRUE) {
    echo "Database created successfully\n";
} else {
    echo "Error creating database: " . $conn->error . "\n";
}

// Select the database
$conn->select_db(DB_NAME);

// Create users table
$sql = "CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    password VARCHAR(255) NOT NULL,
    reset_token VARCHAR(64),
    reset_token_expiry DATETIME
)";

if ($conn->query($sql) === TRUE) {
    echo "Table 'users' created successfully\n";
} else {
    echo "Error creating table 'users': " . $conn->error . "\n";
}

// Create song_requests table
$sql = "CREATE TABLE IF NOT EXISTS song_requests (
    id INT AUTO_INCREMENT PRIMARY KEY,
    track_id VARCHAR(255) NOT NULL,
    ip_address VARCHAR(45) NOT NULL,
    request_datetime DATETIME DEFAULT CURRENT_TIMESTAMP,
    username VARCHAR(50),
    FOREIGN KEY (username) REFERENCES users(username)
)";

if ($conn->query($sql) === TRUE) {
    echo "Table 'song_requests' created successfully\n";
} else {
    echo "Error creating table 'song_requests': " . $conn->error . "\n";
}

// Create remember_tokens table
$sql = "CREATE TABLE IF NOT EXISTS remember_tokens (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    token VARCHAR(64) NOT NULL,
    expires DATETIME NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
)";

if ($conn->query($sql) === TRUE) {
    echo "Table 'remember_tokens' created successfully\n";
} else {
    echo "Error creating table 'remember_tokens': " . $conn->error . "\n";
}

$conn->close();
?>