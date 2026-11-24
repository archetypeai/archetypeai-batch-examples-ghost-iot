#!/usr/bin/env bash
#
# Download batch job output artifacts from the Archetype AI platform.
#
# Usage:
#   ./download_outputs.sh <job_id> [output_dir]
#   ./download_outputs.sh job_6pgect4qqc8h0sd6v3rva23y8g outputs/
#
# Requires: curl, python3 (for JSON parsing)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

source "$PROJECT_DIR/env.sh"

JOB_ID="${1:?Usage: $0 <job_id> [output_dir]}"
OUTPUT_DIR="${2:-outputs/$JOB_ID}"
mkdir -p "$OUTPUT_DIR"

echo "============================================================"
echo " Download Batch Job Outputs (Shell)"
echo "============================================================"
echo "  Job:    $JOB_ID"
echo "  Saving: $OUTPUT_DIR/"
echo

# Outputs are cursor-paginated ({data, has_more, next_cursor}); each data.ref is
# an /api/files/.../content path on the platform root that needs the API key.
ROOT="${BASE_URL%/api}"
echo "[1/1] Downloading outputs..."
DOWNLOADED=0
AFTER=""
while : ; do
    PAGE=$(/usr/bin/curl -sf "$BASE_URL/batch/jobs/$JOB_ID/outputs?limit=100${AFTER:+&after=$AFTER}" \
      -H "Authorization: Bearer $ATAI_API_KEY")
    while IFS=$'\t' read -r REF FNAME; do
        [ -n "$REF" ] || continue
        /usr/bin/curl -sf -o "$OUTPUT_DIR/$FNAME" "$ROOT$REF" -H "Authorization: Bearer $ATAI_API_KEY"
        DOWNLOADED=$((DOWNLOADED + 1))
        if [ $((DOWNLOADED % 50)) -eq 0 ]; then echo "  $DOWNLOADED files..."; fi
    done < <(echo "$PAGE" | python3 -c "
import sys, json
for o in json.load(sys.stdin)['data']:
    print(o['data']['ref'] + '\t' + o['data']['filename'])")
    AFTER=$(echo "$PAGE" | python3 -c "
import sys, json
p = json.load(sys.stdin)
print(p['next_cursor'] if p.get('has_more') and p.get('next_cursor') else '')")
    [ -n "$AFTER" ] || break
done

FILE_COUNT=$(ls -1 "$OUTPUT_DIR" | wc -l | tr -d ' ')
TOTAL_SIZE=$(du -sh "$OUTPUT_DIR" | awk '{print $1}')

echo
echo "  Done! $FILE_COUNT files, $TOTAL_SIZE"
echo "  Saved to: $OUTPUT_DIR/"
echo "============================================================"
