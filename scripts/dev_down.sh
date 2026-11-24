#!/usr/bin/env bash
cd "$(dirname "$0")/.."
for f in var/logs/*.pid; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null; rm -f "$f"; done
pkill -f next-server 2>/dev/null || true
pkill -f "workers.executor.main" 2>/dev/null || true
echo stopped
