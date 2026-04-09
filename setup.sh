#!/usr/bin/env bash
set -euo pipefail

echo "=============================="
echo " Atlas Brain — Setup"
echo "=============================="
echo ""
echo "This will create a .env file with your configuration."
echo "IMPORTANT: Never commit .env to version control."
echo ""

ENV_FILE=".env"

# -- Gather info -----------------------------------------------
read -rp "Your name: " USER_NAME
read -rp "Timezone [UTC]: " TIMEZONE
TIMEZONE="${TIMEZONE:-UTC}"
echo ""

read -rp "Telegram Bot Token: " BOT_TOKEN
read -rp "Telegram Chat ID: " CHAT_ID
read -rp "Telegram Thread ID (optional, press Enter to skip): " THREAD_ID
echo ""

read -rp "LLM Gateway URL [http://127.0.0.1:18789/v1/chat/completions]: " GATEWAY_URL
GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:18789/v1/chat/completions}"
read -rp "LLM Gateway Token: " GATEWAY_TOKEN
echo ""

read -rp "Default model [anthropic/claude-sonnet-4-6]: " MODEL
MODEL="${MODEL:-anthropic/claude-sonnet-4-6}"
read -rp "Deep model [anthropic/claude-opus-4-6]: " DEEP_MODEL
DEEP_MODEL="${DEEP_MODEL:-anthropic/claude-opus-4-6}"
echo ""

read -rp "Reduced-interruption days (weekday nums, e.g. 2,4) [2,4]: " PARENTING_DAYS
PARENTING_DAYS="${PARENTING_DAYS:-2,4}"
read -rp "Quiet hours start (0-23) [22]: " QUIET_START
QUIET_START="${QUIET_START:-22}"
read -rp "Quiet hours end (0-23) [6]: " QUIET_END
QUIET_END="${QUIET_END:-6}"
echo ""

read -rp "Google Calendar account (optional, press Enter to skip): " GOG_ACCOUNT
GOG_PASSWORD=""
if [[ -n "$GOG_ACCOUNT" ]]; then
    read -rp "Google Calendar keyring password: " GOG_PASSWORD
fi

# -- Write .env ------------------------------------------------
cat > "$ENV_FILE" <<EOF
ATLAS_USER_NAME=${USER_NAME}
ATLAS_TIMEZONE=${TIMEZONE}
ATLAS_BOT_TOKEN=${BOT_TOKEN}
ATLAS_CHAT_ID=${CHAT_ID}
ATLAS_THREAD_ID=${THREAD_ID}
ATLAS_GATEWAY_URL=${GATEWAY_URL}
ATLAS_GATEWAY_TOKEN=${GATEWAY_TOKEN}
ATLAS_MODEL=${MODEL}
ATLAS_DEEP_MODEL=${DEEP_MODEL}
ATLAS_PARENTING_DAYS=${PARENTING_DAYS}
ATLAS_QUIET_START=${QUIET_START}
ATLAS_QUIET_END=${QUIET_END}
GOG_ACCOUNT=${GOG_ACCOUNT}
GOG_KEYRING_PASSWORD=${GOG_PASSWORD}
EOF

echo ""
echo "Wrote $ENV_FILE"

# -- Create directories ----------------------------------------
mkdir -p .state memory memory/distillation-staging
echo "Created directories: .state/ memory/ memory/distillation-staging/"

# -- Initialize DB ---------------------------------------------
echo ""
echo "Initializing database..."
python3 brain.py --migrate 2>/dev/null || python3 brain.py --status
echo ""

# -- Verify ----------------------------------------------------
echo "Running status check..."
python3 brain.py --status
echo ""

# -- Crontab ---------------------------------------------------
echo "Suggested crontab entries:"
BRAIN_DIR="$(pwd)"
echo ""
echo "  # Atlas Brain — main loop (every 20 min)"
echo "  */20 * * * * cd ${BRAIN_DIR} && python3 brain.py >> .state/atlas-brain-cron.log 2>&1"
echo ""
echo "  # Morning briefing (7:15 AM)"
echo "  15 7 * * * cd ${BRAIN_DIR} && python3 brain.py --morning >> .state/atlas-brain-cron.log 2>&1"
echo ""
echo "  # Nightly distillation (11:05 PM)"
echo "  5 23 * * * cd ${BRAIN_DIR} && python3 brain.py --nightly >> .state/atlas-brain-cron.log 2>&1"
echo ""
echo "  # Weekly review (Sunday 10:05 AM)"
echo "  5 10 * * 0 cd ${BRAIN_DIR} && python3 brain.py --weekly >> .state/atlas-brain-cron.log 2>&1"
echo ""
echo "  # Watchdog (every 30 min)"
echo "  */30 * * * * cd ${BRAIN_DIR} && python3 brain.py --watchdog >> .state/atlas-brain-cron.log 2>&1"
echo ""

read -rp "Install these crontab entries now? [y/N]: " INSTALL_CRON
if [[ "$INSTALL_CRON" =~ ^[Yy] ]]; then
    (crontab -l 2>/dev/null || true; cat <<CRON
# Atlas Brain — main loop (every 20 min)
*/20 * * * * cd ${BRAIN_DIR} && python3 brain.py >> .state/atlas-brain-cron.log 2>&1
# Morning briefing (7:15 AM)
15 7 * * * cd ${BRAIN_DIR} && python3 brain.py --morning >> .state/atlas-brain-cron.log 2>&1
# Nightly distillation (11:05 PM)
5 23 * * * cd ${BRAIN_DIR} && python3 brain.py --nightly >> .state/atlas-brain-cron.log 2>&1
# Weekly review (Sunday 10:05 AM)
5 10 * * 0 cd ${BRAIN_DIR} && python3 brain.py --weekly >> .state/atlas-brain-cron.log 2>&1
# Watchdog (every 30 min)
*/30 * * * * cd ${BRAIN_DIR} && python3 brain.py --watchdog >> .state/atlas-brain-cron.log 2>&1
CRON
    ) | crontab -
    echo "Crontab installed."
else
    echo "Skipped. You can install manually later."
fi

echo ""
echo "Setup complete. Run 'python3 brain.py --status' to verify."
