#!/bin/bash
git pull
pkill -9 gunicorn
gunicorn -b 0.0.0.0:3003 --preload --workers=8 --threads=20 app:create_app()
