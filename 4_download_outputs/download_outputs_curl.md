# Download Batch Job Outputs with curl (step-by-step)

Manual curl commands for downloading batch job output artifacts.

## Prerequisites

```bash
export ATAI_API_KEY="your-api-key"
export ATAI_API_ENDPOINT="https://platform.u1.archetypeai.app"
export BASE_URL="$ATAI_API_ENDPOINT/api"
export JOB_ID="job_..."   # the job_id returned when you created the activity detection job
```

## Step 1: List Output Artifacts

```bash
curl -s "$BASE_URL/batch/jobs/$JOB_ID/outputs?limit=100" \
  -H "Authorization: Bearer $ATAI_API_KEY" | python3 -m json.tool
```

Response (lists are cursor-paginated, newest first; fetch the next page with `&after=<next_cursor>` while `has_more` is true):
```json
{
  "job_id": "job_...",
  "data": [
    {
      "id": "out_...",
      "input_file_id": "ghost_iot_home_yesterday.jsonl",
      "port_name": "worker.result",
      "data": {
        "ref": "/api/files/fil_.../content",
        "filename": "job_..__output_ghost_iot_home_yesterday.jsonl",
        "num_bytes": 4096
      }
    }
  ],
  "has_more": false,
  "next_cursor": null
}
```

`data.ref` is a path on the platform root, not a presigned URL: download it with your API key. Activity Detection writes one JSONL output per input file, so each GHOST-IoT job has a single small output.

## Step 2: Download a Single File

```bash
REF=$(curl -s "$BASE_URL/batch/jobs/$JOB_ID/outputs?limit=1" \
  -H "Authorization: Bearer $ATAI_API_KEY" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['data'][0]['data']['ref'])")

curl -s -o output.jsonl "$ATAI_API_ENDPOINT$REF" -H "Authorization: Bearer $ATAI_API_KEY"
head -c 300 output.jsonl
# {"line_index": 0, "prediction": "**Summary of Home Network Activity — 2019-10-19 UTC** ..."}
```

## Step 3: Download All Files (loop)

```bash
mkdir -p outputs/$JOB_ID
AFTER=""
while : ; do
  PAGE=$(curl -s "$BASE_URL/batch/jobs/$JOB_ID/outputs?limit=100${AFTER:+&after=$AFTER}" \
    -H "Authorization: Bearer $ATAI_API_KEY")
  echo "$PAGE" | python3 -c "
import sys, json
for out in json.load(sys.stdin)['data']:
    print(out['data']['ref'] + '\t' + out['data']['filename'])
" | while IFS=$'\t' read -r ref fname; do
    curl -s -o "outputs/$JOB_ID/$fname" "$ATAI_API_ENDPOINT$ref" -H "Authorization: Bearer $ATAI_API_KEY"
  done
  AFTER=$(echo "$PAGE" | python3 -c "
import sys, json
p = json.load(sys.stdin)
print(p['next_cursor'] if p.get('has_more') else '')")
  [ -n "$AFTER" ] || break
done
```

## Output Format

Each output JSONL line:

```json
{"line_index": 0, "prediction": "On 2019-10-19 the home wlan0 interface saw..."}
```

A record that failed (for example, one too long for the model):
```json
{"line_index": 3, "prediction": null, "error": "inference error"}
```

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/batch/jobs/{job_id}/outputs` | GET | List output artifacts (cursor-paginated) |
| `/api/files/{file_uid}/content` | GET | Download an artifact (the `data.ref` path; needs the API key) |
