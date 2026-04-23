# Download Batch Job Outputs with curl (step-by-step)

Manual curl commands for downloading batch job output artifacts.

## Prerequisites

```bash
export ATAI_API_KEY="your-api-key"
export ATAI_API_ENDPOINT="https://api.u1.archetypeai.app"
export BASE_URL="$ATAI_API_ENDPOINT/v0.5"
export JOB_ID="job_..."   # the job_id returned when you created the activity detection job
```

## Step 1: List Output Artifacts

```bash
curl -s "$BASE_URL/batch/jobs/$JOB_ID/outputs?limit=50&offset=0" \
  -H "Authorization: Bearer $ATAI_API_KEY" | python3 -m json.tool
```

Response:
```json
{
  "job_id": "job_...",
  "total": 1,
  "offset": 0,
  "limit": 50,
  "outputs": [
    {
      "id": "out_...",
      "data": {
        "ref": "https://s3...presigned-url...",
        "filename": "pred_ghost_iot_home_yesterday_part_0.jsonl",
        "num_bytes": 512
      },
      "expires_at": "..."
    }
  ]
}
```

The Activity Detection outputs are JSONL (one line per input prompt) so for the GHOST-IoT demo each job typically has a single small output file.

## Step 2: Download a Single File

```bash
URL=$(curl -s "$BASE_URL/batch/jobs/$JOB_ID/outputs?limit=1&offset=0" \
  -H "Authorization: Bearer $ATAI_API_KEY" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['outputs'][0]['data']['ref'])")

curl -s -o output.jsonl "$URL"
head output.jsonl
# {"line_index":0,"prediction":"On 2019-10-19 the home network saw..."}
```

## Step 3: Download All Files (loop)

```bash
mkdir -p outputs/$JOB_ID

TOTAL=$(curl -s "$BASE_URL/batch/jobs/$JOB_ID/outputs?limit=1" \
  -H "Authorization: Bearer $ATAI_API_KEY" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['total'])")

echo "Total files: $TOTAL"

OFFSET=0
LIMIT=50

while [ "$OFFSET" -lt "$TOTAL" ]; do
  PAGE=$(curl -s "$BASE_URL/batch/jobs/$JOB_ID/outputs?limit=$LIMIT&offset=$OFFSET" \
    -H "Authorization: Bearer $ATAI_API_KEY")

  echo "$PAGE" | python3 -c "
import sys, json
for out in json.load(sys.stdin)['outputs']:
    print(out['data']['ref'] + '\t' + out['data']['filename'])
" | while IFS=$'\t' read -r url fname; do
    curl -s -o "outputs/$JOB_ID/$fname" "$url"
  done

  OFFSET=$((OFFSET + LIMIT))
  echo "Downloaded $OFFSET/$TOTAL..."
done
```

## Output Format

Each output JSONL line:

```json
{"line_index": 0, "prediction": "On 2019-10-19 the home wlan0 interface saw..."}
```

Or on parse failure:
```json
{"line_index": 3, "prediction": null, "error": "parse error"}
```

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v0.5/batch/jobs/{job_id}/outputs` | GET | List output artifacts (paginated) |
| `{presigned_url}` | GET | Download artifact (no auth, 1hr expiry) |
