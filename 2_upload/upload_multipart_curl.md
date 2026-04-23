# Multipart Upload with curl (step-by-step)

Manual curl commands for the 3-step presigned URL upload flow. For the GHOST-IoT demo the prepared JSONL files are very small (< 1 KB), so the server will return a `simple` upload strategy (one presigned PUT). The flow below works regardless of strategy.

## Prerequisites

```bash
export ATAI_API_KEY="your-api-key"
export ATAI_API_ENDPOINT="https://api.u1.archetypeai.app"
export BASE_URL="$ATAI_API_ENDPOINT/v0.5"
```

## Step 1: Initiate Upload

```bash
FILE="data/ghost_iot_home_yesterday.jsonl"
FILE_SIZE=$(stat -f%z "$FILE")
FILE_NAME=$(basename "$FILE")

curl -s -X POST "$BASE_URL/files/uploads/initiate" \
  -H "Authorization: Bearer $ATAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"filename\":\"$FILE_NAME\",\"file_type\":\"text/plain\",\"num_bytes\":$FILE_SIZE}" \
  | tee /tmp/upload_init.json \
  | python3 -m json.tool
```

Response (simple strategy):
```json
{
  "upload_id": "upl_abc123...",
  "file_uid": "fil_xyz789...",
  "strategy": "simple",
  "num_parts": 1,
  "part_size": 512,
  "parts": [
    {"part_number": 1, "url": "https://s3...presigned-url...", "offset": 0, "length": 512}
  ],
  "expires_at": "..."
}
```

## Step 2: Upload the (Single) Part

```bash
PART_URL=$(python3 -c "import json; print(json.load(open('/tmp/upload_init.json'))['parts'][0]['url'])")
LENGTH=$(python3 -c "import json; print(json.load(open('/tmp/upload_init.json'))['parts'][0]['length'])")

curl -X PUT "$PART_URL" \
  -H "Content-Length: $LENGTH" \
  --data-binary @"$FILE" \
  -D /tmp/part_headers.txt \
  -o /dev/null -s -w "HTTP %{http_code} in %{time_total}s\n"

ETAG=$(grep -i etag /tmp/part_headers.txt | awk '{print $2}' | tr -d '"\r')
echo "ETag: $ETAG"
```

## Step 3: Complete Upload

```bash
UPLOAD_ID=$(python3 -c "import json; print(json.load(open('/tmp/upload_init.json'))['upload_id'])")

curl -s -X POST "$BASE_URL/files/uploads/$UPLOAD_ID/complete" \
  -H "Authorization: Bearer $ATAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"parts\":[{\"part_number\":1,\"part_token\":\"$ETAG\"}]}" \
  | python3 -m json.tool
```

Repeat for `data/ghost_iot_devices_yesterday.jsonl`.

## Abort (if needed)

```bash
curl -s -X POST "$BASE_URL/files/uploads/$UPLOAD_ID/abort" \
  -H "Authorization: Bearer $ATAI_API_KEY"
```
