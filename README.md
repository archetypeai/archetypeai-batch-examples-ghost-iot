# Archetype AI Batch Examples — GHOST-IoT (Activity Detection)

An end-to-end example of running Newton's **Activity Detection** batch pipeline against smart-home WiFi flow data to answer two questions:

1. **What happened at the home level yesterday?** — one daily summary narrative across the whole home.
2. **What happened at the device level yesterday?** — one narrative per active device.

Modeled on [archetypeai-batch-examples-volve](https://github.com/archetypeai/archetypeai-batch-examples-volve), this repo uses **only the Activity Detection pipeline** (no Machine State classification, no n-shot examples, no evaluation — Activity Detection outputs are natural language).

## Pipeline

```
CSV flows ──┬─► prepare_home_level_jsonl.py     ─► ghost_iot_home_yesterday.jsonl   (1 prompt)
            └─► prepare_device_level_jsonl.py   ─► ghost_iot_devices_yesterday.jsonl (N prompts)

                                │ upload (multipart presigned URL)
                                ▼
                        Archetype AI platform
                                │ Activity Detection batch job
                                ▼
                        outputs/.../pred_*.jsonl
                                │ view_results.py
                                ▼
                          natural-language narratives
```

| Step | Script | Description |
|------|--------|-------------|
| Prepare | `1_prepare_data/` | Build JSONL prompts from the raw CSV |
| Upload | `2_upload/` | Multipart presigned-URL upload (Python/shell/curl) |
| Batch job | `3_batch_jobs/` | Create & monitor the Activity Detection job |
| Download | `4_download_outputs/` | Paginated download of prediction files |
| View | `5_view_results/` | Pretty-print the natural-language outputs |

## Dataset

This demo uses the **GHOST-IoT** public dataset — WiFi flows captured on the wlan0 interface of a real smart-home gateway. See `data/wlan0_ipv4_flows_db.csv`.

- Source: [GHOST-IoT dataset](https://github.com/gspathoulas/ghost-iot-dataset) (EU Horizon 2020 project)
- Interface: wlan0 (IPv4 flows)
- Rows: 3,399 bidirectional flows
- Devices: 11 unique MAC addresses (anonymized)
- Date range: **2019-10-10 to 2019-10-20 UTC**
- Paper: Anagnostopoulos, M.; Spathoulas, G.; Viaño, B.; Augusto-Gonzalez, J. *Tracing Your Smart-Home Devices Conversations: A Real World IoT Traffic Data-Set.* Sensors **2020**, 20, 6600. [DOI:10.3390/s20226600](https://doi.org/10.3390/s20226600)

### What counts as "yesterday"?

The dataset is from October 2019, so "yesterday" can't be literal. This demo pins "yesterday" to **2019-10-19 UTC** — the last full calendar day in the data. Override with `--date YYYY-MM-DD` on either prepare script.

### Flow schema (wlan0_ipv4_flows_db.csv)

Each row is a bidirectional network flow between two endpoints (`a` and `b`). Selected columns used by this demo:

| Column | Description |
|--------|-------------|
| `mac_a`, `mac_b` | MAC addresses of the two endpoints (anonymized hex) |
| `ip_a`, `ip_b` | IPs (anonymized) |
| `port_a`, `port_b` | TCP/UDP ports |
| `prot` | Application-layer protocol (HTTP, HTTPS, DNS, Spotify, WhatsApp, ApplePush, mDNS, SSDP, NTP, etc.) |
| `tran_prot` | Transport protocol (`6`=TCP, `17`=UDP, `1`=ICMP) |
| `ts_start`, `ts_end` | Unix epoch seconds (UTC) |
| `packets_a`, `bytes_a` | Packet/byte counts from a → b |
| `packets_b`, `bytes_b` | Packet/byte counts from b → a |

The full field dictionary is in [Description of fields.pdf](https://github.com/gspathoulas/ghost-iot-dataset) from the original dataset.

## 1. Setup

```bash
git clone <this repo>
cd archetypeai-batch-examples-ghost-iot

cp .env.example .env
# Edit .env with your ATAI_API_KEY and ATAI_API_ENDPOINT

python3 -m venv myenv
source myenv/bin/activate
pip install requests
```

To run the curl examples, also export these into your shell:

```bash
export ATAI_API_KEY="your-api-key"
export ATAI_API_ENDPOINT="https://api.u1.archetypeai.app"
export BASE_URL="$ATAI_API_ENDPOINT/v0.5"
```

## 2. Prepare JSONL Prompts

The Activity Detection pipeline consumes JSONL where each line is an `InferenceRecord`: `system`, `instruction`, `prompt`, and an optional `inputs` array for extra context (text/image/video). See the [Nano Inference input format reference](https://github.com/archetypeai/atai_core/tree/main/services/jos_service/nano_inference#input-format).

**Design choice: raw flows only, no pre-aggregation.** The prep scripts do only what a real customer can realistically do — filter the CSV by date (and by device for device-level), then serialize the matching rows as text. Percentages, peak hours, most-active-device, device-type inference — all of that is Newton's job. If the prompt contained pre-computed stats, Newton would just be paraphrasing our work. Here the output is genuinely Newton's analysis of the raw flow log.

Structurally, each record puts the **question** in `prompt` and the **evidence** in `inputs[0].data` — matching the InferenceRecord schema's intent (the `inputs` array is designed for extra context: text, image, video).

```json
{
  "system": "You are a network security analyst reviewing smart-home WiFi traffic...",
  "instruction": "Analyze the attached flow log and describe what happened on the home network on this day. Summarize activity level, dominant protocols, temporal patterns, most-active device, and flag anything unusual.",
  "prompt": "Date: 2019-10-19 UTC. Scope: home wlan0 interface. Flow count: 78.",
  "inputs": [
    {
      "type": "text",
      "format": "plain",
      "data": "Flow log fields (pipe-separated): time_utc|mac_a|mac_b|prot|tran|port_a|port_b|bytes_a|bytes_b|pkts_a|pkts_b. Transport: 6=TCP, 17=UDP, 1=ICMP, 58=ICMPv6, 2=IGMP.\n\n15:55:11|ebd1a7fa8544|e323b826aa71|DHCP|17|67|68|0|900|0|3\n15:55:14|e323b826aa71|13d35af5c06b|DHCP|17|68|67|900|0|3|0\n..."
    }
  ]
}
```

Two small scripts build the two prompt files:

```bash
# Home-level: 1 prompt summarizing all flows on 2019-10-19
python 1_prepare_data/prepare_home_level_jsonl.py

# Device-level: one prompt per device active on 2019-10-19
python 1_prepare_data/prepare_device_level_jsonl.py
```

Outputs:

| File | Lines | Description |
|------|-------|-------------|
| `data/ghost_iot_home_yesterday.jsonl` | 1 | Raw home-level flow log wrapped as one InferenceRecord |
| `data/ghost_iot_devices_yesterday.jsonl` | 5 | Per-device flow logs — one InferenceRecord per active device |

Both scripts accept `--date YYYY-MM-DD` to pick any day in the dataset (default: `2019-10-19`).

## 3. Upload Files

Both JSONL files are tiny (< 10 KB), so the platform will use the `simple` upload strategy (single presigned PUT). The Python/shell scripts below handle both simple and multipart transparently.

### Python

```bash
python 2_upload/upload_multipart.py data/ghost_iot_home_yesterday.jsonl
python 2_upload/upload_multipart.py data/ghost_iot_devices_yesterday.jsonl
```

### Shell

```bash
./2_upload/upload_multipart.sh data/ghost_iot_home_yesterday.jsonl
./2_upload/upload_multipart.sh data/ghost_iot_devices_yesterday.jsonl
```

### curl

Step-by-step curl commands: [2_upload/upload_multipart_curl.md](2_upload/upload_multipart_curl.md).

## 4. Run Activity Detection Jobs

Two independent jobs — one per input file:

### Home-level job (1 prompt → 1 narrative)

```bash
python 3_batch_jobs/create_activity_detection_job.py --file ghost_iot_home_yesterday.jsonl
# or: ./3_batch_jobs/create_activity_detection_job.sh ghost_iot_home_yesterday.jsonl
```

### Device-level job (N prompts → N narratives)

```bash
python 3_batch_jobs/create_activity_detection_job.py --file ghost_iot_devices_yesterday.jsonl
# or: ./3_batch_jobs/create_activity_detection_job.sh ghost_iot_devices_yesterday.jsonl
```

Both jobs use:

```yaml
pipeline_key: activity-detection
parameters:
  worker:
    parallelism: 1
    config:
      generation:
        do_sample: true
        max_new_tokens: 256
        repetition_penalty: 1
        temperature: 0.7
        top_k: 20
        top_p: 0.8
```

The script prints the `job_id`, polls until `COMPLETED`/`FAILED`/`CANCELLED`, and dumps the event log. curl walkthrough: [3_batch_jobs/create_activity_detection_job_curl.md](3_batch_jobs/create_activity_detection_job_curl.md).

## 5. Download Outputs

Use the `job_id` printed by the create step:

```bash
python 4_download_outputs/download_outputs.py <home_job_id>   outputs/home
python 4_download_outputs/download_outputs.py <device_job_id> outputs/devices
```

Each output line is:
```json
{"line_index": 0, "prediction": "On 2019-10-19 the home's wlan0 interface saw..."}
```

curl walkthrough: [4_download_outputs/download_outputs_curl.md](4_download_outputs/download_outputs_curl.md).

## 6. View the Narratives

```bash
# Home — one paragraph
python 5_view_results/view_results.py outputs/home

# Devices — one paragraph per device, labeled by MAC
python 5_view_results/view_results.py outputs/devices --input data/ghost_iot_devices_yesterday.jsonl

# To also print the prompt that produced each narrative
python 5_view_results/view_results.py outputs/devices --input data/ghost_iot_devices_yesterday.jsonl --show-prompt
```

## Why this design

**One home-level prompt vs. hourly chunks.** A single daily prompt produces a compact story. Chunking by hour would yield 24 paragraphs with more temporal detail — easy to switch to by changing the prepare script if desired.

**MAC only, no device-type labels.** MACs are anonymized hex and we don't have a device directory, so we give Newton the MAC plus the device's protocol/port/traffic profile and let it infer the device type (phone, laptop, smart speaker, etc.) from the evidence. Whether it guesses correctly is part of what this demo shows.

**Raw flow log in `inputs`, no pre-aggregation.** We deliberately don't pre-compute protocol percentages, peak hour, or most-active device. Newton gets the raw flows via `inputs[0].data` and writes the analysis. The `prompt` carries only the question context (date, scope, flow count). This reflects what a customer can realistically do (filter + serialize is 20 lines of code; computing "most active device by bytes" is not what they want to be maintaining) and makes the demo's output genuinely Newton's work, not a paraphrase of ours.

**Empty-MAC rows dropped.** A small number of flows in the source CSV have an empty `mac_a` or `mac_b`. The prepare scripts drop these so you don't get a phantom "blank" device in the device-level output.

## Data Attribution

GHOST-IoT dataset © the GHOST consortium, released for research use alongside:

> Anagnostopoulos, M.; Spathoulas, G.; Viaño, B.; Augusto-Gonzalez, J. *Tracing Your Smart-Home Devices Conversations: A Real World IoT Traffic Data-Set.* Sensors **2020**, 20, 6600. [DOI:10.3390/s20226600](https://doi.org/10.3390/s20226600)

## API Reference

### Files API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v0.5/files/uploads/initiate` | POST | Initiate upload, get presigned URLs |
| `{presigned_url}` | PUT | Upload part directly to S3 |
| `/v0.5/files/uploads/{upload_id}/complete` | POST | Finalize upload with ETags |
| `/v0.5/files/uploads/{upload_id}/abort` | POST | Cancel in-progress upload |

### Batch Jobs API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v0.5/batch/jobs` | POST | Create a batch job |
| `/v0.5/batch/jobs/{job_id}` | GET | Get job status |
| `/v0.5/batch/jobs/{job_id}/events` | GET | Get job events/logs |
| `/v0.5/batch/jobs/{job_id}/outputs` | GET | List output artifacts (paginated, presigned URLs) |

## License

Apache 2.0 (code). Dataset under its original GHOST-IoT license — see attribution above.
