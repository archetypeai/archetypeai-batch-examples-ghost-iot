# Create Activity Detection Job with curl (step-by-step)

Manual curl commands for creating and monitoring Activity Detection batch jobs for the GHOST-IoT demo.

## Prerequisites

```bash
export ATAI_API_KEY="your-api-key"
export ATAI_API_ENDPOINT="https://api.u1.archetypeai.app"
export BASE_URL="$ATAI_API_ENDPOINT/v0.5"
```

Make sure you've already uploaded the JSONL files (see `2_upload/`).

## Input Format

Each input file is JSONL with `system`, `instruction`, and/or `prompt` fields. The two inputs for this demo are produced by:
- `1_prepare_data/prepare_home_level_jsonl.py` → `ghost_iot_home_yesterday.jsonl` (1 prompt)
- `1_prepare_data/prepare_device_level_jsonl.py` → `ghost_iot_devices_yesterday.jsonl` (N prompts)

## Step 1: Create a Job

### Home-level job (1 prompt in, 1 narrative out)

```bash
curl -s -X POST "$BASE_URL/batch/jobs" \
  -H "Authorization: Bearer $ATAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "ghost-iot-home-yesterday-activity-detection",
    "pipeline_type": "batch",
    "pipeline_key": "activity-detection",
    "inputs": {
      "worker.data": [{"file_id": "ghost_iot_home_yesterday.jsonl"}]
    },
    "parameters": {
      "worker": {
        "parallelism": 1,
        "config": {
          "generation": {
            "do_sample": true,
            "max_new_tokens": 256,
            "repetition_penalty": 1,
            "temperature": 0.7,
            "top_k": 20,
            "top_p": 0.8
          }
        }
      }
    }
  }' | python3 -m json.tool
```

### Device-level job (N prompts in, N narratives out)

```bash
curl -s -X POST "$BASE_URL/batch/jobs" \
  -H "Authorization: Bearer $ATAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "ghost-iot-devices-yesterday-activity-detection",
    "pipeline_type": "batch",
    "pipeline_key": "activity-detection",
    "inputs": {
      "worker.data": [{"file_id": "ghost_iot_devices_yesterday.jsonl"}]
    },
    "parameters": {
      "worker": {
        "parallelism": 1,
        "config": {
          "generation": {
            "do_sample": true,
            "max_new_tokens": 256,
            "repetition_penalty": 1,
            "temperature": 0.7,
            "top_k": 20,
            "top_p": 0.8
          }
        }
      }
    }
  }' | python3 -m json.tool
```

Response:
```json
{
  "id": "job_...",
  "name": "...",
  "pipeline_type": "batch",
  "pipeline_key": "activity-detection",
  "pipeline_version": "0.0.20",
  "status": "PENDING"
}
```

## Step 2: Check Job Status

```bash
JOB_ID="job_..."

curl -s "$BASE_URL/batch/jobs/$JOB_ID" \
  -H "Authorization: Bearer $ATAI_API_KEY" | python3 -m json.tool
```

Status progresses: `PENDING` → `RUNNING` → `COMPLETED` / `FAILED` / `CANCELLED`.

## Step 3: View Job Events

```bash
curl -s "$BASE_URL/batch/jobs/$JOB_ID/events" \
  -H "Authorization: Bearer $ATAI_API_KEY" | python3 -m json.tool
```

## Output Format

Each output line:
```json
{"line_index": 0, "prediction": "On 2019-10-19 the home wlan0 interface saw..."}
```

On error:
```json
{"line_index": 5, "prediction": null, "error": "parse error"}
```

## Important Notes

- Input must be **JSONL format** — raw CSV will produce `"error": "parse error"` for every line.
- Both job inputs are tiny (≤ a few KB), so jobs typically complete in a minute or two.
