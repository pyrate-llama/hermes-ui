#!/bin/bash
# Start Hermes UI — kills old instances, starts fresh

PSQL="/Users/jonbest/.pg0/installation/18.1.0/bin/psql"

# Load API keys from ~/.hermes/.env into environment
if [ -f ~/.hermes/.env ]; then
  set -a
  source ~/.hermes/.env
  set +a
fi

# Gracefully stop hindsight daemon first (SIGTERM, not SIGKILL)
# This lets it finish any in-progress consolidation and clean up
lsof -ti:9177 | xargs kill -15 2>/dev/null
sleep 2
# Force kill only if it didn't stop gracefully
lsof -ti:9177 | xargs kill -9 2>/dev/null

# Kill gateway and UI proxy by port (SIGTERM first, then force)
for port in 3333 8642; do
  lsof -ti:$port | xargs kill -15 2>/dev/null
done
sleep 2
for port in 3333 8642; do
  lsof -ti:$port | xargs kill -9 2>/dev/null
done

# Also kill any gateway processes by name — catches orphans that
# changed ports or haven't bound yet
pkill -f "hermes_cli.main gateway" 2>/dev/null
pkill -f "hermes_cli\.main gateway" 2>/dev/null
# Kill stale serve.py proxies
pkill -f "python3 serve.py" 2>/dev/null
sleep 1

# Verify nothing is left on our ports
for port in 3333 8642 9177; do
  lsof -ti:$port | xargs kill -9 2>/dev/null
done
sleep 1

# Clear stale consolidation tasks in PostgreSQL that crash the daemon
PGPASSWORD=hindsight "$PSQL" -h localhost -U hindsight -d hindsight \
  -c "UPDATE async_operations SET status = 'completed', completed_at = now() WHERE status IN ('pending', 'processing');" \
  2>/dev/null

# Start Hermes gateway (port 8642)
cd ~/.hermes
nohup hermes-agent/venv/bin/python -m hermes_cli.main gateway run --replace > /tmp/hermes-webapi.log 2>&1 &
sleep 3

# Start Hermes UI proxy (port 3333)
cd ~/hermes-ui
nohup python3 serve.py > /tmp/hermes-ui.log 2>&1 &
sleep 1

# Open in browser
open http://localhost:3333/hermes-ui.html
