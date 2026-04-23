#!/usr/bin/env bash
#
# Create and monitor an Activity Detection batch job on the Archetype AI platform.
#
# Usage:
#   ./create_activity_detection_job.sh                                      # default: home
#   ./create_activity_detection_job.sh ghost_iot_devices_yesterday.jsonl
#   ./create_activity_detection_job.sh ghost_iot_home_yesterday.jsonl my-custom-name
#
# Requires: curl, python3 (for JSON parsing)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Load env
export $(grep -v '^#' "$PROJECT_DIR/.env" | xargs)
BASE_URL="${ATAI_API_ENDPOINT}/v0.5"

FILE_ID="${1:-ghost_iot_home_yesterday.jsonl}"

if [ $# -ge 2 ]; then
  JOB_NAME="$2"
else
  STEM="${FILE_ID%.jsonl}"
  STEM="${STEM//_/-}"
  JOB_NAME="${STEM}-activity-detection"
fi

echo "============================================================"
echo " Archetype AI Activity Detection Job (Shell)"
echo "============================================================"
echo " file_id: $FILE_ID"
echo " name:    $JOB_NAME"
echo

echo "[1/3] Creating activity detection job..."

JOB_RESPONSE=$(/usr/bin/curl -s -X POST "$BASE_URL/batch/jobs" \
  -H "Authorization: Bearer $ATAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"name\": \"$JOB_NAME\",
    \"pipeline_type\": \"batch\",
    \"pipeline_key\": \"activity-detection\",
    \"inputs\": {
      \"worker.data\": [{\"file_id\": \"$FILE_ID\"}]
    },
    \"parameters\": {
      \"worker\": {
        \"parallelism\": 1,
        \"config\": {
          \"generation\": {
            \"do_sample\": true,
            \"max_new_tokens\": 256,
            \"repetition_penalty\": 1,
            \"temperature\": 0.7,
            \"top_k\": 20,
            \"top_p\": 0.8
          }
        }
      }
    }
  }")

JOB_ID=$(echo "$JOB_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
JOB_STATUS=$(echo "$JOB_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")

echo "      job_id: $JOB_ID"
echo "      status: $JOB_STATUS"
echo

echo "[2/3] Monitoring job status..."
POLL_INTERVAL=10
PREV_STATUS=""

while true; do
    STATUS_RESPONSE=$(/usr/bin/curl -s "$BASE_URL/batch/jobs/$JOB_ID" \
      -H "Authorization: Bearer $ATAI_API_KEY")
    STATUS=$(echo "$STATUS_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")

    if [ "$STATUS" != "$PREV_STATUS" ]; then
        echo "      [$(date +%H:%M:%S)] $STATUS"
        PREV_STATUS="$STATUS"
    fi

    case "$STATUS" in
        COMPLETED|FAILED|CANCELLED) break ;;
    esac

    sleep $POLL_INTERVAL
done

echo

echo "[3/3] Job events:"
EVENTS=$(/usr/bin/curl -s "$BASE_URL/batch/jobs/$JOB_ID/events" \
  -H "Authorization: Bearer $ATAI_API_KEY")

echo "$EVENTS" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for event in reversed(data.get('events', [])):
    level = event['level']
    msg = event['message']
    ts = event['created_at'][11:19]
    marker = '!!' if level == 'ERROR' else '  '
    print(f'  {marker} [{ts}] {level:<5} {msg}')
"

echo
echo "============================================================"
echo " Job: $JOB_ID"
echo " Status: $STATUS"
echo "============================================================"
echo
echo "Next: download outputs with"
echo "  python 4_download_outputs/download_outputs.py $JOB_ID outputs/$JOB_NAME"
