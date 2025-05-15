#!/bin/bash

APP_CMD="gunicorn -b 0.0.0.0:3003 --preload --workers=8 --threads=20 'app:create_app()'"
GUNICORN_PID=""

start_app() {
    echo "Starting app..."
    $APP_CMD &
    GUNICORN_PID=$!
    echo "App started with PID $GUNICORN_PID"
}

stop_app() {
    if [ -n "$GUNICORN_PID" ]; then
        echo "Stopping app with PID $GUNICORN_PID..."
        kill $GUNICORN_PID
        wait $GUNICORN_PID 2>/dev/null
    fi
}

check_for_updates() {
    git remote update > /dev/null
    LOCAL=$(git rev-parse @)
    REMOTE=$(git rev-parse @{u})
    BASE=$(git merge-base @ @{u})

    if [ $LOCAL = $REMOTE ]; then
        return 1  # No update
    elif [ $LOCAL = $BASE ]; then
        return 0  # Update available
    else
        echo "Repository is ahead or diverged."
        return 2
    fi
}

start_app

while true; do
    sleep 60  # Check every 60 seconds

    check_for_updates
    result=$?

    if [ $result -eq 0 ]; then
        echo "Update detected. Pulling latest code..."
        git pull
        stop_app
        start_app
    elif [ $result -eq 2 ]; then
        echo "Repository has diverged or is ahead. Manual intervention may be required."
    fi
done
