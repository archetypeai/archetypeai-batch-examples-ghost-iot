# Archetype AI Batch Examples — GHOST-IoT (Activity Detection)

An end-to-end example of running Newton's **Activity Detection** batch pipeline against smart-home WiFi flow data to answer two questions:

1. **What happened at the home level yesterday?** — one daily summary narrative across the whole home.
2. **What happened at the device level yesterday?** — one narrative per active device.

Modeled on [archetypeai-batch-examples-volve](https://github.com/archetypeai/archetypeai-batch-examples-volve), this repo uses **only the Activity Detection pipeline** (no Machine State classification, no n-shot examples, no evaluation — Activity Detection outputs are natural language).

## Pipeline

**Simple pattern** — one JSONL record carries the whole filtered day. Works for small inputs (hundreds of flows). Used in sections 2–6 of this README.

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

**Multi-home batch pattern** — required for GB-scale inputs and multi-tenant deployments (multiple homes, humans per home, devices per human). Streaming dynamic chunking preserves every flow; **one record per file** (≤10 KB) so every upload is an independent inference unit, mirroring [wifi-multi's `/query` design](https://github.com/archetypeai/archetypeai-query-examples-wifi-multi/#constraints-driving-the-design). Six batch jobs (chunk → bucket-A → bucket-B → device-day → user-day + house-day) fold the chunks back into per-device, per-user, and per-house daily narratives. Used in [section 8](#8-multi-home-batch-with-many-small-files).

```
multi-home CSV
   │ prepare_per_device_hour_jsonls.py   (dynamic chunking, ≤10 KB / chunk, no sampling)
   ▼
~37K single-record JSONLs in data/per_device_hour/  + sidecar manifest
   │ upload_directory.py --concurrency 8
   ▼
~37K file_ids → Activity Detection batch job (worker.data = ~37K file_ids)
   │ extract_predictions.py
   ▼
~37K chunk-slice narratives
   │ prepare_bucket_reduce.py --stage a   (group ≤20 chunks per pack)
   ▼
~1700 partial narratives → batch job
   │ prepare_bucket_reduce.py --stage b   (1 record per (device, hour))
   ▼
576 device-hour narratives → batch job
   │ prepare_device_day_reduce.py
   ▼
24 device-day narratives → batch job
   ├─► prepare_user_day_reduce.py  ─►  6 user-day narratives
   └─► prepare_house_day_reduce.py ─►  3 house-day narratives
```

| Step | Script | Description |
|------|--------|-------------|
| Prepare (simple) | `1_prepare_data/prepare_{home,device}_level_jsonl.py` | Build one-record-per-scope JSONL |
| Generate (multihome) | `1_prepare_data/generate_synthetic_multihome_csv.py` | Synthesize multi-home CSV from `data/topology.json` |
| Chunk prep | `1_prepare_data/prepare_per_device_hour_jsonls.py` | Greedy size-based chunking — every flow preserved, one chunk per single-record JSONL file (~37K files at 1 GB) |
| Bucket reduce | `1_prepare_data/prepare_bucket_reduce.py --stage {a,b}` | Two-pass fold: chunks → partials → 1 narrative per (device, hour) |
| Device/user/house reduce | `1_prepare_data/prepare_{device,user,house}_day_reduce.py` | Fold device-hour → device-day → {user-day, house-day} |
| Upload (single) | `2_upload/upload_multipart.py` | Multipart presigned-URL upload, one file |
| Upload (directory) | `2_upload/upload_directory.py` | Concurrent bulk-upload of a directory of JSONLs, idempotent |
| Batch job | `3_batch_jobs/create_activity_detection_job.py` | Create & monitor; supports `--file` or `--file-list` |
| Download | `4_download_outputs/download_outputs.py` | Paginated download of prediction files |
| Join predictions | `4_download_outputs/extract_predictions.py` | Map output filenames back to input file_ids via upload manifest |
| View | `5_view_results/view_results.py` | Pretty-print outputs; `--manifest` mode labels by topology scope |

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

## 7. Scale Testing with Synthetic Data

The included GHOST-IoT CSV is tiny (600 KB). To stress test the end-to-end pipeline (upload → job → download) at realistic sizes, use the synthetic generator to produce 1 GB, 10 GB, 100 GB, or 200 GB WiFi flow CSVs.

### Generator

`1_prepare_data/generate_synthetic_csv.py` samples rows from the real GHOST-IoT CSV with replacement, randomizes their timestamps to fall within the target UTC day (default `2019-10-19`), and optionally jitters byte/packet counts (±20%) to avoid exact duplicates. Output preserves the source schema so all downstream scripts work unchanged.

Before running, the script prints a disk-space pre-flight check and an estimated runtime (~150k rows/sec on a typical SSD).

```bash
# 1 GB  (~5.5M rows, ~40 seconds)
python 1_prepare_data/generate_synthetic_csv.py \
  --target-size-gb 1 \
  --output data/wlan0_ipv4_flows_1gb.csv

# 10 GB  (~55M rows, ~6 minutes)
python 1_prepare_data/generate_synthetic_csv.py \
  --target-size-gb 10 \
  --output data/wlan0_ipv4_flows_10gb.csv

# 100 GB  (~550M rows, ~1 hour)
python 1_prepare_data/generate_synthetic_csv.py \
  --target-size-gb 100 \
  --output data/wlan0_ipv4_flows_100gb.csv

# 200 GB  (~1.1B rows, ~2 hours; requires ≥200 GB free disk)
python 1_prepare_data/generate_synthetic_csv.py \
  --target-size-gb 200 \
  --output data/wlan0_ipv4_flows_200gb.csv
```

Other useful flags: `--target-size-mb`, `--rows N` (exact row count), `--date YYYY-MM-DD`, `--seed N`, `--no-jitter`.

All `data/wlan0_ipv4_flows_*gb.csv` / `*_mb.csv` files are `.gitignore`d so they won't accidentally be committed.

### Running the prep scripts at scale

Both `prepare_home_level_jsonl.py` and `prepare_device_level_jsonl.py` stream the source CSV — memory is constant regardless of input size. Use `--input` to point them at the synthetic file:

```bash
# Home-level (1 JSONL line containing all filtered flows)
python 1_prepare_data/prepare_home_level_jsonl.py \
  --input data/wlan0_ipv4_flows_1gb.csv \
  --output data/ghost_iot_home_1gb.jsonl

# Device-level (1 JSONL line per active device)
python 1_prepare_data/prepare_device_level_jsonl.py \
  --input data/wlan0_ipv4_flows_1gb.csv \
  --output data/ghost_iot_devices_1gb.jsonl
```

Observed at 1 GB input: ~25 seconds per script, 5.8M flows on the target day, output JSONL ≈ 364 MB (home) / 729 MB (device).

### End-to-end example at 1 GB (uncapped)

The full stress-test flow for the 1 GB synthetic CSV, producing the uncapped `ghost_iot_home_1gb.jsonl` and `ghost_iot_devices_1gb.jsonl` and pushing them all the way through the platform:

```bash
# 1. Generate the 1 GB source CSV (~40s on SSD)
python 1_prepare_data/generate_synthetic_csv.py \
  --target-size-gb 1 \
  --output data/wlan0_ipv4_flows_1gb.csv

# 2. Build the two JSONL files (streaming prep, ~25s each)
python 1_prepare_data/prepare_home_level_jsonl.py \
  --input data/wlan0_ipv4_flows_1gb.csv \
  --output data/ghost_iot_home_1gb.jsonl
python 1_prepare_data/prepare_device_level_jsonl.py \
  --input data/wlan0_ipv4_flows_1gb.csv \
  --output data/ghost_iot_devices_1gb.jsonl

# 3. Upload (multipart kicks in automatically for files > ~5 MB)
python 2_upload/upload_multipart.py data/ghost_iot_home_1gb.jsonl
python 2_upload/upload_multipart.py data/ghost_iot_devices_1gb.jsonl

# 4. Create Activity Detection jobs (each references the uploaded filename)
python 3_batch_jobs/create_activity_detection_job.py \
  --file ghost_iot_home_1gb.jsonl \
  --name ghost-iot-home-1gb
python 3_batch_jobs/create_activity_detection_job.py \
  --file ghost_iot_devices_1gb.jsonl \
  --name ghost-iot-devices-1gb
# Save the two job_ids printed by these commands.

# 5. Download outputs
python 4_download_outputs/download_outputs.py <home_job_id>   outputs/ghost-iot-home-1gb
python 4_download_outputs/download_outputs.py <device_job_id> outputs/ghost-iot-devices-1gb

# 6. View results
python 5_view_results/view_results.py outputs/ghost-iot-home-1gb
python 5_view_results/view_results.py outputs/ghost-iot-devices-1gb \
  --input data/ghost_iot_devices_1gb.jsonl
```

#### Observed failure at 1 GB uncapped (run of `ghost_iot_home_1gb.jsonl`)

The home-level run with the uncapped 376 MB `inputs[0].data` fails fast. The platform emits an explicit pre-flight truncation warning, surfaces a pod-level OOM, and reports `Status: FAILED` in ~5 minutes:

```
[3/3] Job events:
  [01:18:46] INFO    Initializing engine & loading model
  [01:21:01] INFO    Model loaded & engine initialized
  [01:21:02] INFO    Processing input 0: ghost_iot_home_1gb.jsonl (item count unknown)
  [01:21:09] WARN    Batch 0: 1 items exceed token_budget=16,384, 0 items within 5%
                     of it (max_new_tokens=1,024). Truncation drops text and the
                     generation prompt suffix first, so the model may receive an
                     incomplete prompt and produce garbage output. Shorten the
                     prompt, reduce text input variables, or use fewer/shorter
                     media inputs.
  [01:23:23] ERROR   Container worker terminated: OOMKilled (exit=137)
  [01:23:36] FAILED  Job failed: BackoffLimitExceeded: Job has reached the
                     specified backoff limit

 Status: FAILED   (~5 minutes wall clock)
```

Three things are useful in this output:

1. **Named token budget.** `token_budget=16,384` — the per-record context window in tokens, surfaced explicitly. This is the same number the §8.2 binary search converges on (~150 GHOST-IoT flow rows ≈ ~10 KB ≈ ~2.5K tokens, with the rest of the budget eaten by activation memory and pre-allocated batch overhead).
2. **Pre-flight warning, not silent.** The warning fires *before* generation starts, naming the specific record(s) that exceed the budget. You can fail fast on this signal.
3. **Honest terminal status.** `Status: FAILED` matches reality.

**Takeaway for GTM:** you still cannot fully trust `Status: COMPLETED` in every scenario — there are still failure paths (CUDA OOM batch-halving with empty per-record output, see below) where the top-level status hides per-record failure. Always read the events log and inspect at least the first prediction's text. The client is responsible for keeping `inputs[0].data` within the per-record token budget.

#### Observed failure at 1 GB uncapped (run of `ghost_iot_devices_1gb.jsonl`)

The device-level run with the uncapped 729 MB JSONL (10 records, up to 310 MB each in `inputs[0].data`) fails identically to the home-level run, but with a more informative warning: the platform names the count of over-budget records.

```
[3/3] Job events:
  [01:25:39] INFO    Initializing engine & loading model
  [01:27:54] INFO    Model loaded & engine initialized
  [01:27:55] INFO    Processing input 0: ghost_iot_devices_1gb.jsonl (item count unknown)
  [01:28:11] WARN    Batch 0: 6 items exceed token_budget=16,384, 0 items within 5%
                     of it (max_new_tokens=1,024). Truncation drops text and the
                     generation prompt suffix first, so the model may receive an
                     incomplete prompt and produce garbage output. Shorten the
                     prompt, reduce text input variables, or use fewer/shorter
                     media inputs.
  [01:29:23] ERROR   Container worker terminated: OOMKilled (exit=137)
  [01:29:40] FAILED  Job failed: BackoffLimitExceeded: Job has reached the
                     specified backoff limit

 Status: FAILED   (~5 minutes wall clock)
```

**`6 items exceed token_budget=16,384`** — the platform identifies how many of the 10 input records are over the per-record context budget. (The remaining 4 are smaller devices whose flow logs happen to fit; they don't save the run since the entire pod is OOM-terminated.)

#### Summary of observed failure modes at 1 GB uncapped

| Scenario | Records × per-record size | What happens | Wall clock |
|---|---|---|---|
| `ghost_iot_home_1gb.jsonl` | 1 × 376 MB | `WARN inference.truncation` (1 item over budget) → `OOMKilled` → `Status: FAILED` | ~5 min |
| `ghost_iot_devices_1gb.jsonl` | 10 × ~1–310 MB | `WARN inference.truncation` (6 items over budget) → `OOMKilled` → `Status: FAILED` | ~5 min |

Both are clean fast-fail with diagnostic detail. For a working run on the same 1 GB input, regenerate with `--max-flows` / `--max-flows-per-device` (see next subsection) and repeat upload/job/download.

### Capping per-record flow counts for Newton

The uncapped JSONL scales linearly with CSV size. At 1 GB input a single home-level `inputs[0].data` already contains 376 MB of flow text (~100M tokens) — far beyond any model context window. Newton's Activity Detection pipeline will very likely truncate, error, or time out on uncapped JSONL at 1 GB+.

To produce JSONL the model can actually process, cap the per-record flow count:

```bash
# ~5000 flows → ~100-300 KB per record, well within context limits
python 1_prepare_data/prepare_home_level_jsonl.py \
  --input data/wlan0_ipv4_flows_10gb.csv \
  --output data/ghost_iot_home_10gb_capped.jsonl \
  --max-flows 5000

python 1_prepare_data/prepare_device_level_jsonl.py \
  --input data/wlan0_ipv4_flows_10gb.csv \
  --output data/ghost_iot_devices_10gb_capped.jsonl \
  --max-flows-per-device 5000
```

The cap takes the first N matching flows. The scripts still scan the whole CSV (so flow counts in the prompt are accurate), but only the first N rows per record end up in `inputs[0].data`.

### What you're actually testing

| Layer | 1 GB | 10 GB | 100 GB | 200 GB |
|---|---|---|---|---|
| CSV generation | ✓ | ✓ | ✓ | ✓ (needs ≥200 GB free disk) |
| Streaming prep (memory) | ✓ | ✓ | ✓ | ✓ |
| Multipart upload (`2_upload/`) | ✓ | ✓ | ✓ | ⚠ close to 250 GB platform limit |
| Activity Detection, 1-record uncapped | ✗ | ✗ | ✗ | ✗ |
| Activity Detection, N-record uncapped | ✗ | ✗ | ✗ | ✗ |
| Activity Detection (`--max-flows 5000`) | ✓ | ✓ | ✓ | ✓ |

**Both uncapped scenarios fail fast at 1 GB.** Modern pipeline emits `WARN inference.truncation` naming the per-record token budget (`16,384`) and the count of over-budget records, then `OOMKilled` and `Status: FAILED` in ~5 minutes. See the "Observed failure" subsections above for the detailed event logs. You MUST cap `inputs[0].data` client-side for any run that exceeds the model's per-record context window — section 8's dynamic chunking does this automatically.

For a pure **upload stress test**, use the uncapped JSONL (the upload pipeline itself is fine — the file lands on the platform intact). For **end-to-end including inference**, use the capped version.

### Disk-space planning

Running all four targets back-to-back without cleanup needs 311 GB of free space (1 + 10 + 100 + 200). If disk is tight, generate → upload → delete → move to the next target. The generator refuses to start if the target size would exceed 90% of free disk.

## 8. Multi-home batch with many small files

The single-record approach in sections 2–6 fits in context only for the small reference dataset (a few hundred flows). At GB scale, Activity Detection's per-record context budget (~150 flows / ~10 KB / ~2.5K tokens — see [section 7](#7-scale-testing-with-synthetic-data) for the observed failure modes, and §8.2 below for the binary search that produced this number) is far smaller than the day's flow log, so a single record carrying everything either silently truncates or OOMs.

This section's **multi-home batch pattern** solves both problems at once:

- **Context fit:** chunk the input dynamically — for each `(device, hour)` bucket, pack flow rows into chunks until each chunk hits the byte budget, then close it and start a new chunk. **No flows are dropped** — high-volume buckets simply produce more chunks. At 1 GB the chunk batch processes ~38K independent inferences across 576 multi-record JSONL files.
- **Multi-tenant scope:** model a realistic deployment with multiple homes, multiple humans per home, and a mix of personal and shared devices. Five follow-up reduce jobs (bucket-A → bucket-B → device-day → user-day + house-day) fold the chunks back into per-device, per-user, and per-house daily summaries.

The pattern is adapted from the [wifi-multi `/query` demo](https://github.com/archetypeai/archetypeai-query-examples-wifi-multi) — same topology, same prompt structure — but rebuilt around the **batch jobs + Files API** instead of the synchronous `/query` endpoint. The batch pattern's selling point: independent of source CSV size (1 MB, 1 GB, or 200 GB), the prep step always emits 576 files, each fitting Activity Detection's per-record context budget.

### Pipeline (DAG)

Every flow in the source CSV is processed — no sampling. Buckets that exceed
the per-record context budget are split into multiple chunks, then folded
back to one narrative per `(device, hour)` via a two-pass bucket reduce.

```
1 GB multi-home CSV (5.8M flows, 24 devices × 24 hours of data)
         │
         ▼  prepare_per_device_hour_jsonls.py  (dynamic chunking, ≤10 KB / chunk)
~37K single-record JSONLs in data/per_device_hour/  (one chunk per file)
+ data/manifest_chunked.jsonl  (one entry per chunk file)
         │
         ▼  upload_directory.py --concurrency 8 → Files API
~37K file_ids → Activity Detection batch job (worker.data = ~37K file_ids)
         │
         ▼  ~37K chunk-slice narratives, downloaded + joined by extract_predictions.py
data/predictions_chunked.jsonl
         │
         ▼  prepare_bucket_reduce.py --stage a   (group chunks → ≤20 chunks per pack)
data/bucket_reduce_a.jsonl  (~1700 records at 1 GB) → batch job
         │
         ▼  prepare_bucket_reduce.py --stage b   (1 record per (device, hour))
data/bucket_reduce_b.jsonl  (576 records) → batch job
         │
         ▼  576 device-hour narratives + manifest_per_device_hour.jsonl
         │
         ▼  prepare_device_day_reduce.py  (group by device)
1 JSONL × 24 records → batch job → 24 device-day narratives
         │
         ├─►  prepare_user_day_reduce.py   (group by human, personal devices only)
         │    1 JSONL × 6 records → batch job → 6 user-day narratives
         │
         └─►  prepare_house_day_reduce.py  (group by home, all devices)
              1 JSONL × 3 records → batch job → 3 house-day narratives
```

### Topology

The cast — 3 homes, 6 humans, 24 devices — lives in `data/topology.json`. Each human owns 3 personal devices (phone, laptop, watch); each home has 2 shared devices (smart speaker, thermostat) attributed to nobody. `1_prepare_data/topology.py` parses it into typed objects every section-9 script imports.

```
Home A (gateway aa1100000001)        Home B (gateway bb2200000001)        Home C (gateway cc3300000001)
  Alice    : phone, laptop, watch      Carol    : phone, laptop, watch      Eve      : phone, laptop, watch
  Bob      : phone, laptop, watch      Dan      : phone, laptop, watch      Frank    : phone, laptop, watch
  shared   : smart_speaker, thermostat shared   : smart_speaker, thermostat shared   : smart_speaker, thermostat
```

User-day reductions only fold a human's **personal** devices. Shared devices show up in house-day only.

### 8.1 Generate the multi-home synthetic CSV

```bash
python 1_prepare_data/generate_synthetic_multihome_csv.py --target-size-gb 1.0 \
  --output data/wifi_flows_multihome_1gb.csv
```

Each output row inherits its protocol/port/byte distribution from a randomly chosen GHOST-IoT seed row, but `mac_a` is replaced with a topology device MAC and `mac_b` with that device's home gateway MAC. `ts_start` is sampled from a per-device-type hour profile (phones bursty in evenings, laptops sustained midday, watches sparse all day, etc.) so each device's day looks plausible.

Per-device row counts are allocated proportionally — phones and laptops dominate, thermostats are sparse — and total to the requested target size.

### 8.2 Build per-chunk JSONL files with dynamic chunking

```bash
python 1_prepare_data/prepare_per_device_hour_jsonls.py \
  --input data/wifi_flows_multihome_1gb.csv \
  --output-dir data/per_device_hour \
  --manifest data/manifest_chunked.jsonl
```

The prep script streams the CSV, buckets flows by `(device_mac, hour_utc)`, and packs each bucket's flows into chunks using **greedy size-based fill**: append flow rows to a chunk until the chunk's flow-log payload approaches `--max-chunk-bytes` (default 10 KB ≈ 150 GHOST-IoT-formatted flow rows ≈ 2.5K tokens), then close the chunk and write it to **its own single-record JSONL file**. **No flows are dropped — every row in the source ends up in exactly one chunk.** A 12,345-flow bucket produces ~83 separate JSONL files (chunks 0–82), each ≤10 KB.

**Why one record per file, not many records per file?** The platform's batch worker treats each input file as an independent inference unit. Multi-record JSONLs trigger CUDA OOM with batch-size bisection that cannot recover (observed at 2 MB / 196 records, where `bs=16→8→4→2→1` all OOMed and the pod was crashed for restart). Mirroring [wifi-multi's `/query` design](https://github.com/archetypeai/archetypeai-query-examples-wifi-multi/#constraints-driving-the-design) — one independent unit per call — sidesteps the issue.

Outputs:

| File / dir | Contents |
|---|---|
| `data/per_device_hour/dev_<home_id>__<device_id>__hHH__cNNNN.jsonl` (~37K at 1 GB) | Single-record JSONL — one chunk per file, ≤10 KB each |
| `data/manifest_chunked.jsonl` (~37K lines at 1 GB) | Sidecar: one entry per chunk file, mapping `file_id` → `{home_id, human, device_id, mac, hour_utc, chunk_index, n_flows, n_bytes, ts_start_min, ts_start_max, ...}` |

File count scales linearly with source size: 1 GB ≈ 37K files, 10 GB ≈ 370K, etc. The 576-bucket grid is fixed by topology (24 devices × 24 hours), but each bucket's chunk count varies with that device-hour's flow volume.

Memory: the script keeps 576 in-flight chunk buffers (each ≤ `max_chunk_bytes`) ≈ ~6 MB RAM regardless of source size. Streaming on the input side, so any CSV size works. Each chunk file is opened, written in one shot, and closed — no concurrent file-handle pressure.

#### Why the per-chunk byte cap is 10 KB (observed)

Activity Detection advertises a **16K-token model context window** (`token_budget=16,384` per the platform's `WARN inference.truncation` event when records exceed it — see §10.2). The empirical per-record GPU-memory ceiling is tighter due to activation memory and pre-allocated batch overhead. Earlier binary search at 1 GB on a single-home hourly pipeline produced:

| Per-record flow count | Per-record ~tokens | ~Bytes | Result |
|---|---|---|---|
| 5000 | ~80K | ~340 KB | FAILED — pre-flight `WARN inference.truncation` + OOMKilled |
| 1000 | ~16K | ~68 KB | FAILED — same |
| 500 | ~8K | ~34 KB | FAILED — same |
| 300 | ~5K | ~20 KB | FAILED — same |
| 200 | ~3.3K | ~14 KB | FAILED — same |
| **150** | **~2.5K** | **~10 KB** | **✓** (single-record-per-file) |
| 100 | ~1.6K | ~7 KB | ✓ |

GHOST-IoT-formatted flow rows are ~65-70 bytes each (tighter than the per-row averages of typical CSV formats), so 150 flows ≈ 10 KB ≈ 2.5K tokens. The 16K context budget is *gross* of fixed activation overhead, pre-allocated max-batch-size memory, and other runtime allocations. The default `--max-chunk-bytes 10240` matches the empirical safe ceiling.

**Multi-record-per-file makes this worse.** A separate UI test uploaded a 196-record JSONL (each record ~10 KB, file total 2 MB) and the platform OOMed during batch processing — bisected from `bs=16` all the way down to `bs=1` and still OOMed. The platform's pre-allocated batch memory plus per-record activations exceeded GPU budget even when each individual record was within the 10 KB safe cap. **Conclusion: keep one record per file.** The current prep script enforces this.

**Don't raise `--max-chunk-bytes` past 10 KB without re-running the binary search.**

### 8.3 Upload all chunk files (concurrent)

```bash
python 2_upload/upload_directory.py data/per_device_hour --concurrency 8
# manifest goes to data/uploaded_file_ids.txt by default
# sibling data/uploaded_file_ids.jsonl records {filename, file_uid} for the post-download join
```

37K sequential uploads at ~1–2 s each would be 10–20 hours, so the script uploads in parallel via a thread pool. **Default `--concurrency 8`** brings 1 GB down to ~1–2 hours; bump higher (16, 32) if the platform tolerates it. Idempotent: if the manifest already lists a filename, that file is skipped. Re-run after a partial network failure (or ctrl-C) to pick up where it left off.

### 8.4 Run the main chunk batch job

```bash
python 3_batch_jobs/create_activity_detection_job.py \
  --file-list data/uploaded_file_ids.txt \
  --name multihome-chunked
```

`worker.data` is a ~37,000-element array of `{"file_id": <filename>}` at 1 GB, with each input file containing exactly one chunk record. The platform processes each file as an independent inference. **Caveat:** if the `/v0.5/batch/jobs` endpoint enforces a request-size limit, you may need to split the manifest into multiple jobs (e.g. four sub-jobs of ~9K files each) — the current code submits everything in one call.

### 8.5 Download and join chunk predictions

```bash
python 4_download_outputs/download_outputs.py <main_job_id> outputs/multihome-chunked
python 4_download_outputs/extract_predictions.py outputs/multihome-chunked \
  --upload-manifest data/uploaded_file_ids.jsonl \
  --output data/predictions_chunked.jsonl
```

`extract_predictions.py` parses each `inp_<HASH>_output.jsonl` filename, looks `<HASH>` up in the upload manifest's `file_uid` column, and emits one record per `(file_id, line_index)` pair — preserving the chunk-index ordering. ~38K predictions at 1 GB.

### 8.6 Bucket reduce stage A — group chunks into per-bucket partials

```bash
python 1_prepare_data/prepare_bucket_reduce.py --stage a \
  --predictions data/predictions_chunked.jsonl \
  --manifest data/manifest_chunked.jsonl \
  --output data/bucket_reduce_a.jsonl \
  --output-manifest data/manifest_bucket_reduce_a.jsonl

python 2_upload/upload_multipart.py data/bucket_reduce_a.jsonl
python 3_batch_jobs/create_activity_detection_job.py \
  --file bucket_reduce_a.jsonl \
  --name multihome-bucket-reduce-a \
  --max-new-tokens 1024

python 4_download_outputs/download_outputs.py <stage_a_job_id> outputs/multihome-bucket-reduce-a
python 4_download_outputs/extract_predictions.py outputs/multihome-bucket-reduce-a \
  --upload-manifest data/uploaded_file_ids.jsonl \
  --output data/predictions_bucket_reduce_a.jsonl
```

For each `(device, hour)` bucket, Stage A groups the bucket's chunks into packs of up to 20 (configurable via `--group-size`) and emits one reduce record per group. At 1 GB this produces ~1700 records — average 3 partials per bucket (busy buckets have more, low-volume buckets have just 1). Each pack stays comfortably under the C model's ~21 KB narrative-heavy ceiling.

### 8.7 Bucket reduce stage B — fold partials into one narrative per bucket

```bash
python 1_prepare_data/prepare_bucket_reduce.py --stage b \
  --predictions data/predictions_bucket_reduce_a.jsonl \
  --manifest data/manifest_bucket_reduce_a.jsonl \
  --output data/bucket_reduce_b.jsonl \
  --output-manifest data/manifest_per_device_hour.jsonl

python 2_upload/upload_multipart.py data/bucket_reduce_b.jsonl
python 3_batch_jobs/create_activity_detection_job.py \
  --file bucket_reduce_b.jsonl \
  --name multihome-bucket-reduce-b \
  --max-new-tokens 1024

python 4_download_outputs/download_outputs.py <stage_b_job_id> outputs/multihome-bucket-reduce-b
python 4_download_outputs/extract_predictions.py outputs/multihome-bucket-reduce-b \
  --upload-manifest data/uploaded_file_ids.jsonl \
  --output data/predictions_per_device_hour.jsonl
```

Stage B always emits exactly **576 reduce records** — one per `(device, hour)` bucket — folding that bucket's Stage-A partials into a single device-hour narrative. The output sidecar `manifest_per_device_hour.jsonl` matches the shape the downstream device-day reduce script expects.

### 8.8 Reduce stage 3: device-day (576 → 24)

```bash
python 1_prepare_data/prepare_device_day_reduce.py \
  --predictions data/predictions_per_device_hour.jsonl \
  --manifest data/manifest_per_device_hour.jsonl \
  --output data/device_day_reduce.jsonl \
  --output-manifest data/manifest_device_day.jsonl

python 2_upload/upload_multipart.py data/device_day_reduce.jsonl
python 3_batch_jobs/create_activity_detection_job.py \
  --file device_day_reduce.jsonl \
  --name multihome-device-day \
  --max-new-tokens 2048

python 4_download_outputs/download_outputs.py <device_day_job_id> outputs/multihome-device-day
python 4_download_outputs/extract_predictions.py outputs/multihome-device-day \
  --upload-manifest data/uploaded_file_ids.jsonl \
  --output data/predictions_device_day.jsonl
```

Each of the 24 reduce records concatenates that device's 24 hourly narratives in a `=== HOURLY SUMMARIES (24) FOR <device> IN <home> ON <date> ===` envelope, ending with the synthesis instruction. Pack size ≈ 12 KB per record — well under the C model's ~21 KB narrative-heavy ceiling.

### 8.9 Reduce stages 4a/4b: user-day and house-day (24 → {6, 3})

Both consume the device-day predictions and run as independent batch jobs:

```bash
# User-day (personal devices only)
python 1_prepare_data/prepare_user_day_reduce.py \
  --predictions data/predictions_device_day.jsonl \
  --manifest data/manifest_device_day.jsonl \
  --output data/user_day_reduce.jsonl \
  --output-manifest data/manifest_user_day.jsonl

# House-day (every device — personal + shared)
python 1_prepare_data/prepare_house_day_reduce.py \
  --predictions data/predictions_device_day.jsonl \
  --manifest data/manifest_device_day.jsonl \
  --output data/house_day_reduce.jsonl \
  --output-manifest data/manifest_house_day.jsonl

python 2_upload/upload_multipart.py data/user_day_reduce.jsonl
python 2_upload/upload_multipart.py data/house_day_reduce.jsonl
python 3_batch_jobs/create_activity_detection_job.py --file user_day_reduce.jsonl  --name multihome-user-day  --max-new-tokens 2048
python 3_batch_jobs/create_activity_detection_job.py --file house_day_reduce.jsonl --name multihome-house-day --max-new-tokens 2048
```

User-day pack size ≈ 3 KB (3 device-day narratives per human), house-day ≈ 8 KB (8 device-day narratives per home) — both fit in C-model context with margin.

### 8.10 View results

```bash
python 4_download_outputs/download_outputs.py <user_day_job_id>  outputs/multihome-user-day
python 4_download_outputs/download_outputs.py <house_day_job_id> outputs/multihome-house-day

# Hourly — labeled by (home, human, device, hour)
python 5_view_results/view_results.py data/predictions_per_device_hour.jsonl \
  --manifest data/manifest_per_device_hour.jsonl

# Device-day, user-day, house-day — pretty-printed in topology order
python 5_view_results/view_results.py outputs/multihome-device-day --manifest data/manifest_device_day.jsonl
python 5_view_results/view_results.py outputs/multihome-user-day  --manifest data/manifest_user_day.jsonl
python 5_view_results/view_results.py outputs/multihome-house-day --manifest data/manifest_house_day.jsonl
```

### Why this processes every flow

| Stage | Records at 1 GB | Per-record size | Bound by |
|---|---|---|---|
| Chunk inference | ~38,700 | ≤10 KB | `--max-chunk-bytes` |
| Bucket reduce A (intra-bucket) | ~1,700 | ≤20 KB | `--group-size` × per-chunk narrative length |
| Bucket reduce B (per-bucket) | 576 | ~3-8 KB | partial narrative length × group_count |
| Device-day reduce | 24 | ~12 KB | 24 × hourly narrative length |
| User-day reduce | 6 | ~3 KB | 3 × device-day narrative length |
| House-day reduce | 3 | ~8 KB | 8 × device-day narrative length |

Chunk count scales linearly with source size: at 1 GB ≈ 5.8M flows / 150 flows-per-chunk ≈ 38K chunks; at 10 GB ≈ 380K. The pipeline's *structure* (576 device-hour buckets, 24 device-day records, 6+3 user/house-day records) stays identical at any scale — only stage 1's chunk count grows.

**No sampling, no truncation:** every flow in the source CSV ends up in exactly one chunk; chunk narratives are folded losslessly through Stages A/B into one device-hour narrative. Stages 3-4 then summarize across hours, devices, users, and homes.

### Wall-clock at 1 GB (observed)

The end-to-end demo at 1 GB is **GPU-bound on the main chunk batch**. Numbers below come from an actual run against `pipeline: activity-detection v1.1.1-409-e749ac0` (May 2026, default `max_new_tokens=1024`):

| Stage | Records / files | Observed wall clock |
|---|---|---|
| Generate 1 GB CSV | — | ~40 s |
| Per-chunk prep | — | ~1–2 min (streaming, ~37K small file writes) |
| Upload 37K files (`--concurrency 8`) | 37,000 | ~1–2 hr |
| Main chunk batch (per sub-job) | 5,000 | **~41 hr** at ~29 s/step |
| Main chunk batch (8 sub-jobs at observed org concurrency=2) | 37,000 | **~7 days** |
| Bucket reduce A (upload + batch + download) | ~1,700 | ~10–15 hr |
| Bucket reduce B (upload + batch + download) | 576 | ~3–5 hr |
| Device-day prep + upload + batch + download | 24 | ~10–20 min |
| User-day + house-day (parallel) | 6 + 3 | ~5–10 min each |

**Two factors shape the chunk-batch wall clock**, and both vary by deployment:

1. **Per-step time = `max_new_tokens` × generation rate.** At `max_new_tokens=1024` the C model takes ~29 s/step; lowering to 256 drops it ~4× to ~7 s/step. For per-chunk slice narratives (bucket-reduce input), 256 tokens is usually sufficient — bump to 1024+ only for the final reduce stages where you want multi-paragraph output.

2. **Org-level batch-job concurrency cap.** This run observed 2 concurrent RUNNING jobs at any time; parts 003–008 stayed PENDING until earlier sub-jobs completed. With 8 sub-jobs and concurrency=2, the chunk batch serializes into 4 waves. If your org has more pods allocated, expect proportionally less wall-clock — at concurrency=8, total chunk-batch time drops from ~7 days to ~41 hours.

| Concurrency | 8 sub-jobs of ~5K files (1024 tokens) | with `--max-new-tokens 256` |
|---|---|---|
| 2 (observed) | ~7 days | **~40 hr** |
| 4 | ~3.5 days | ~20 hr |
| 8 | ~41 hr | **~10 hr** |

**Tuning levers** if you need to fit the demo in a tighter window:

- **Lower `--max-new-tokens` to 256** on the chunk batch (`create_activity_detection_job.py --max-new-tokens 256`). Bucket-reduce, device-day, user-day, and house-day reduces can keep 1024 or 2048 since they need richer output and run on far fewer records.
- **Run at smaller scale** (100 MB → ~3.7K files, ~10× shorter wall-clock at every stage).
- **Negotiate higher org concurrency** with your platform team.

**Beyond 1 GB:** both file count and chunk count grow linearly with source size, so 10 GB = ~370K files. Even at concurrency=8 with `max_new_tokens=256`, that's ~100 hours just for the main chunk batch. Past 1 GB the architecture works in principle but isn't practical without higher pod concurrency, batched-multi-job submission, or per-bucket pre-aggregation.

## 9. Cleanup between runs

A full section-8 run leaves several gigabytes of generated artifacts on disk — synthetic CSVs, ~38K chunk JSONLs, manifests, predictions, downloaded outputs. The Files API also keeps every uploaded filename indefinitely, so re-running the same pipeline trips a 409 Conflict on the second upload of any file with the same name. `cleanup.py` at the repo root handles both.

```bash
# Dry-run (default) — list everything that WOULD be deleted, touch nothing
python cleanup.py

# Local only — delete generated files in data/ and every outputs/<job>/ directory
python cleanup.py --local

# Local but keep generated CSVs (saves the ~40 s regen if you're iterating on prep)
python cleanup.py --local --keep-csv

# Platform side — attempt DELETE /v0.5/files/{file_uid} for everything in
# data/uploaded_file_ids.jsonl, falling back to /v0.5/files/{filename}
python cleanup.py --remote

# Full reset
python cleanup.py --local --remote
```

**Always preserved** — never touched regardless of flags:

- `data/wlan0_ipv4_flows_db.csv` (the original GHOST-IoT seed CSV)
- `data/topology.json`
- `data/ghost_iot_home_yesterday.jsonl`, `data/ghost_iot_devices_yesterday.jsonl` (small reference inputs for sections 2–6)

**Caveat on `--remote`:** the platform's DELETE endpoint isn't listed in this repo's API Reference. The script tries the most likely shapes (`/v0.5/files/{file_uid}`, then `/v0.5/files/{filename}`) and reports the response per file. If both 404, the script prints the failures and points at the workaround: rename your local files before re-uploading (e.g. append a session suffix), since the platform won't let you overwrite an existing filename.

**409 Conflict during upload.** If you don't run `--remote` cleanup, the next attempt to upload `ghost_iot_home_yesterday.jsonl` (or any other previously-uploaded filename) will return 409. Both upload scripts now handle this gracefully:

- `upload_multipart.py` prints "File already exists on the platform — skipping upload" and exits 0. You can reference the existing file directly in `create_activity_detection_job.py --file <name>`.
- `upload_directory.py` records the filename in the manifest with `file_uid=""` and continues. Note: any output whose `<HASH>` doesn't match a known `file_uid` will fail the post-download join in `extract_predictions.py` — for the section-8 chunk pipeline specifically, you generally want `--remote` cleanup or unique filenames before each run.

## 10. Common pitfalls

Failure modes seen during development of this demo. All of them stem from one underlying truth: **terminal `status: COMPLETED` from the batch jobs API does not mean processing succeeded.** The platform reports `COMPLETED` whenever the worker pod exits cleanly, even if the model produced nothing, the input was rejected, or every record OOMed. The only authoritative signal is the prediction text itself plus the events log — read both before trusting any run.

### 10.1 `COMPLETED` with no actual predictions (wrong content-type at upload)

**Symptom:**

```
[3/3] Job events:
  Processing input 0: ghost_iot_home_yesterday.jsonl (item count unknown)
  FAILED Processing failed for ghost_iot_home_yesterday.jsonl
  SUCCESS Job completed in 349s

 Status: COMPLETED
```

The job reports `COMPLETED` at the top level, but the events log contains a `FAILED Processing failed` line. Downloaded predictions are empty. (Note: `(item count unknown)` on its own is *not* a fault signal — the platform emits that line on every run before counting records. The fault signal is the absence of any subsequent `Batch N: processed M items` / `Processed M items` line, replaced instead by `FAILED Processing failed`.)

**Cause:** the file was uploaded with the wrong `file_type` (e.g. `text/csv` or `text/plain`) but its actual content is JSONL. The platform stores `file_type` on each file and uses it when batch jobs later read the file — pointing at a `.jsonl` file with `file_type=text/csv` makes the worker's record reader fail to parse the contents, which surfaces as `(item count unknown)` and an unrecoverable `Processing failed`. Compounding the trap: 409 Conflict on re-upload means you can't simply re-upload the same filename with the right content-type — the original wrong-type record stays.

**Fix:** the upload scripts now auto-detect content-type from extension. JSONL is newline-delimited JSON, which maps to **`application/x-ndjson`** — that's what the platform expects (it rejects `application/jsonl` with HTTP 400). Full mapping: `.jsonl/.ndjson → application/x-ndjson`, `.json → application/json`, `.csv → text/csv`, `.txt → text/plain`. The platform's supported `file_type` values are: `image/jpeg, image/png, video/mp4, text/csv, text/plain, application/json, application/x-ndjson`.

To recover a previously bad-typed upload, re-upload under a fresh filename:

```bash
cp data/ghost_iot_home_yesterday.jsonl data/ghost_iot_home_yesterday_v2.jsonl
python 2_upload/upload_multipart.py data/ghost_iot_home_yesterday_v2.jsonl
# the new upload uses application/jsonl automatically (auto-detected from .jsonl)
python 3_batch_jobs/create_activity_detection_job.py --file ghost_iot_home_yesterday_v2.jsonl
```

To override auto-detection (e.g. for an unusual extension), pass `--file-type application/jsonl` explicitly. The successful run will report `total_lines=1` (or the actual record count) and produce real prediction text.

### 10.2 `FAILED` — input over the per-record token budget

**Symptom:**

```
WARN   Batch 0: 1 items exceed token_budget=16,384, 0 items within 5% of it
       (max_new_tokens=1,024). Truncation drops text and the generation prompt
       suffix first, so the model may receive an incomplete prompt and produce
       garbage output.
ERROR  Container worker terminated: OOMKilled (exit=137)
FAILED Job failed: BackoffLimitExceeded
Status: FAILED
```

A single oversize record triggers an explicit pre-flight `WARN inference.truncation` event naming the budget, followed by pod-level OOMKilled and an honest `Status: FAILED` in ~5 minutes.

**Cause:** `inputs[0].data` exceeds the C model's effective per-record context window — the platform reports this directly as `token_budget=16,384`. In practice the empirical safe-cap is tighter (~2.5K tokens / ~10 KB / ~150 GHOST-IoT flow rows) because activation memory and pre-allocated batch overhead consume part of the 16K nominally-available budget. See section 8.2's binary search.

**Fix:** keep every record under the 10 KB / 150-flow ceiling. Section 8's dynamic chunking does this automatically; for the simple flow in sections 2–6, restrict your input scope to a small enough window. Don't raise `--max-chunk-bytes` past 10 KB without re-running the binary search in section 8.2.

### 10.3 409 Conflict on re-upload

**Symptom:**

```
HTTP 409 from /files/uploads/initiate
Response body: {"errors":[{"code":"conflict","message":"File already exists: ..."}]}
```

**Cause:** the Files API enforces unique filenames per org. Once you've uploaded `foo.jsonl`, you can't upload another file with that name (no overwrite, no documented per-file delete endpoint).

**Fix:** the upload scripts catch this and exit gracefully now — `upload_multipart.py` skips and points at the existing file_id; `upload_directory.py` records `file_uid=""` and continues. To force a re-upload of new content, either rename the local file (add a session suffix) or try `python cleanup.py --remote` to delete the platform-side copy first.

### 10.4 Reading the events log: success vs failure signals

**`(item count unknown)` is normal.** The platform emits `Processing input 0: <name>.jsonl (item count unknown)` on every run before it has counted records — successful AND failed. Don't treat it as an error indicator on its own.

**The actual success signal** is the pair of events that follow:

```
Batch 0: processed 1 items (1 success, 0 failed)
SUCCESS Processed 1 items (1 success, 0 failed) from <name>.jsonl
```

**The actual failure signals are:**

- `WARN inference.truncation: Batch N: M items exceed token_budget=16,384` — pre-flight warning that one or more records are over the per-record budget. Typically followed by `ERROR pod.terminated: OOMKilled` and `Status: FAILED`. See 10.2.
- `ERROR pod.terminated: Container worker terminated: OOMKilled (exit=137)` — pod-level OOM, fatal. The job ends `FAILED` with `BackoffLimitExceeded`.
- `FAILED Processing failed for <name>.jsonl` — typically wrong content-type at upload (see 10.1) or other unrecoverable file-read error.
- Empty or 1–3-character prediction text in the downloaded output — even when the events log looks clean, always inspect at least the first prediction's text before declaring a run successful.

In short: **don't rely on the top-level `Status: COMPLETED`.** Read the full events log AND inspect at least the first prediction's text.

### 10.5 Output filenames don't join back to input file_ids

**Symptom:** `extract_predictions.py` reports `WARNING: N output files could not be matched to a file_uid.`

**Cause:** the upload manifest (`data/uploaded_file_ids.jsonl`) is missing entries for some files — typically because the section-8 directory upload skipped them via a 409 (and recorded `file_uid=""`), or you ran the batch job against files uploaded in a different session whose `file_uid`s aren't in the current manifest.

**Fix:** ensure every file submitted to the batch job has its `file_uid` recorded in `data/uploaded_file_ids.jsonl`. Easiest path is to start fresh: `python cleanup.py --local --remote`, then re-run the upload step from scratch.

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
