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

**Multi-home batch pattern** — required for GB-scale inputs and multi-tenant deployments (multiple homes, humans per home, devices per human). Streaming dynamic chunking preserves every flow into ≤16 KB / ~4K-token chunks (the empirically validated upper bound — §10.6). Chunks are concatenated into a single multi-record JSONL, **split positionally in half**, and uploaded as two files via multipart so two batch jobs can run in parallel on both available GPU nodes. Six logical batch stages (chunk → bucket-A → bucket-B → device-day → user-day + house-day) fold the chunks back into per-device, per-user, and per-house daily narratives — each stage is split into two parallel jobs. Used in [section 8](#8-multi-home-batch-with-many-small-files).

```
multi-home CSV
   │ prepare_per_device_hour_jsonls.py   (dynamic chunking, ≤16 KB / ~4K tokens per chunk — §10.6; no sampling)
   ▼
data/per_device_hour/  (one tiny JSONL per chunk) + sidecar manifest
   │ cat → 1 multi-record JSONL → positional split into halves
   ▼
data/multihome_chunks_h1.jsonl  +  data/multihome_chunks_h2.jsonl
   │ upload_multipart.py × 2  (parallel)
   ▼
2 file_uids
   │ create_activity_detection_job.py --file <half>  × 2  (run on 2 GPUs in parallel)
   ▼
outputs/h1 + outputs/h2  →  cat (in order)  →  data/predictions_chunks.jsonl
   │ join to manifest by content key (device_id + hour + n_flows + ts_lo + ts_hi + n_bytes — §10.7)
   ▼
chunk-slice narratives keyed by manifest file_id
   │ prepare_bucket_reduce.py --stage a   (`--group-size 3` — our cliff-safe default in this repo; script default is 4 — §8.6)
   ▼
~7,950 partials at group-size 3 → split in 2 → 2 batch jobs
   │ prepare_bucket_reduce.py --stage b   (1 record per (device, hour))
   ▼
576 device-hour narratives → split in 2 → 2 batch jobs
   │ prepare_device_day_reduce.py
   ▼
24 device-day narratives → split in 2 → 2 batch jobs
   ├─► prepare_user_day_reduce.py  ─►  6 user-day narratives (1 batch job — too small to split usefully)
   └─► prepare_house_day_reduce.py ─►  3 house-day narratives (1 batch job)
```

| Step | Script | Description |
|------|--------|-------------|
| Prepare (simple) | `1_prepare_data/prepare_{home,device}_level_jsonl.py` | Build one-record-per-scope JSONL |
| Generate (multihome) | `1_prepare_data/generate_synthetic_multihome_csv.py` | Synthesize multi-home CSV from `data/topology.json` |
| Chunk prep | `1_prepare_data/prepare_per_device_hour_jsonls.py` | Greedy size-based chunking — every flow preserved, one chunk per single-record JSONL file (~37K files at 1 GB) |
| Bucket reduce | `1_prepare_data/prepare_bucket_reduce.py --stage {a,b}` | Two-pass fold: chunks → partials → 1 narrative per (device, hour) |
| Device/user/house reduce | `1_prepare_data/prepare_{device,user,house}_day_reduce.py` | Fold device-hour → device-day → {user-day, house-day} |
| Concat + split | `cat` + Python one-liner | Concatenate per-chunk JSONLs into one multi-record file, then positionally split into two halves for 2-GPU parallelism (§8.3) |
| Upload | `2_upload/upload_multipart.py` | Multipart presigned-URL upload of one large file (run twice in parallel, once per half) |
| Batch job | `3_batch_jobs/create_activity_detection_job.py` | Create & monitor a single-file job; run twice in parallel (`--file <h1>` and `--file <h2>`) for 2-GPU concurrency |
| Download | `4_download_outputs/download_outputs.py` | Paginated download of prediction files |
| Join predictions | Python one-liner | Concatenate the two output JSONLs in order, then join to the source manifest by content key (§10.7) — no separate join script needed |
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

- **Context fit:** chunk the input dynamically — for each `(device, hour)` bucket, pack flow rows into chunks until each chunk hits the byte budget, then close it and start a new chunk. **No flows are dropped** — high-volume buckets simply produce more chunks. At 1 GB the chunk batch processes **23,249 independent inferences** at `--max-chunk-bytes 16384` (~4K tokens / chunk). The 576 device-hour buckets stay constant (24 devices × 24 hours); only the chunks-per-bucket count varies with flow volume.
- **Multi-tenant scope:** model a realistic deployment with multiple homes, multiple humans per home, and a mix of personal and shared devices. Five follow-up reduce jobs (bucket-A → bucket-B → device-day → user-day + house-day) fold the chunks back into per-device, per-user, and per-house daily summaries.
- **Throughput — 2 GPU nodes:** the org's batch-job concurrency is ≥2 (confirmed: pairs of jobs run RUNNING simultaneously). Every batch in this section is split into **two positional halves** (`_a1` / `_a2`, `_h1` / `_h2`, etc.) and submitted as two independent jobs so both GPU nodes stay saturated. Splits are positional (lines `[0:N/2]` and `[N/2:]`, no shuffle), so the post-job join is trivially `cat a1_output a2_output` — positionally aligned with the single source manifest, no content-key remap needed. This roughly halves wall-clock at zero quality cost: the chunks batch (23K records at 16 KB cap) finished in ~46h split vs. an extrapolated ~62h single-job; Stage A (~8K records, group-size 3) targets ~7–9h split vs. ~13–18h single-job.

The pattern is adapted from the [wifi-multi `/query` demo](https://github.com/archetypeai/archetypeai-query-examples-wifi-multi) — same topology, same prompt structure — but rebuilt around the **batch jobs + Files API** instead of the synchronous `/query` endpoint. The batch pattern's selling point: independent of source CSV size (1 MB, 1 GB, or 200 GB), the prep step always emits 576 files, each fitting Activity Detection's per-record context budget.

### Pipeline (DAG)

Every flow in the source CSV is processed — no sampling. Buckets that exceed
the per-record context budget are split into multiple chunks, then folded
back to one narrative per `(device, hour)` via a two-pass bucket reduce.

```
1 GB multi-home CSV (5.8M flows, 24 devices × 24 hours of data)
         │
         ▼  prepare_per_device_hour_jsonls.py  (dynamic chunking, ≤16 KB / chunk ≈ ≤4K tokens — validated upper bound, see §10.6)
data/per_device_hour/  (~23K single-record JSONLs at 16 KB cap)
+ data/manifest_chunked.jsonl  (one entry per chunk file, in chunker-emit order)
         │
         ▼  cat per_device_hour/*.jsonl > multihome_chunks.jsonl   (one multi-record JSONL, no shuffle)
         ▼  positional split: head -n N/2  →  _h1.jsonl    /    tail  →  _h2.jsonl
data/multihome_chunks_h1.jsonl  +  data/multihome_chunks_h2.jsonl   (≈11.6K records each at 1 GB / 16 KB cap)
         │
         ▼  upload_multipart.py × 2 (parallel)
         ▼  create_activity_detection_job.py --file <half>  × 2 (run on 2 GPU nodes in parallel — org concurrency ≥2)
outputs/h1/output_*.jsonl  +  outputs/h2/output_*.jsonl
         │
         ▼  cat outputs/h1/* outputs/h2/* > data/predictions_chunks.jsonl   (concat in order)
         ▼  content-key join to manifest_chunked.jsonl (§10.7)   →   predictions keyed by file_id
         │
         ▼  prepare_bucket_reduce.py --stage a   (group ≤4 chunks per pack, default; drop to 3 if cliff warning fires)
data/bucket_reduce_a.jsonl  (~5,800 at group-size 4 / ~7,950 at group-size 3)
         │
         ▼  positional split + upload × 2 + create_activity_detection_job × 2  (2 GPUs in parallel)
         ▼  cat outputs → predictions_bucket_reduce_a.jsonl   (positional join — we control the split, no shuffle)
         │
         ▼  prepare_bucket_reduce.py --stage b   (1 record per (device, hour))
data/bucket_reduce_b.jsonl  (576 records)  →  split × 2 → 2 batch jobs → cat outputs
         │
         ▼  576 device-hour narratives + manifest_per_device_hour.jsonl
         │
         ▼  prepare_device_day_reduce.py  (group by device)
1 JSONL × 24 records  →  split × 2 → 2 batch jobs → cat outputs  →  24 device-day narratives
         │
         ├─►  prepare_user_day_reduce.py   (group by human, personal devices only)
         │    1 JSONL × 6 records → 1 batch job → 6 user-day narratives  (too small to split usefully)
         │
         └─►  prepare_house_day_reduce.py  (group by home, all devices)
              1 JSONL × 3 records → 1 batch job → 3 house-day narratives
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

**Why one record per file at this stage?** The chunker emits per-chunk JSONLs primarily so its sidecar manifest can use the chunk filename as a stable `file_id` (the manifest records device, hour, n_flows, ts range per chunk). Downstream we **concatenate the per-chunk files into a single multi-record JSONL and upload via multipart** rather than uploading thousands of small files — multipart at GB scale is dramatically faster and avoids per-file 409s on re-run.

> **Historical note on multi-record JSONLs.** An earlier UI test uploaded a 196-record JSONL (each record ~10 KB, file total 2 MB) and the platform OOMed during batch processing — bisected from `bs=16` all the way down to `bs=1` and still OOMed (see §10.5). With `batch_size: 4` (now passed by default from `create_activity_detection_job.py`), the platform respects the cap and batches normally. **Multi-record JSONLs are now the recommended ingestion shape** as long as `batch_size: 4` is set.

Outputs:

| File / dir | Contents |
|---|---|
| `data/per_device_hour/dev_<home_id>__<device_id>__hHH__cNNNN.jsonl` (~37K at 1 GB) | Single-record JSONL — one chunk per file, ≤10 KB each |
| `data/manifest_chunked.jsonl` (~37K lines at 1 GB) | Sidecar: one entry per chunk file, mapping `file_id` → `{home_id, human, device_id, mac, hour_utc, chunk_index, n_flows, n_bytes, ts_start_min, ts_start_max, ...}` |

File count scales linearly with source size: 1 GB ≈ 37K files, 10 GB ≈ 370K, etc. The 576-bucket grid is fixed by topology (24 devices × 24 hours), but each bucket's chunk count varies with that device-hour's flow volume.

Memory: the script keeps 576 in-flight chunk buffers (each ≤ `max_chunk_bytes`) ≈ ~6 MB RAM regardless of source size. Streaming on the input side, so any CSV size works. Each chunk file is opened, written in one shot, and closed — no concurrent file-handle pressure.

#### Why the per-chunk byte cap is 10 KB (empirically observed)

The 10 KB cap is binding for **two independent reasons**, both confirmed by experiment in May 2026 against `pipeline activity-detection v1.1.1-409-e749ac0`:

##### Reason 1: GPU memory at batch initialization (mostly fixed by `batch_size: 4`)

The platform's batching engine starts at `bs=16`, periodically tries `bs=24` after several successful batches, and OOMs unrecoverably on the larger size — bisecting `bs=24 → 16 → 8 → 4 → 2 → 1` and crashing the pod when even `bs=1` doesn't fit. Per the platform team, **the C 2.5.1 model variant has a broken memory-budget calculation** that triggers this at any non-trivial chunk size; C 2.4.0 has a milder version of the same issue. See [§10.5](#105-oom-cascade-on-batch-size-escalation) for full diagnostic details and the workaround:

```yaml
worker:
  config:
    model_variant: newton/c:2.4.0-7b-base    # avoid the broken 2.5.1 budget
    batch_size: 4                            # cap the engine at bs=4 (no bs=24 escalation)
```

These two settings together let chunks up to ~10 KB run cleanly through batched inference at bs=4. **Without `batch_size: 4`, the engine still escalates to bs=24 and crashes; without `model_variant: newton/c:2.4.0-7b-base`, even bs=1 fails on first allocation.**

##### Reason 2: CSV-heavy quality cliff (no warning, silent garbage)

Even when per-line context fits the platform's 16,384-token budget AND batch initialization succeeds, **the model degrades into table-completion mode** when the input is dominated by tabular/CSV-style data above a content-shape-dependent threshold. No warning fires; the platform reports `0 failed`; the predictions are 1-3 character fragments like `'00'`, `'|153|'`, or empty strings. This is exactly the failure mode wifi-multi observed at ~18.5 KB for `/query` ([their "Constraints driving the design"](https://github.com/archetypeai/query-examples-wifi-multi/#constraints-driving-the-design)).

**Empirical sweep at 1 GB (May 2026), 5-record probe per cap, `bs=1`, `model_variant=newton/c:2.4.0-7b-base`, `max_new_tokens=1024`:**

| `--max-chunk-bytes` cap | Avg `inputs[0].data` | ~Tokens (data)¹ | `WARN inference.truncation`? | Output quality |
|---|---|---|---|---|
| 10240 (10 KB, current default) | 10.4 KB | ~2.5k | None | **Real narratives** ✓ |
| 12288 (12 KB) | 12.4 KB | ~3k | None | **Real narratives** ✓ |
| 16384 (16 KB) | 16.5 KB | **~4k** (last good) | None | **Real narratives** ✓ (richer than 12 KB — more specific volume numbers, peak-hour observations) |
| 20480 (20 KB) | 20.6 KB | ~5k | None | ✗ Garbage (`'72\|1'`, `'0'`, `''`) |
| 24576 (24 KB) | 24.5 KB | ~6k | None | ✗ Garbage |
| 28672 (28 KB) | 28.8 KB | ~7k | None | ✗ Garbage |
| 32768 (32 KB) | 32.7 KB | ~8k | None | ✗ Garbage (`'00'`, `'801'`) |
| 36864 (36 KB) | 37.0 KB | ~9k | None | ✗ Garbage |
| 40960 (40 KB) | 41.1 KB | ~10k | **Yes — every record** | ✗ Garbage |
| 49152 (48 KB) | 48.4 KB | ~12k | Yes | ✗ Garbage |
| 53248 (52 KB) | 53.4 KB | ~13k | Yes | ✗ Garbage |
| 55296 (54 KB) | 55.4 KB | ~14k | Yes | ✗ Garbage |
| 61440 (60 KB) | 61.6 KB | ~15k | Yes | ✗ Garbage |
| 65536 (64 KB) | 65.7 KB | ~16k | Yes | ✗ Garbage |

¹ **Tokens column uses ~0.25 tok/byte** (generic 4 chars/token), which matches the quality cliff — empirically, **~4k tokens of flow data is the last good size**. The platform's own tokenizer for pipe-separated flow rows is denser (~0.4 tok/byte), and that's what drives the *truncation* cliff at ~41 KB (~16k tokens of actual context spend, exceeding the 16,384 ctx − 1024 out − ~200 prompt budget — see observation 1 below). The two cliffs are independent: the **quality cliff** (~4k tokens / 16 KB) is a small-model coherence limit and fires silently; the **truncation cliff** (~16k tokens / 41 KB) is a context-length limit and emits a warning.

**Two observations from this table:**

1. **The truncation warning fires at ≥41 KB**, not at the much higher 16K-token estimate (~64 KB by naive `bytes/4`). The platform's tokenizer counts pipe-separated flow rows at ~0.4 tokens per byte, not the ~0.25 a generic estimate would suggest. So `inputs[0].data ≥ 41 KB` ≈ `15,360+ tokens`, exceeding the 16,384 − 1024(max_new_tokens) − ~200(prompt overhead) input ceiling.
2. **The quality cliff sits between 16.5 KB and 20.6 KB**, well below the truncation threshold. **At ≤ 16.5 KB the model produces real narratives. At ≥ 20.6 KB it produces garbage with no warning.** The model auto-completes tabular content rather than analyzing it.

##### Default `--max-chunk-bytes` rationale

The proven safe range is **10 KB (default) to 16 KB (empirically validated upper bound)**. 10 KB has ~60% margin below the cliff; 16 KB sits right under it but produces meaningfully richer narratives (more specific numbers, peak-hour observations) because the model has 1.6× more flow data to work with.

| Cap | Trade-off |
|---|---|
| `--max-chunk-bytes 10240` (default) | Safe, well-tested, 50% margin below cliff. ~150 flows / chunk. |
| `--max-chunk-bytes 12288` | Validated empirically; richer narratives. ~180 flows / chunk. |
| `--max-chunk-bytes 16384` | **Empirically maximum useful** — at the cliff edge, narratives are richest, but very little margin. Re-validate with a 5-record probe before committing to a long run. |
| `--max-chunk-bytes > 16384` | **Don't.** ≥ 20 KB = silent garbage; ≥ 41 KB = explicit truncation warning. |

Going beyond 16 KB requires re-running the cliff sweep — there is *zero* signal in the events log to distinguish "fits" from "garbage" until you read the prediction text.

**Multi-record-per-file architecture aside.** An earlier UI test uploaded a 196-record JSONL (each record ~10 KB, file total 2 MB) and the platform OOMed during batch processing — bisected from `bs=16` all the way down to `bs=1` and still OOMed. With `batch_size: 4` (added later) the platform respects the cap and batches normally. Either architecture (one record per file × N files, or one file × N records) works as long as `batch_size: 4` is set.

### 8.3 Concat per-chunk JSONLs, split positionally, upload both halves

```bash
# 1. Concatenate ~23K per-chunk JSONLs into one multi-record file (no shuffle — keeps positional alignment with the sidecar manifest)
cat data/per_device_hour/*.jsonl > data/multihome_chunks.jsonl
# At 1 GB / 16 KB cap this produces ~23K lines in one file (~400 MB).

# 2. Positional split into two halves (no shuffle)
./myenv/bin/python <<'PY'
with open("data/multihome_chunks.jsonl") as f: lines = f.readlines()
mid = len(lines) // 2
open("data/multihome_chunks_h1.jsonl", "w").writelines(lines[:mid])
open("data/multihome_chunks_h2.jsonl", "w").writelines(lines[mid:])
PY

# 3. Upload both halves in parallel — multipart presigned-URL upload
python 2_upload/upload_multipart.py data/multihome_chunks_h1.jsonl &
python 2_upload/upload_multipart.py data/multihome_chunks_h2.jsonl &
wait
```

**Why concat + split + multipart instead of uploading 23K small files?**
- Multipart upload of two ~200 MB files saturates the API far better than thousands of sequential ~17 KB PUTs. Empirically the multipart path completes in ~1–2 minutes per half; the equivalent directory upload took 1–2 hours.
- The split is **positional** (no shuffle), which keeps `multihome_chunks_h1[i]` aligned with `manifest_chunked[i]` for `i < N/2` and `multihome_chunks_h2[j]` aligned with `manifest_chunked[N/2 + j]`. Post-download join is then a trivial concat in the same order (§8.5).
- **2 GPU nodes in parallel.** The org's batch-job concurrency is ≥2, so two single-file jobs run simultaneously on two GPU nodes and halve wall-clock at no quality cost (see [§8 intro](#8-multi-home-batch-with-many-small-files) — bullet 3, and the wall-clock table below).

### 8.4 Run the main chunk batch — two jobs in parallel

```bash
python 3_batch_jobs/create_activity_detection_job.py \
  --file multihome_chunks_h1.jsonl \
  --name multihome-chunks-h1 \
  --model-variant newton/c:2.4.0-7b-base \
  --batch-size 1 \
  --max-new-tokens 1024 \
  --poll-interval 300 &

python 3_batch_jobs/create_activity_detection_job.py \
  --file multihome_chunks_h2.jsonl \
  --name multihome-chunks-h2 \
  --model-variant newton/c:2.4.0-7b-base \
  --batch-size 1 \
  --max-new-tokens 1024 \
  --poll-interval 300 &
```

Both jobs use `--file` (single multi-record JSONL per job). The platform reads the file once and treats each line as one inference. Per-record output is keyed by `line_index` (0..N/2 within each half). At 1 GB / 16 KB cap each half is ~11.6K records.

### 8.5 Download both halves, concat in order, join to manifest

```bash
python 4_download_outputs/download_outputs.py <h1_job_id> outputs/multihome-chunks-h1
python 4_download_outputs/download_outputs.py <h2_job_id> outputs/multihome-chunks-h2

# Concat in order (h1 first, h2 second — preserves positional alignment with manifest_chunked.jsonl)
cat outputs/multihome-chunks-h1/output_*.jsonl \
    outputs/multihome-chunks-h2/output_*.jsonl \
  > data/predictions_chunks_raw.jsonl
```

If the per-chunk JSONLs were concatenated **without** shuffling in §8.3, the positional join holds: `predictions_chunks_raw.jsonl[i]` corresponds to `manifest_chunked[i]`. If you (or an earlier session) shuffled before splitting, the positional join is silently wrong — see §10.7 and use the content-key join below.

**Joining to the manifest by content key** (works for both shuffled and non-shuffled inputs — recommended default):

```python
import json, re
from datetime import datetime, timezone

def fmt_hms(ts): return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")
PREAMBLE_OVERHEAD = len(
    "Flow log fields (pipe-separated): time_utc|mac_a|mac_b|prot|tran|port_a|port_b|"
    "bytes_a|bytes_b|pkts_a|pkts_b. Transport: 6=TCP, 17=UDP, 1=ICMP, 58=ICMPv6, 2=IGMP."
    .encode("utf-8")) + 2

manifest_by_key = {}
for e in (json.loads(l) for l in open("data/manifest_chunked.jsonl")):
    if e["n_flows"] == 0:
        key = ("empty", e["device_id"], e["hour_utc"])
    else:
        key = (e["device_id"], e["hour_utc"], e["n_flows"],
               fmt_hms(e["ts_start_min"]), fmt_hms(e["ts_start_max"]), e["n_bytes"])
    manifest_by_key[key] = e["file_id"]

PROMPT_RE = re.compile(
    r"Device: (\S+?) \(.*?Hour: (\d+):\d+-\d+:\d+ UTC\."
    r"(?: Chunk slice: (\d+) flows covering (\d+:\d+:\d+)-(\d+:\d+:\d+) UTC\.|"
    r" Flow count: 0\.)")

def record_to_key(rec):
    m = PROMPT_RE.search(rec["prompt"])
    if not m: return None
    dev, hr, n, lo, hi = m.groups()
    if n is None: return ("empty", dev, int(hr))
    n_bytes = len(rec["inputs"][0]["data"].encode("utf-8")) - PREAMBLE_OVERHEAD
    return (dev, int(hr), int(n), lo, hi, n_bytes)

# Build keys from the same-order concatenated inputs (h1 + h2)
keys = []
for path in ("data/multihome_chunks_h1.jsonl", "data/multihome_chunks_h2.jsonl"):
    keys.extend(record_to_key(json.loads(l)) for l in open(path))

with open("data/predictions_chunks_raw.jsonl") as f, \
     open("data/predictions_chunks.jsonl", "w") as out:
    for key, line in zip(keys, f):
        pred = json.loads(line)
        fid = manifest_by_key.get(key)
        if fid is None: continue   # log if you care
        out.write(json.dumps({"file_id": fid, "line_index": 0,
                              "prediction": pred.get("prediction", "")}) + "\n")
```

The content key is `(device_id, hour, n_flows, ts_lo, ts_hi, n_bytes)`. At 1 GB / 16 KB cap, **23,249 of 23,249** chunks join cleanly (6 latent collisions out of 23,249 — 0.03%; the `n_bytes` term disambiguates). See §10.7 for the rationale.

### 8.6 Bucket reduce stage A — group chunks into per-bucket partials (split × 2)

```bash
# Prep (single output JSONL covering all (device, hour) buckets — keep group-size at 3 to stay clear of the cliff)
python 1_prepare_data/prepare_bucket_reduce.py --stage a \
  --predictions data/predictions_chunks.jsonl \
  --manifest    data/manifest_chunked.jsonl \
  --output      data/bucket_reduce_a.jsonl \
  --output-manifest data/manifest_bucket_reduce_a.jsonl \
  --group-size 3

# Positional split (no shuffle — preserves alignment with manifest_bucket_reduce_a.jsonl)
./myenv/bin/python <<'PY'
with open("data/bucket_reduce_a.jsonl") as f: lines = f.readlines()
mid = len(lines) // 2
open("data/bucket_reduce_a_a1.jsonl", "w").writelines(lines[:mid])
open("data/bucket_reduce_a_a2.jsonl", "w").writelines(lines[mid:])
PY

# Upload + jobs in parallel (2 GPUs)
python 2_upload/upload_multipart.py data/bucket_reduce_a_a1.jsonl &
python 2_upload/upload_multipart.py data/bucket_reduce_a_a2.jsonl &
wait

python 3_batch_jobs/create_activity_detection_job.py --file bucket_reduce_a_a1.jsonl --name multihome-bucket-reduce-a-a1 --max-new-tokens 1024 --batch-size 1 --poll-interval 300 &
python 3_batch_jobs/create_activity_detection_job.py --file bucket_reduce_a_a2.jsonl --name multihome-bucket-reduce-a-a2 --max-new-tokens 1024 --batch-size 1 --poll-interval 300 &
wait

# Download + concat (positional — we control the split so no content-key join needed)
python 4_download_outputs/download_outputs.py <a1_job_id> outputs/multihome-bucket-reduce-a-a1
python 4_download_outputs/download_outputs.py <a2_job_id> outputs/multihome-bucket-reduce-a-a2
cat outputs/multihome-bucket-reduce-a-a1/output_*.jsonl \
    outputs/multihome-bucket-reduce-a-a2/output_*.jsonl \
  > data/predictions_bucket_reduce_a.jsonl
```

For each `(device, hour)` bucket, Stage A groups the bucket's chunks into packs (up to `--group-size` chunks per pack) and emits one reduce record per group. **We use `--group-size 3`** — empirically the cliff-safe setting in this repo: zero `WARN  ... > 16,384` lines fired during prep on the 1 GB / 16 KB-chunk run, producing **~7,950 partial records** (average ~14 partials per bucket on the dense halves). The script's own default is **4** (lowered from 20 in commit `1086817`), which usually works but is enough to occasionally push a partial-record payload past the 16 KB / ~4K-token quality cliff (§10.6); we go one notch lower for zero-risk margin. Going to 5 or 6 would shave wall-clock further at the cost of more cliff warnings on dense buckets. Stage B's record count is unchanged regardless (always 576).

### 8.7 Bucket reduce stage B — fold partials into one narrative per bucket (split × 2)

```bash
python 1_prepare_data/prepare_bucket_reduce.py --stage b \
  --predictions data/predictions_bucket_reduce_a.jsonl \
  --manifest    data/manifest_bucket_reduce_a.jsonl \
  --output      data/bucket_reduce_b.jsonl \
  --output-manifest data/manifest_per_device_hour.jsonl

# Positional split (no shuffle)
./myenv/bin/python <<'PY'
with open("data/bucket_reduce_b.jsonl") as f: lines = f.readlines()
mid = len(lines) // 2  # 288
open("data/bucket_reduce_b_b1.jsonl", "w").writelines(lines[:mid])
open("data/bucket_reduce_b_b2.jsonl", "w").writelines(lines[mid:])
PY

python 2_upload/upload_multipart.py data/bucket_reduce_b_b1.jsonl &
python 2_upload/upload_multipart.py data/bucket_reduce_b_b2.jsonl &
wait

python 3_batch_jobs/create_activity_detection_job.py --file bucket_reduce_b_b1.jsonl --name multihome-bucket-reduce-b-b1 --max-new-tokens 1024 --batch-size 1 --poll-interval 300 &
python 3_batch_jobs/create_activity_detection_job.py --file bucket_reduce_b_b2.jsonl --name multihome-bucket-reduce-b-b2 --max-new-tokens 1024 --batch-size 1 --poll-interval 300 &
wait

python 4_download_outputs/download_outputs.py <b1_job_id> outputs/multihome-bucket-reduce-b-b1
python 4_download_outputs/download_outputs.py <b2_job_id> outputs/multihome-bucket-reduce-b-b2
cat outputs/multihome-bucket-reduce-b-b1/output_*.jsonl \
    outputs/multihome-bucket-reduce-b-b2/output_*.jsonl \
  > data/predictions_per_device_hour.jsonl
```

Stage B always emits exactly **576 reduce records** — one per `(device, hour)` bucket — folding that bucket's Stage-A partials into a single device-hour narrative. The output sidecar `manifest_per_device_hour.jsonl` matches the shape the downstream device-day reduce script expects.

### 8.8 Reduce stage 3: device-day (576 → 24, split × 2)

```bash
python 1_prepare_data/prepare_device_day_reduce.py \
  --predictions data/predictions_per_device_hour.jsonl \
  --manifest    data/manifest_per_device_hour.jsonl \
  --output      data/device_day_reduce.jsonl \
  --output-manifest data/manifest_device_day.jsonl

# Positional split: 12 + 12
./myenv/bin/python <<'PY'
with open("data/device_day_reduce.jsonl") as f: lines = f.readlines()
mid = len(lines) // 2  # 12
open("data/device_day_reduce_d1.jsonl", "w").writelines(lines[:mid])
open("data/device_day_reduce_d2.jsonl", "w").writelines(lines[mid:])
PY

python 2_upload/upload_multipart.py data/device_day_reduce_d1.jsonl &
python 2_upload/upload_multipart.py data/device_day_reduce_d2.jsonl &
wait

python 3_batch_jobs/create_activity_detection_job.py --file device_day_reduce_d1.jsonl --name multihome-device-day-d1 --max-new-tokens 2048 --batch-size 1 --poll-interval 60 &
python 3_batch_jobs/create_activity_detection_job.py --file device_day_reduce_d2.jsonl --name multihome-device-day-d2 --max-new-tokens 2048 --batch-size 1 --poll-interval 60 &
wait

python 4_download_outputs/download_outputs.py <d1_job_id> outputs/multihome-device-day-d1
python 4_download_outputs/download_outputs.py <d2_job_id> outputs/multihome-device-day-d2
cat outputs/multihome-device-day-d1/output_*.jsonl \
    outputs/multihome-device-day-d2/output_*.jsonl \
  > data/predictions_device_day.jsonl
```

Each of the 24 reduce records concatenates that device's 24 hourly narratives in a `=== HOURLY SUMMARIES (24) FOR <device> IN <home> ON <date> ===` envelope, ending with the synthesis instruction. Pack size ≈ 12 KB per record — well under the C model's ~21 KB narrative-heavy ceiling.

### 8.9 Reduce stages 4a/4b: user-day and house-day (24 → {6, 3})

Both consume the device-day predictions and run as independent batch jobs. **These are too small to benefit from splitting** (6 and 3 records — queue/setup overhead dominates), so they're each submitted as a single job. The two jobs run in parallel and saturate both GPUs together:

```bash
# User-day (personal devices only)
python 1_prepare_data/prepare_user_day_reduce.py \
  --predictions data/predictions_device_day.jsonl \
  --manifest    data/manifest_device_day.jsonl \
  --output      data/user_day_reduce.jsonl \
  --output-manifest data/manifest_user_day.jsonl

# House-day (every device — personal + shared)
python 1_prepare_data/prepare_house_day_reduce.py \
  --predictions data/predictions_device_day.jsonl \
  --manifest    data/manifest_device_day.jsonl \
  --output      data/house_day_reduce.jsonl \
  --output-manifest data/manifest_house_day.jsonl

python 2_upload/upload_multipart.py data/user_day_reduce.jsonl &
python 2_upload/upload_multipart.py data/house_day_reduce.jsonl &
wait

python 3_batch_jobs/create_activity_detection_job.py --file user_day_reduce.jsonl  --name multihome-user-day  --max-new-tokens 2048 --batch-size 1 --poll-interval 60 &
python 3_batch_jobs/create_activity_detection_job.py --file house_day_reduce.jsonl --name multihome-house-day --max-new-tokens 2048 --batch-size 1 --poll-interval 60 &
wait

python 4_download_outputs/download_outputs.py <user_day_job_id>  outputs/multihome-user-day
python 4_download_outputs/download_outputs.py <house_day_job_id> outputs/multihome-house-day
```

User-day pack size ≈ 3 KB (3 device-day narratives per human), house-day ≈ 8 KB (8 device-day narratives per home) — both fit in C-model context with margin. Output JSONLs land directly in `outputs/multihome-{user,house}-day/` — no extra join step needed since `view_results.py --manifest` consumes the output directory directly (see §8.10).

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
| Chunk inference | ~38,700 (default 10 KB cap) / ~23,250 (validated 16 KB cap) | ≤10 KB (~2.5K tok) default; ≤16 KB (~4K tok) validated upper bound | `--max-chunk-bytes` |
| Bucket reduce A (intra-bucket) | **~7,950 at `--group-size 3`** (our cliff-safe setting); ~5,800 at the script default of 4 | ≤16 KB (~4K tok) — sized to stay under quality cliff | `--group-size` × per-chunk narrative length |
| Bucket reduce B (per-bucket) | 576 | ~3-8 KB | partial narrative length × group_count |
| Device-day reduce | 24 | ~12 KB | 24 × hourly narrative length |
| User-day reduce | 6 | ~3 KB | 3 × device-day narrative length |
| House-day reduce | 3 | ~8 KB | 8 × device-day narrative length |

Chunk count scales linearly with source size. At the default 10 KB cap (~150 flows/chunk): 1 GB ≈ 38K chunks, 10 GB ≈ 380K. At the validated 16 KB cap (~250 flows/chunk): 1 GB ≈ 23K chunks, 10 GB ≈ 230K. The pipeline's *structure* (576 device-hour buckets, 24 device-day records, 6+3 user/house-day records) stays identical at any scale — only stage 1's chunk count grows.

**No sampling, no truncation:** every flow in the source CSV ends up in exactly one chunk; chunk narratives are folded losslessly through Stages A/B into one device-hour narrative. Stages 3-4 then summarize across hours, devices, users, and homes.

### Wall-clock at 1 GB (observed)

End-to-end is **GPU-bound on the main chunk batch**. Numbers below come from an actual run against `pipeline: activity-detection v1.1.1-409-e749ac0` (May 2026, `model_variant: newton/c:2.4.0-7b-base`, `batch_size: 1`, `max_new_tokens: 1024`, `--max-chunk-bytes 16384`). Every stage uses the **2-job split pattern** ("Stage X-1 / Stage X-2" running in parallel on 2 GPU nodes — see §8 intro bullet 3):

| Stage | Records (h1+h2 split) | Observed wall clock |
|---|---|---|
| Generate 1 GB CSV | — | ~40 s |
| Per-chunk prep + concat + split | ~23K chunks (16 KB cap) | ~2–3 min |
| Multipart upload × 2 (parallel) | 2 × ~200 MB | ~1–2 min |
| **Main chunk batch (h1 / h2 in parallel)** | 11,625 + 11,624 records | **~46 hr** (single-job extrapolation: ~62 hr; split saves ~16 hr) |
| Download + concat + content-key join | — | ~5 min |
| Bucket reduce A prep | — | ~10 s |
| **Bucket reduce A (a1 / a2 in parallel)** | 3,984 + 3,984 records (group-size 3) | **~7–9 hr** (single-job extrapolation: ~13–18 hr) — *currently RUNNING; numbers will be updated after completion* |
| Bucket reduce B (b1 / b2 in parallel) | 288 + 288 records | est. ~1.5–2 hr *(pending validation)* |
| Device-day (d1 / d2 in parallel) | 12 + 12 records | est. ~10–15 min *(pending validation)* |
| User-day + house-day (parallel, 1 job each) | 6 + 3 records | est. ~5–10 min total *(pending validation)* |

**Total end-to-end (1 GB, 2 GPU nodes, 16 KB chunks):** **~55–60 hours** dominated by the main chunk batch. The chunk batch alone is ~46 hr out of that.

**Two factors shape the chunk-batch wall clock:**

1. **Per-step time = `max_new_tokens` × generation rate × per-record context size.** At `max_new_tokens=1024` and 16 KB chunk inputs the C model takes ~18–22 s/record; lowering `max_new_tokens` to 256 drops it ~4× to ~5–7 s/record. Reduce stages have smaller inputs (a few KB of narrative vs. 16 KB of flow rows) and run ~3× faster per record at the same `max_new_tokens`: Stage A observed ~6–8 s/record.

2. **Org-level batch-job concurrency cap.** Confirmed ≥2 (both halves of every split observed RUNNING simultaneously). The 2-job split assumes exactly 2 — if your org has higher concurrency (4, 8), split each stage into more pieces (a1/a2/a3/a4...) proportionally for further wall-clock savings.

**Tuning levers if you need a tighter window:**

- **Lower `--max-new-tokens` to 256** on the chunk batch — Bucket-reduce, device-day, user-day, and house-day reduces can keep 1024 or 2048 since they need richer output and run on far fewer records.
- **Run at smaller scale** (100 MB → ~2,300 chunks at 16 KB cap, ~10× shorter wall-clock at every stage).
- **Negotiate higher org concurrency** with your platform team — splits scale linearly.
- **Default to `--max-chunk-bytes 10240` (10 KB)** if cliff-margin matters more than narrative richness. ~38K chunks at 1 GB instead of ~23K, but each is smaller and faster per record (~8–12 s vs. ~18–22 s).

**Beyond 1 GB:** chunk count grows linearly with source size — 10 GB ≈ 230K chunks at 16 KB cap. Even with the 2-job split that's ~460 hours on the chunk batch alone. Past 1 GB the architecture works in principle but isn't practical without higher pod concurrency (4+ way splits), batched-multi-job submission, or per-bucket pre-aggregation.

## 9. Cleanup between runs

A full section-8 run leaves several gigabytes of generated artifacts on disk — synthetic CSVs, ~23K–38K chunk JSONLs (depending on `--max-chunk-bytes`), manifests, predictions, downloaded outputs. The Files API also keeps every uploaded filename indefinitely, so re-running the same pipeline trips a 409 Conflict on the second upload of any file with the same name. `cleanup.py` at the repo root handles both.

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

- `WARN inference.truncation: Batch N: M items exceed token_budget=16,384` — pre-flight warning that one or more records are over the per-record budget. Typically followed by `ERROR pod.terminated: OOMKilled` and `Status: FAILED`. See [§10.2](#102-failed--input-over-the-per-record-token-budget).
- `WARN inference.oom: CUDA OOM on batch 0 (16 items), recovering: splitting into 8 + 8` — engine-level OOM during batch initialization. Sometimes recovers; sometimes cascades to bs=1 and crashes the pod. See [§10.5](#105-oom-cascade-on-batch-size-escalation).
- `ERROR pod.terminated: Container worker terminated: OOMKilled (exit=137)` — pod-level OOM, fatal. The job ends `FAILED` with `BackoffLimitExceeded`.
- `FAILED Processing failed for <name>.jsonl` — typically wrong content-type at upload (see [§10.1](#101-completed-with-no-actual-predictions-wrong-content-type-at-upload)) or other unrecoverable file-read error.
- **Empty or 1–3-character prediction text** in the downloaded output — **silent CSV-heavy quality cliff failure** ([§10.6](#106-csv-heavy-quality-cliff-silent-garbage-with-no-warning)). The events log will be clean and `Status: COMPLETED` — only the prediction text reveals the problem. Always read at least the first prediction.

In short: **don't rely on the top-level `Status: COMPLETED`.** Read the full events log AND inspect at least the first prediction's text. Silent failures (§10.6) are the most insidious — neither the platform's status nor the events log will warn you.

### 10.5 OOM cascade on batch-size escalation (C 2.5.1, partial in 2.4.0)

**Symptom (C 2.5.1, default model):** the very first batch OOMs at `bs=16` and bisects all the way to `bs=1` without recovering, then crashes the pod:

```
WARN   CUDA OOM on batch 0 (16 items), recovering: splitting into 8 + 8
WARN   CUDA OOM on batch 0 (8 items), recovering: splitting into 4 + 4
WARN   CUDA OOM on batch 0 (4 items), recovering: splitting into 2 + 2
WARN   CUDA OOM on batch 0 (2 items), recovering: splitting into 1 + 1
ERROR  CUDA OOM on batch 0 (1 items), cannot reduce further
ERROR  GPU memory unrecoverable: 5 consecutive OOMs, crashing pod for restart
```

This happens even when every input line is well within `token_budget=16,384` (e.g. 10 KB / ~2.6K tokens — 16% of the budget). No `WARN inference.truncation` event fires.

**Cause:** per the platform team, the C 2.5.1 memory-budget calculation is incorrect — the model overstates the GPU memory it has available. Bisection cannot recover because the platform pre-allocates batch memory based on the wrong budget.

**Two-part workaround:**

1. **Pin the older C 2.4.0 model variant** to avoid the broken 2.5.1 memory budget.
2. **Set `batch_size: 4` in `parameters.worker.config`** to cap the engine's batch size and prevent the bs=24 escalation entirely.

```bash
python 3_batch_jobs/create_activity_detection_job.py \
  --file my_chunks.jsonl \
  --name my-job \
  --model-variant newton/c:2.4.0-7b-base
```

You'll need to inject `batch_size: 4` directly into the payload (the `create_activity_detection_job.py` CLI doesn't expose it as a flag yet). Resulting `parameters.worker.config`:

```yaml
worker:
  parallelism: 1
  config:
    generation:
      do_sample: true
      max_new_tokens: 1024
      ...
    model_variant: newton/c:2.4.0-7b-base
    batch_size: 4
```

**Empirically validated.** A 50-record test JSONL with this exact config ran cleanly:

```
Batch 0: processed 4 items (4 success, 0 failed)
Batch 1: processed 8 items (8 success, 0 failed)
Batch 2: processed 12 items (12 success, 0 failed)
... 12 batches total, 50 items, 0 OOMs ...
Job completed in 1010s
```

**What happens without each setting:**

| Config | Behavior |
|---|---|
| Default (C 2.5.1, no `batch_size`) | OOM cascade on batch 0, bisects to bs=1, pod crashes immediately |
| C 2.4.0 only (no `batch_size`) | Runs through several batches at bs=16→bs=8, then trips bs=24 escalation. Sometimes recovers (24→16→8→4 ✓), sometimes doesn't (→2→1 ✗, pod crashes). Longer jobs more likely to hit unrecoverable cascade |
| C 2.4.0 + `batch_size: 4` | **Stable**. Engine respects the cap, never escalates to bs=24, no OOM cascades |

**Without `batch_size: 4`, longer jobs reliably fail.** Two ~18.5K-line jobs we ran (without the batch_size cap) failed on different bs=24 escalations:

| Job | First bs=24 escalation | Outcome |
|---|---|---|
| `-a` | batch 9 (after 144 items) | recovered ✓ — kept running |
| `-b` | batch 7 (after 112 items) | unrecoverable ✗ — pod crashed at bs=1 |

A separate 4,627-line probe (`p4`) reached batch 35 (560 items) before tripping a fatal escalation. Failure point varies; eventual failure on long jobs without `batch_size: 4` is reliable.

**This entry should partially go stale once the C 2.5.1 budget is fixed.** Re-test by dropping `--model-variant` and submitting a small job; if it runs cleanly without the OOM-at-bs=1 pattern, the C 2.5.1 fix has landed. The `batch_size: 4` cap will likely stay relevant longer (it's a defensive cap, not a workaround for a specific bug).

### 10.6 CSV-heavy quality cliff (silent garbage with no warning)

**Symptom:** the events log shows clean batch processing — `Batch 0: processed 1 items (1 success, 0 failed)`, `inference.completed`, no warnings — but the downloaded predictions are 1-3 character fragments:

```
[0] ''
[1] '999'
[2] '0'
[3] ''
[4] '|153|'
```

The platform reports the job as `COMPLETED` with `0 failed`. There is **no signal** in the events log that anything went wrong.

**Cause:** the C model degenerates into **table-completion mode** when the input is dominated by tabular/CSV-style data above a content-shape-dependent threshold. Newton's autoregressive prediction reads "this looks like the start of a table" from the long pipe-separated flow log, and starts auto-completing more table rows instead of producing the analysis the prompt asked for. The trailing instruction (`"Analyze the attached flow log slice..."`) gets effectively ignored.

This matches the [wifi-multi `/query` finding](https://github.com/archetypeai/query-examples-wifi-multi/#constraints-driving-the-design) of an ~18.5 KB CSV-heavy quality cutoff, expressed slightly differently on the batch endpoint.

**Empirical sweep at 1 GB (May 2026):** see the table in §8.2. Summary:

| `inputs[0].data` size | Output |
|---|---|
| ≤ 16.5 KB | Real narratives ✓ |
| **16.5 – 20.6 KB (cliff zone)** | **Boundary — empirically untested in 4 KB increment, exact cutoff unknown** |
| ≥ 20.6 KB | Garbage (table-completion mode), no warning |
| ≥ 41 KB | Truncation warning fires + garbage |

**Detection:** the only signal is the prediction text itself. **Always inspect at least the first prediction before trusting a `COMPLETED` job:**

```bash
python 4_download_outputs/download_outputs.py <job_id> outputs/<job_name>
python -c "
import json, glob
for path in glob.glob('outputs/<job_name>/*.jsonl'):
    with open(path) as f:
        for i, line in enumerate(f):
            pred = json.loads(line).get('prediction', '')
            print(f'  [{i}] len={len(pred)} chars  {pred[:80]!r}')
            if i >= 4:
                break
"
```

If the prediction lengths are 0–3 characters, you've crossed the cliff. Re-prep with `--max-chunk-bytes` no higher than ~12 KB.

**Why no warning fires:** the model is producing tokens — the platform just doesn't second-guess what it produces. Generation completes; predictions get returned; the engine's only job is to confirm "did the worker exit cleanly?" That's why §10.4 emphasizes you must always read prediction text, not just the events log.

**Fix:** use `--max-chunk-bytes 10240` (the proven default), or up to **16384** which is empirically validated and produces richer narratives. **Do not exceed 16 KB without re-running the cliff sweep** with a 5-record probe at the new size — there is no other signal that you've crossed.

### 10.7 Position-based join silently mislabels predictions when the input JSONL was shuffled

**Symptom:** `view_results.py --manifest` runs cleanly and prints labeled narratives, but the manifest scope (e.g., `Home A • Alice • alice_phone • 07:00`) doesn't match the prediction text (which talks about a `smart_speaker` at `20:00`). No error, no warning — predictions look real, just attached to the wrong device-hour.

**Cause:** when you concatenate the per-chunk single-record JSONLs into one master JSONL and split it into shards (e.g., `_h1.jsonl`, `_h2.jsonl`) — especially if you `shuf` before splitting to balance shard size — the master file's row order no longer matches the sidecar manifest's row order. The output preserves `line_index` relative to *the shuffled input*, not relative to the manifest. A positional join (`manifest[i] ↔ output[i]`) then silently pairs every prediction with the wrong manifest entry.

**Fix:** join by **content** extracted from the input prompt, not by position. Every prompt emitted by `prepare_per_device_hour_jsonls.py` embeds `device_id`, `hour`, `n_flows`, and the chunk's `ts_lo`–`ts_hi` UTC range — enough to uniquely key against the manifest. Add `n_bytes` (from `len(inputs[0].data.encode("utf-8"))` minus the fixed preamble) for the last few percent of collisions when a device has multiple chunks within the same hour. If you never shuffle or split the master JSONL, positional join is safe — but there's no signal that tells you which case you're in, so prefer content-based joining as the default whenever a master JSONL passes through any reordering step.

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
