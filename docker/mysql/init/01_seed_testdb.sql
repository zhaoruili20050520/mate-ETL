CREATE TABLE IF NOT EXISTS test_users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    phone VARCHAR(32)
);

INSERT INTO test_users (username, password_hash, email, phone)
VALUES
    ('alice', 'secret123hash', 'alice@example.com', '13800138000'),
    ('bob', 'bobpass456', 'bob@test.com', '13900139000');
