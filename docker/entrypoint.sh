#!/bin/sh
# Ensure the appuser owns the mounted volumes (cache + Excel files),
# then drop privileges and run the server based on FSC_MODE.

# Fix ownership of cache volume and input/output files if present
chown -R appuser:appuser /app/cache 2>/dev/null || true
chown appuser:appuser /app/universities.xlsx 2>/dev/null || true
chown appuser:appuser /app/faculty_data.xlsx 2>/dev/null || true
chown appuser:appuser /app/config.yaml 2>/dev/null || true
chown appuser:appuser /app/schema.json 2>/dev/null || true

MODE="${FSC_MODE:-api}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}"

if [ "$MODE" = "mcp" ]; then
    exec su -s /bin/sh appuser -c \
        "exec python -m fscout.mcp_server --sse --host 0.0.0.0 --port 8000"
else
    exec su -s /bin/sh appuser -c \
        "exec python -m fscout.rest_api --host 0.0.0.0 --port 8000"
fi
