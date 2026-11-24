#!/usr/bin/env bash
#
# Multipart file upload to Archetype AI using presigned URLs (shell script).
#
# Usage:
#   ./upload_multipart.sh data/volve_inference.csv
#
# Requires: curl, python3 (for JSON parsing), dd

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

source "$PROJECT_DIR/env.sh"

FILE_PATH="${1:?Usage: $0 <file_path>}"
FILE_NAME="$(basename "$FILE_PATH")"
FILE_SIZE=$(stat -f%z "$FILE_PATH" 2>/dev/null || stat --printf="%s" "$FILE_PATH")

echo "============================================================"
echo " Archetype AI Multipart Upload (Shell)"
# Platform-supported MIME types: image/jpeg, image/png, video/mp4, text/csv,
# text/plain, application/json, application/x-ndjson.
# JSONL is newline-delimited JSON → application/x-ndjson.
case "$FILE_NAME" in
  *.jsonl|*.ndjson) FILE_TYPE="application/x-ndjson" ;;
  *.json)           FILE_TYPE="application/json"      ;;
  *.csv)            FILE_TYPE="text/csv"              ;;
  *.txt)            FILE_TYPE="text/plain"            ;;
  *)                FILE_TYPE="text/plain"            ;;
esac

echo "============================================================"
echo " File:      $FILE_NAME"
echo " Size:      $(echo "$FILE_SIZE" | awk '{printf "%.2f GB", $1/1073741824}') ($FILE_SIZE bytes)"
echo " Type:      $FILE_TYPE"
echo " Endpoint:  $BASE_URL"
echo "============================================================"
echo

# --- Step 1: Initiate -------------------------------------------------------
echo "[1/3] Initiating upload..."

INIT_RESPONSE=$(curl -s -w '\n%{http_code}' -X POST "$BASE_URL/files/uploads/initiate" \
  -H "Authorization: Bearer $ATAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"filename\":\"$FILE_NAME\",\"file_type\":\"$FILE_TYPE\",\"num_bytes\":$FILE_SIZE}")
HTTP_CODE="${INIT_RESPONSE##*$'\n'}"
INIT_RESPONSE="${INIT_RESPONSE%$'\n'*}"
if [ "$HTTP_CODE" = "409" ]; then
  echo "  '$FILE_NAME' already exists on the platform; skipping upload."
  echo "  Use it directly: python 3_batch_jobs/create_activity_detection_job.py --file $FILE_NAME"
  exit 0
elif [ "$HTTP_CODE" -ge 300 ]; then
  echo "  HTTP $HTTP_CODE from /files/uploads/initiate: $INIT_RESPONSE" >&2
  exit 1
fi

UPLOAD_ID=$(echo "$INIT_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['upload_id'])")
# If anything below fails, abort the upload so the filename isn't left stuck in UPLOADING.
trap 'curl -s -X POST "$BASE_URL/files/uploads/$UPLOAD_ID/abort" -H "Authorization: Bearer $ATAI_API_KEY" > /dev/null; echo "  upload aborted" >&2' ERR
FILE_UID=$(echo "$INIT_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['file_uid'])")
NUM_PARTS=$(echo "$INIT_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['num_parts'])")

echo "      upload_id : $UPLOAD_ID"
echo "      file_uid  : $FILE_UID"
echo "      parts     : $NUM_PARTS"
echo

# Save initiate response for part extraction
echo "$INIT_RESPONSE" > /tmp/atai_upload_init.json

# --- Step 2: Upload parts ---------------------------------------------------
echo "[2/3] Uploading $NUM_PARTS parts to S3..."
echo

COMPLETED_PARTS="["
UPLOAD_START=$(date +%s)
BYTES_UPLOADED=0

for i in $(seq 0 $((NUM_PARTS - 1))); do
    PART_NUM=$((i + 1))

    # Extract part info using python3
    PART_INFO=$(python3 -c "
import json
with open('/tmp/atai_upload_init.json') as f:
    data = json.load(f)
part = data['parts'][$i]
print(part['url'])
print(part['offset'])
print(part['length'])
")
    PART_URL=$(echo "$PART_INFO" | sed -n '1p')
    OFFSET=$(echo "$PART_INFO" | sed -n '2p')
    LENGTH=$(echo "$PART_INFO" | sed -n '3p')

    # Extract bytes [OFFSET, OFFSET+LENGTH) of the file (portable: macOS and Linux)
    PART_FILE="/tmp/atai_part_${PART_NUM}.bin"
    tail -c +$((OFFSET + 1)) "$FILE_PATH" | head -c "$LENGTH" > "$PART_FILE"

    PART_START=$(date +%s)

    # Upload part and capture ETag
    ETAG=$(curl -s -X PUT "$PART_URL" \
      -H "Content-Length: $LENGTH" \
      --data-binary "@$PART_FILE" \
      -D - -o /dev/null 2>/dev/null | grep -i "^etag:" | tr -d '\r' | awk '{print $2}' | tr -d '"')

    rm -f "$PART_FILE"

    PART_END=$(date +%s)
    PART_ELAPSED=$((PART_END - PART_START))
    [ "$PART_ELAPSED" -eq 0 ] && PART_ELAPSED=1

    BYTES_UPLOADED=$((BYTES_UPLOADED + LENGTH))
    PART_SPEED=$((LENGTH / PART_ELAPSED / 1048576))
    OVERALL_ELAPSED=$((PART_END - UPLOAD_START))
    [ "$OVERALL_ELAPSED" -eq 0 ] && OVERALL_ELAPSED=1
    PCT=$((BYTES_UPLOADED * 100 / FILE_SIZE))
    REMAINING=$((FILE_SIZE - BYTES_UPLOADED))
    SPEED=$((BYTES_UPLOADED / OVERALL_ELAPSED))
    [ "$SPEED" -eq 0 ] && SPEED=1
    ETA=$((REMAINING / SPEED))

    printf "  Part %2d/%d  [%3d%%]  %s/%s  %d MB/s  ETag: %s...  ETA: %ds\n" \
      "$PART_NUM" "$NUM_PARTS" "$PCT" \
      "$(echo "$BYTES_UPLOADED" | awk '{printf "%.2f GB", $1/1073741824}')" \
      "$(echo "$FILE_SIZE" | awk '{printf "%.2f GB", $1/1073741824}')" \
      "$PART_SPEED" "${ETAG:0:12}" "$ETA"

    # Build completed parts JSON
    if [ "$i" -gt 0 ]; then COMPLETED_PARTS="$COMPLETED_PARTS,"; fi
    COMPLETED_PARTS="$COMPLETED_PARTS{\"part_number\":$PART_NUM,\"part_token\":\"$ETAG\"}"
done

COMPLETED_PARTS="$COMPLETED_PARTS]"
UPLOAD_END=$(date +%s)
TOTAL_TIME=$((UPLOAD_END - UPLOAD_START))
[ "$TOTAL_TIME" -eq 0 ] && TOTAL_TIME=1
AVG_SPEED=$((FILE_SIZE / TOTAL_TIME / 1048576))

echo
echo "      All parts uploaded in ${TOTAL_TIME}s (avg ${AVG_SPEED} MB/s)"
echo

# --- Step 3: Complete -------------------------------------------------------
echo "[3/3] Completing upload..."

COMPLETE_RESPONSE=$(curl -s -X POST "$BASE_URL/files/uploads/$UPLOAD_ID/complete" \
  -H "Authorization: Bearer $ATAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"parts\":$COMPLETED_PARTS}")

echo "      $COMPLETE_RESPONSE"
echo
echo "============================================================"
echo " DONE  file_uid: $(echo "$COMPLETE_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('file_uid','$FILE_UID'))")"
echo "       status:   $(echo "$COMPLETE_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('file_status','unknown'))")"
echo "============================================================"

# Cleanup
rm -f /tmp/atai_upload_init.json
