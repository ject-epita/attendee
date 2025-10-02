#!/bin/bash
set -e

echo "Starting production initialization..."

# Wait for postgres to be ready using Python
echo "Waiting for PostgreSQL..."
python << END
import socket
import time
import sys

host = "postgres"
port = 5432
max_retries = 30
retry_interval = 1

for i in range(max_retries):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        sock.connect((host, port))
        sock.close()
        print(f"PostgreSQL is ready!")
        sys.exit(0)
    except (socket.error, socket.timeout):
        if i < max_retries - 1:
            time.sleep(retry_interval)
        else:
            print(f"Failed to connect to PostgreSQL after {max_retries} attempts")
            sys.exit(1)
END

# Wait for redis to be ready using Python
echo "Waiting for Redis..."
python << END
import socket
import time
import sys

host = "redis"
port = 6379
max_retries = 30
retry_interval = 1

for i in range(max_retries):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        sock.connect((host, port))
        sock.close()
        print(f"Redis is ready!")
        sys.exit(0)
    except (socket.error, socket.timeout):
        if i < max_retries - 1:
            time.sleep(retry_interval)
        else:
            print(f"Failed to connect to Redis after {max_retries} attempts")
            sys.exit(1)
END

# Run migrations
echo "Running database migrations..."
python manage.py migrate --noinput

# Collect static files
echo "Collecting static files..."
python manage.py collectstatic --noinput --clear

echo "Initialization complete!"

# Execute the main command
exec "$@"
