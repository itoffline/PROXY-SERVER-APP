#!/bin/sh

set -eu

REPO_URL="https://github.com/monosans/proxy-list.git"
DATA_DIR="/data"
RELEASES_DIR="$DATA_DIR/releases"
CURRENT="$DATA_DIR/current"
UPDATE_INTERVAL=600

mkdir -p "$RELEASES_DIR"

update_repository() {
    echo "[$(date)] Starting repository update..."

    STAGING="$DATA_DIR/staging-$$"
    RELEASE="$RELEASES_DIR/$(date +%Y%m%d%H%M%S)"
    NEXT_LINK="$DATA_DIR/current.new"

    rm -rf "$STAGING" "$NEXT_LINK"

    if ! git clone --depth 1 "$REPO_URL" "$STAGING"; then
        rm -rf "$STAGING"
        echo "[$(date)] ERROR: Git clone failed"
        return 1
    fi

    # Проверяем наличие файлов и корректность JSON
    if ! python -c '
import json, sys
from pathlib import Path

p = Path(sys.argv[1])

for name in ("proxies.json", "proxies_pretty.json"):
    with (p / name).open() as f:
        json.load(f)

print("JSON validation successful")
' "$STAGING"; then
        rm -rf "$STAGING"
        echo "[$(date)] ERROR: Invalid JSON"
        return 1
    fi

    # Публикуем новую версию
    mv "$STAGING" "$RELEASE"

    ln -s "$RELEASE" "$NEXT_LINK"

    # Атомарно переключаем активную версию
    python -c '
import os, sys
os.replace(sys.argv[1], sys.argv[2])
' "$NEXT_LINK" "$CURRENT"

    # Оставляем три последние версии
    python -c '
import os, sys, shutil
from pathlib import Path

root = Path(sys.argv[1])
current = Path(sys.argv[2]).resolve()

dirs = sorted(
    [p for p in root.iterdir() if p.is_dir()],
    key=lambda p: p.stat().st_mtime,
    reverse=True
)

keep = {current}
keep.update(dirs[:3])

for directory in dirs:
    if directory not in keep:
        shutil.rmtree(directory)
' "$RELEASES_DIR" "$CURRENT"

    echo "[$(date)] Repository successfully updated"
}

# Первоначальная загрузка
if ! update_repository; then
    if [ ! -L "$CURRENT" ]; then
        echo "FATAL: No available proxy list"
        exit 1
    fi

    echo "Using previously downloaded version"
fi

# Запускаем HTTP-сервер
python -m http.server 8080 \
    --bind 0.0.0.0 \
    --directory "$CURRENT" &

HTTP_PID=$!

trap 'kill "$HTTP_PID" 2>/dev/null || true' EXIT INT TERM

echo "HTTP server started on port 8080"

# Автоматическое обновление
while true; do
    sleep "$UPDATE_INTERVAL"

    update_repository || echo "[$(date)] Update failed. Keeping previous version"
done
