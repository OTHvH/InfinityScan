#!/bin/bash
# Local test seed script for InfinityScan
# This script seeds the database with sample data from local manga files
# 
# Usage: ./scripts/seed_local.sh /path/to/your/manga/folder

MANGA_PATH="${1:-}"

if [ -z "$MANGA_PATH" ]; then
    echo "Usage: $0 /path/to/manga/folder"
    echo ""
    echo "Example: $0 /home/OTH/Documents/Mangus"
    exit 1
fi

if [ ! -d "$MANGA_PATH" ]; then
    echo "Error: Directory does not exist: $MANGA_PATH"
    exit 1
fi

echo "Seeding InfinityScan with manga from: $MANGA_PATH"

cd "$(dirname "$0")/../api"

# Set environment variables and run seed
export DATABASE_URL="sqlite:///./infinityscan.db"
export MANGA_LOCAL_PATH="$MANGA_PATH"

source .venv/bin/activate
python seed.py
