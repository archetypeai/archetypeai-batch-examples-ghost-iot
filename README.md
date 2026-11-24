# Archetype AI Batch Examples: GHOST-IoT (Activity Detection)

Run Newton's **Activity Detection** batch pipeline over smart-home WiFi flow logs and get plain-language answers to two questions:

1. **What happened on the home network yesterday?** One narrative for the whole home.
2. **What did each device do yesterday?** One narrative per active device.

Section 7 scales the same idea to synthetic multi-home data: GB of flows, folded by map-reduce into one narrative per device-hour, device-day, person and house.

## Model and platform

| | |
|---|---|
| Platform | Archetype AI platform API 2.0 (`https://platform.u1.archetypeai.app/api`) |
| Pipeline | `activity-detection` (batch) |
| Default model | `newton/c:2.6.0-8b-fp8-base` (Newton C 2.6 8B FP8), set by the scripts |
| Other model | `newton/c:2.5.1-8b-base` (Newton C 2.5.1 8B, the platform's default): pass `--model-variant newton/c:2.5.1-8b-base` |
| Largest record that gives a real narrative | **~16 KB** of flow log on C 2.5.1 (16,384-token context), **~32 KB** on C 2.6 ([§9.3](#93-oversized-records-garbage-on-c-251-inference-error-on-c-26)) |

The scripts default to C 2.6 because it is faster and takes twice the input. On the full multi-home run its 716 near-full 16 KB records took 22 minutes against 5 h 15 min on C 2.5.1 ([§7](#7-scale-multi-home-map-reduce)), and the home-level record here takes 17 s against 37 s. Both models produce comparable narratives on these examples. The scripts always send `model_variant`, so a change of the platform's default doesn't change your results.

## Pipeline

```
wlan0_ipv4_flows_db.csv
   │ 1_prepare_data/prepare_{home,device}_level_jsonl.py   (filter one day, serialize raw flows)
   ▼
ghost_iot_home_yesterday.jsonl (1 record)   ghost_iot_devices_yesterday.jsonl (5 records)
   │ 2_upload/upload_multipart.py           (presigned multipart upload)
   ▼
Archetype AI platform: Activity Detection batch job
   │ 3_batch_jobs/create_activity_detection_job.py
   ▼
outputs/<job>/*.jsonl                       {"line_index": 0, "prediction": "..."}
   │ 4_download_outputs/download_outputs.py → 5_view_results/view_results.py
   ▼
natural-language narratives
```

Every step has a Python script, a shell script (`*.sh`) and a curl walkthrough (`*_curl.md`).

## Dataset

The **GHOST-IoT** dataset: WiFi flows captured on the wlan0 interface of a real smart-home gateway (EU Horizon 2020 GHOST project). See [Data attribution](#data-attribution).

- File: `data/wlan0_ipv4_flows_db.csv`, 3,399 bidirectional IPv4 flows
- Devices: 11 MAC addresses (anonymized)
- Dates: 2019-10-10 to 2019-10-20 UTC

The data is from October 2019, so "yesterday" is pinned to **2019-10-19 UTC**, the last full day. Pass `--date YYYY-MM-DD` to either prepare script for another day.

| Column | Description |
|--------|-------------|
| `mac_a`, `mac_b` | MAC addresses of the two endpoints (anonymized) |
| `prot` | Application protocol (HTTP, HTTPS, DNS, WhatsApp, ApplePush, mDNS, ...) |
| `tran_prot` | Transport protocol (`6`=TCP, `17`=UDP, `1`=ICMP) |
| `port_a`, `port_b` | Ports |
| `ts_start`, `ts_end` | Unix epoch seconds (UTC) |
| `packets_a`, `bytes_a`, `packets_b`, `bytes_b` | Counters in each direction |

The full field dictionary is `Description of fields.pdf` in the [dataset repository](https://github.com/gspathoulas/ghost-iot-dataset).

## 1. Setup

```bash
git clone https://github.com/archetypeai/batch-example-activity-detection-ghost-iot.git
cd batch-example-activity-detection-ghost-iot

cp .env.example .env          # then set ATAI_API_KEY
python3 -m venv myenv
source myenv/bin/activate
pip install requests
```

`.env`:

```
ATAI_API_KEY=your-api-key-here
ATAI_API_ENDPOINT=https://platform.u1.archetypeai.app
```

The endpoint may be the bare root or end in `/api`. Every script calls `{root}/api/...`.

Variables already exported in your shell win over `.env`, and the scripts print a warning when the two differ. If requests go somewhere unexpected, `unset ATAI_API_KEY ATAI_API_ENDPOINT`.

For the curl walkthroughs, export the same values plus the base URL:

```bash
export ATAI_API_KEY="your-api-key"
export ATAI_API_ENDPOINT="https://platform.u1.archetypeai.app"
export BASE_URL="$ATAI_API_ENDPOINT/api"
```

## 2. Prepare the prompts

Activity Detection reads JSONL, one record per prompt: `system`, `instruction`, `prompt`, and an `inputs` array for the evidence. Each record here puts the question context in `prompt` and the day's raw flow log in `inputs[0].data`:

```json
{
  "system": "You are a network security analyst reviewing smart-home WiFi traffic...",
  "instruction": "Analyze the attached flow log and describe what happened on the home network on this day...",
  "prompt": "Date: 2019-10-19 UTC. Scope: home wlan0 interface. Flow count: 78.",
  "inputs": [{
    "type": "text",
    "format": "plain",
    "data": "Flow log fields (pipe-separated): time_utc|mac_a|mac_b|prot|tran|port_a|port_b|bytes_a|bytes_b|pkts_a|pkts_b. ...\n\n15:55:11|ebd1a7fa8544|e323b826aa71|DHCP|17|67|68|0|900|0|3\n..."
  }]
}
```

**Raw flows only, no pre-aggregation.** The scripts filter the CSV by date (and by device) and serialize the rows. Totals, peak times, the busiest device and device types are Newton's job, so the narrative is Newton's analysis, not a paraphrase of ours.

```bash
python 1_prepare_data/prepare_home_level_jsonl.py      # data/ghost_iot_home_yesterday.jsonl     (1 record)
python 1_prepare_data/prepare_device_level_jsonl.py    # data/ghost_iot_devices_yesterday.jsonl  (5 records, one per active device)
```

Both files are already in `data/`, and these commands reproduce them exactly.

## 3. Upload

```bash
python 2_upload/upload_multipart.py data/ghost_iot_home_yesterday.jsonl
python 2_upload/upload_multipart.py data/ghost_iot_devices_yesterday.jsonl
# or: ./2_upload/upload_multipart.sh data/ghost_iot_home_yesterday.jsonl
```

The file name becomes the file's id. If a file with that name already exists in your org, the upload is skipped and the existing file is used ([§9.4](#94-409-conflict-on-re-upload)). curl walkthrough: [2_upload/upload_multipart_curl.md](2_upload/upload_multipart_curl.md).

## 4. Run the jobs

```bash
# Home: 1 prompt -> 1 narrative
python 3_batch_jobs/create_activity_detection_job.py --file ghost_iot_home_yesterday.jsonl

# Devices: 5 prompts -> 5 narratives
python 3_batch_jobs/create_activity_detection_job.py --file ghost_iot_devices_yesterday.jsonl

# Same job on Newton C 2.5.1
python 3_batch_jobs/create_activity_detection_job.py --file ghost_iot_devices_yesterday.jsonl \
  --model-variant newton/c:2.5.1-8b-base
```

The script creates the job, polls it to a terminal status, prints the event log, and prints the download command. A job takes about 5 minutes, mostly queueing and model loading; the home record itself takes 37 s on C 2.5.1 and 17 s on C 2.6.

The request it sends:

```json
{
  "name": "ghost-iot-home-yesterday-activity-detection",
  "pipeline_type": "batch",
  "pipeline_key": "activity-detection",
  "inputs": {"worker.data": [{"file_id": "ghost_iot_home_yesterday.jsonl"}]},
  "parameters": {"worker": {"parallelism": 1, "config": {
    "model_variant": "newton/c:2.6.0-8b-fp8-base",
    "generation": {"do_sample": true, "max_new_tokens": 1024, "temperature": 0.7,
                   "top_k": 20, "top_p": 0.8, "repetition_penalty": 1}
  }}}
}
```

`--model-variant` sets `config.model_variant`; the shell script reads it from `MODEL_VARIANT`.

**Long runs:** the upload script, the job script and `run_multihome.py` take `--background [LOG]`. It relaunches the same command under `nohup` (it keeps going if the terminal closes) and, on macOS, `caffeinate -i` (the Mac doesn't sleep until it ends), prints the pid and the `tail -f` and `kill` commands, and writes all output to LOG (default `outputs/logs/<script>.log`). Stopping the script doesn't cancel a job already on the platform: cancel it with `POST /api/batch/jobs/{job_id}/cancel`.

```bash
python 3_batch_jobs/create_activity_detection_job.py --file ghost_iot_devices_yesterday.jsonl --background
tail -f outputs/logs/activity_detection_job.log
```

curl walkthrough: [3_batch_jobs/create_activity_detection_job_curl.md](3_batch_jobs/create_activity_detection_job_curl.md).

## 5. Download

```bash
python 4_download_outputs/download_outputs.py <home_job_id>   outputs/home
python 4_download_outputs/download_outputs.py <device_job_id> outputs/devices
# or: ./4_download_outputs/download_outputs.sh <job_id> outputs/home
```

Each output line is `{"line_index": 0, "prediction": "..."}`, in input order. A record that failed has `"prediction": null` and an `"error"`. curl walkthrough: [4_download_outputs/download_outputs_curl.md](4_download_outputs/download_outputs_curl.md).

## 6. View the narratives

```bash
python 5_view_results/view_results.py outputs/home
python 5_view_results/view_results.py outputs/devices --input data/ghost_iot_devices_yesterday.jsonl   # labels each by MAC
```

An excerpt of the home narrative (C 2.5.1):

> **Overall Activity Level** — Flow Count: 78 flows ... concentrated activity in a single burst, suggesting a user-initiated session or application launch. **Dominant Application Protocols** — HTTP/HTTPS, DNS and mDNS, with WhatsApp (port 5222), Apple Push Notification Service (port 5223) and IMAPS ...

Check the numbers in a narrative against the data before relying on them. The models describe the mix of protocols and devices well but often misstate totals and time spans (for example, a 100-second burst described as 13 minutes).

## 7. Scale: multi-home map-reduce

A real deployment sees many homes, people and devices, and far more flows than fit in one record. `run_multihome.py` handles that on synthetic data generated from the GHOST-IoT flows and `data/topology.json` (3 homes, 6 people, 24 devices):

```bash
# 1. Synthesize a multi-home CSV (reproducible with --seed)
python 1_prepare_data/generate_synthetic_multihome_csv.py --target-size-mb 12 \
  --output data/wifi_flows_multihome_12mb.csv

# 2a. Quick run: one home, detached (about 1 hour)
python run_multihome.py --csv data/wifi_flows_multihome_12mb.csv --run quick \
  --homes home_a --background
tail -f outputs/logs/run_multihome.log

# 2b. Full run: all three homes (about 1 h 15 min; see below)
python run_multihome.py --csv data/wifi_flows_multihome_12mb.csv --run mh12 --background

# 3. Read the results (replace quick with your --run)
python 5_view_results/view_results.py data/multihome/quick/predictions_house_day.jsonl \
  --manifest data/multihome/quick/manifest_house_day.jsonl
python 5_view_results/view_results.py data/multihome/quick/predictions_user_day.jsonl \
  --manifest data/multihome/quick/manifest_user_day.jsonl
```

The stages, each one batch job:

| Stage | Input record | Records, 12 MB, 3 homes | `--homes home_a` |
|---|---|---|---|
| `chunks` | ≤16 KB of one device's flows in one hour (`--max-chunk-bytes`) | 716 | 267 |
| `bucket_a` | up to 3 chunk narratives of one device-hour | 576 | 192 |
| `bucket_a2` | up to 4 `bucket_a` partials | 576 | 192 |
| `device_hour` | all partials of one device-hour → one narrative | 576 | 192 |
| `super_hour` | 4 device-hour narratives | 144 | 48 |
| `device_day` | 6 super-hours → one narrative per device | 24 | 8 |
| `user_day`, `house_day` | a person's devices / a house's devices | 6, 3 | 2, 1 |

**Every flow is used.** Chunking splits each device-hour at the byte cap and never samples or truncates; every later stage folds narratives from the stage before. Only the first stages grow with the data: a 1 GB CSV makes about 23,000 chunks, folded by `bucket_a` and `bucket_a2` into the same 576 device-hours.

**Time is set by records, not megabytes.** Every record writes a narrative of up to 1,024 tokens (2,048 from `device_day` on), and every input has 24 devices × 24 hours, so even a tiny CSV makes 576 device-hours to fold. Each later stage takes at least 5–10 minutes however few records it has, so the seven reduce stages add 45–60 minutes. `--homes home_a` keeps a third of the records at every stage.

**One job per stage.** Each stage's input is one JSONL, so output `line_index` equals the input line and every stage joins to its manifest by position. Each stage's job id is saved in `data/multihome/<run>/jobs.json` as soon as the job is created. Rerunning the same command (with the same `--homes`) skips finished stages and waits on a job that is still running, so a run survives a closed terminal or a reboot. Artifacts go to `data/multihome/<run>/` and `outputs/<run>/`.

Measured on Prod (November 2026), 12 MB CSV:

| Run | Model | `chunks` stage | Whole run |
|---|---|---|---|
| `--homes home_a` (902 records) | C 2.6 | 14 min (267 chunks) | 1 h 00 min |
| all 3 homes (2,621 records) | C 2.6 | 22 min (716 chunks) | about 1 h 10 min |
| all 3 homes (2,621 records) | C 2.5.1 | 5 h 15 min (716 chunks) | 6 h 22 min |

On C 2.6 every stage after `chunks` took 5–7 minutes, mostly queueing and model loading. Times depend on how busy the platform is. At 1 GB (about 23,000 chunks), expect several hours on C 2.6 and days on C 2.5.1; on C 2.6, `--max-chunk-bytes 32768` halves the chunk count.

## 8. Cleanup

```bash
python cleanup.py                 # dry run: lists local artifacts and uploaded files
python cleanup.py --local         # delete generated local files (the reference inputs are kept)
python cleanup.py --remote        # delete uploaded files listed in data/uploaded_file_ids.jsonl
```

## 9. Common pitfalls

A job's status alone doesn't tell you it worked. `COMPLETED` can mean some or all inputs failed, and a run can succeed and still return garbage. Read the job's `error`, the event log, and at least the first prediction.

### 9.1 `COMPLETED` with failed inputs

```
!! ERROR Input (ghost_iot_devices_yesterday.jsonl) rejected: could not read input (JosS3Error)
   FAILED Job completed in 341s but all inputs failed and no output was produced
 Error: {'failed_inputs': 1, 'outputs': 0, 'reason': '1 input(s) failed during processing', ...}
```

The job script prints `Error:` whenever the job reports one. Here the input file had never finished uploading ([§9.4](#94-409-conflict-on-re-upload)).

### 9.2 Wrong content type at upload

JSONL must be uploaded as `application/x-ndjson`; the upload scripts pick it from the `.jsonl` extension. A JSONL file uploaded as `text/csv` or `text/plain` fails at read time. Re-upload it under a new name.

### 9.3 Oversized records: garbage on C 2.5.1, `inference error` on C 2.6

Measured on Prod (November 2026) with 5 real chunk records at each size, `max_new_tokens=1024`:

| Flow log in `inputs[0].data` | C 2.5.1 | C 2.6 FP8 |
|---|---|---|
| 16 KB | 5/5 real narratives | 5/5 real narratives |
| 32 KB | **0/5: garbage, no error** | 5/5 real narratives |
| 64–100 KB | 0/5: garbage, no error | **0/5: rejected, `"error": "inference error"`** |

C 2.5.1 runs with `context_length=16,384` tokens, so the input may use 15,360 tokens when `max_new_tokens` is 1,024. A longer record is truncated from the end, which removes the instruction, and the model continues the flow log instead of answering. It comes back as a "successful" prediction that is a fragment of the table (`'2'`, `'|100|5|7'`, `'0|0|1'`). The only signal is a warning in the event log:

```
WARN inference.truncation  Batch 1: 5 items exceed input_token_cap=15,360 (context_length=16,384 − max_new_tokens=1,024) ...
```

C 2.6 takes about twice the input and rejects a record that is too long, so the failure shows as `"error": "inference error"`.

Keep records at 16 KB or less on C 2.5.1 (a 16 KB chunk of flow log is about 15,000 tokens, so it is close to the cap; the event log reports records "within 256 tokens of it") and 32 KB or less on C 2.6. Raising `max_new_tokens` lowers the input cap by the same amount. `3_batch_jobs/audit_predictions.py <predictions.jsonl>` flags fragments, table rows and other signs of this failure.

### 9.4 409 Conflict on re-upload

File names are unique per org: uploading a name that exists returns 409, and the scripts skip the upload and reuse the existing file. **That file may hold different content** if you changed the local file since. To replace it, rename the local file, or delete the old one with `python cleanup.py --remote` (`DELETE /api/files/{file_uid}`).

An upload that dies after `initiate` leaves its name in `UPLOADING`, and that name returns 409 until the platform expires it. The upload scripts abort on failure to prevent this; if it happens anyway, upload under a new name.

### 9.5 Reading the event log

Success looks like:

```
Processing ghost_iot_home_yesterday.jsonl (6.1 KB)
Batch 0: processed 1 items (1 success, 0 failed)
Processed 1 items (1 success, 0 failed) from ghost_iot_home_yesterday.jsonl in 37.9s
Job completed in 322s
```

Look for `failed` counts above zero, `completed_with_errors`, `rejected`, `Job completed ... with some failed inputs`, and `inference.truncation` warnings that report items exceeding the cap ([§9.3](#93-oversized-records-garbage-on-c-251-inference-error-on-c-26)).

## Why this design

**One home-level prompt, not hourly chunks.** One prompt gives one compact story for the day. Section 7 shows the hourly, per-device version.

**MAC only, no device labels.** MACs are anonymized and there is no device directory, so Newton gets each device's protocols, ports and traffic and infers what it is. Whether it guesses right is part of what the example shows.

**Rows with an empty MAC are dropped,** so no phantom "blank" device appears.

## API reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/files/uploads/initiate` | POST | Start an upload; returns presigned part URLs |
| `{presigned part url}` | PUT | Upload a part directly to storage |
| `/api/files/uploads/{upload_id}/complete` | POST | Finish the upload with the part ETags |
| `/api/files/uploads/{upload_id}/abort` | POST | Cancel an upload |
| `/api/files/{file_uid}` | GET, DELETE | Read or delete a file |
| `/api/batch/jobs` | POST | Create a batch job |
| `/api/batch/jobs/{job_id}` | GET | Job status, `error`, `input_progress` |
| `/api/batch/jobs/{job_id}/events` | GET | Event log |
| `/api/batch/jobs/{job_id}/outputs` | GET | Output files; each `data.ref` is an `/api/files/{file_uid}/content` path |
| `/api/files/{file_uid}/content` | GET | Download a file (with your API key) |

Lists are cursor-paginated, newest first: `{data, has_more, next_cursor}`; fetch the next page with `?after=<next_cursor>`.

## Data attribution

All network data used here comes from the **GHOST-IoT** dataset, captured on a real smart-home gateway by the EU Horizon 2020 project GHOST (Safe-Guarding Home IoT Environments with Personalised Real-time Risk Control):

- **Authors:** Michail Anagnostopoulos, Georgios Spathoulas, Brais Viaño, Javier Augusto-Gonzalez.
- **Dataset:** <https://github.com/gspathoulas/ghost-iot-dataset> (`data.rar`, 12 CSV files; field definitions in `Description of fields.pdf`).
- **Paper:** *Tracing Your Smart-Home Devices Conversations: A Real World IoT Traffic Data-Set.* Sensors **2020**, 20(22), 6600. [doi:10.3390/s20226600](https://doi.org/10.3390/s20226600)
- **Licence:** none stated. The dataset's README presents it as a public dataset and asks that work using it cite the paper above.
- **No endorsement.** The authors are not involved in this example and do not endorse it.

**What is included, and what was changed:**
- **`data/wlan0_ipv4_flows_db.csv`:** included unchanged. It is 1 of the dataset's 12 files (WiFi interface, IPv4 flows) and is byte-identical to the copy in `data.rar`.
- **`data/ghost_iot_home_yesterday.jsonl`, `data/ghost_iot_devices_yesterday.jsonl`:** derived. The flows that started on 2019-10-19 UTC, without rows that have an empty MAC, with 11 of the columns serialized as pipe-separated text and start times reduced to UTC time of day. `1_prepare_data/prepare_{home,device}_level_jsonl.py` rebuilds them exactly.
- **Synthetic multi-home CSVs (section 7):** not included; you generate them. `1_prepare_data/generate_synthetic_multihome_csv.py` builds each row from a randomly chosen row of `wlan0_ipv4_flows_db.csv` (protocol, ports, counters and the other flow statistics), jitters bytes and packets by up to ±20 %, replaces the MAC addresses with the fictional homes and devices in `data/topology.json`, and moves start and end times to 2019-10-19 following a per-device-type daily profile. The same `--seed` gives the same file.

The code is under its own licence: see [License](#license).

## License

Apache 2.0 (code). The dataset is subject to its own terms; see Data attribution.
