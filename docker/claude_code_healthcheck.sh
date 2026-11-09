#!/bin/sh
# Check that the Claude Code login inside the container still works.
# Run it on a schedule, e.g. from Unraid's User Scripts plugin:
#   docker exec pr-agent claude-code-healthcheck
# If NOTIFY_WEBHOOK_URL is set (for example a Home Assistant webhook), a failure is POSTed there.
set -u

out=$(cd /tmp && echo "Reply with exactly: OK" | timeout 120 claude -p --output-format json --tools "" \
    --model "${HEALTHCHECK_MODEL:-claude-haiku-5-5}" --no-session-persistence --strict-mcp-config 2>&1)
status=$?

if [ $status -eq 0 ] && printf '%s' "$out" | grep -q '"is_error":false'; then
    echo "claude-code-healthcheck: OK"
    exit 0
fi

reason=$(printf '%s' "$out" | sed -n 's/.*"result":"\([^"]*\)".*/\1/p' | head -c 300)
[ -n "$reason" ] || reason=$(printf '%s' "$out" | tail -c 300)
echo "claude-code-healthcheck: FAILED: $reason" >&2

if [ -n "${NOTIFY_WEBHOOK_URL:-}" ]; then
    msg=$(printf '%s' "$reason" | tr -d '"\\\n')
    curl -fsS -m 15 -X POST -H "Content-Type: application/json" \
        -d "{\"title\":\"PR-Agent: Claude Code login failing\",\"message\":\"$msg\"}" \
        "$NOTIFY_WEBHOOK_URL" >/dev/null || echo "claude-code-healthcheck: notify failed" >&2
fi
exit 1
