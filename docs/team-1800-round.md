# Team 1800-episode Round (Trial-sharded, 94+ Gate)

This round collects **1800 validated episodes** (≈30h at ~1m/episode) split
across three trial scenarios and six members. Every kept episode must score
**≥ 94** on its trial total; anything below is automatically re-queued.

## Round contract

| Trial   | Task | Fixed target            | Total |
| ------- | ---- | ----------------------- | ----- |
| trial_1 | SFP  | NIC rail 0 + sfp_port_0 | 600   |
| trial_2 | SFP  | NIC rail 1 + sfp_port_0 | 600   |
| trial_3 | SC   | SC  rail 1 + sc_port_1  | 600   |

Per-member workload (option A — M0 owns trial_3 alone, the rest split
trial_1+trial_2 evenly):

| Member    | trial_1 | trial_2 | trial_3 | Episodes |
| --------- | ------- | ------- | ------- | -------- |
| M0 한준모  | -       | -       | 600     | 600      |
| M1 전유진  | 120     | 120     | -       | 240      |
| M2 이진형  | 120     | 120     | -       | 240      |
| M3 정혜린  | 120     | 120     | -       | 240      |
| M4 엄창용  | 120     | 120     | -       | 240      |
| M5 이강인  | 120     | 120     | -       | 240      |

Per-member queue indices live in disjoint 100,000-wide shards (`shard_stride`
in `configs/team/preset.yaml`), so members never collide:

| Member | trial_1 start | trial_2 start | trial_3 start |
| ------ | ------------- | ------------- | ------------- |
| M0     | -             | -             | 0             |
| M1     | 100,000       | 100,120       | -             |
| M2     | 200,000       | 200,120       | -             |
| M3     | 300,000       | 300,120       | -             |
| M4     | 400,000       | 400,120       | -             |
| M5     | 500,000       | 500,120       | -             |

## Paths used by this round

- Preset:        `configs/team/preset.yaml`
- Ledger:        `configs/team/seed_ledger.yaml`
- Queue root:    `configs/train/{sfp,sc}/{pending,running,done,failed}/`
- Output root:   `~/aic_community_e2e_round_<TS>/` (set when the round started)
- Template:      `configs/community_random_config.yaml`

The output root must be the **same path on every member's machine** so that
the score reconciler can find every run. If members work on separate hosts,
sync the output directory back to one host before reconciling.

## 1. Initial submit (already done at round start)

```bash
AIC_ALLOW_DIRTY=1 uv run python -c "
from pathlib import Path
from aic_collector.team_preset import load_preset, submit_member_claim
preset = load_preset(Path('configs/team/preset.yaml'))
for m in preset.members:
    if m['id'] in preset.member_assignments:
        submit_member_claim(
            preset, member_id=m['id'],
            queue_root=Path('configs/train'),
            ledger_path=Path('configs/team/seed_ledger.yaml'),
            template_path=Path('configs/community_random_config.yaml'),
        )
"
```

Result: `configs/train/sfp/pending/` holds 1200 configs, `configs/train/sc/pending/`
holds 600 configs. Each ledger entry records `trial_id` and `fixed_target`.

## 2. Per-member worker (run on each collector machine)

Set the same output root everywhere:

```bash
export OUT_ROOT=~/aic_community_e2e_round_<TS>      # match the round's TS
```

### M1..M5 (SFP — both trial_1 and trial_2 land in `sfp/pending/`)

```bash
uv run aic-collector-worker \
    --root configs/train \
    --task sfp \
    --policy cheatcode \
    --output-root "$OUT_ROOT" \
    --ground-truth true \
    --use-compressed false \
    --collect-episode true \
    --timeout 300 \
    --recover
```

### M0 (SC only — owns all of trial_3)

```bash
uv run aic-collector-worker \
    --root configs/train \
    --task sc \
    --policy cheatcode \
    --output-root "$OUT_ROOT" \
    --ground-truth true \
    --use-compressed false \
    --collect-episode true \
    --timeout 300 \
    --recover
```

Workers claim configs atomically from `pending/`, run the engine, and move
each config into `done/` (success) or `failed/` (engine timeout / crash).
Real-time state lives at `/tmp/aic_worker_state.json`; full engine logs go
to `/tmp/aic_worker_run.log`.

## 3. Score reconciliation (after a member's queue empties)

```bash
uv run python -m aic_collector.team_preset reconcile-score \
    --ledger configs/team/seed_ledger.yaml \
    --output-root "$OUT_ROOT" \
    --threshold 94
```

Output (one line per ledger entry):

```
M0/sfp: 300 claimed, 287 validated (>= 94.0), 11 low-score, 2 missing
M1/sfp: 300 claimed, 296 validated (>= 94.0), 4 low-score, 0 missing
...
```

`missing` indices are configs that haven't run yet (still `pending/` or
crashed before scoring). Resolve those first by re-running the worker with
`--recover`. Once `missing == 0` for every entry, move to step 4.

## 4. Re-queue low-score replacements

For each member with `low-score > 0`:

```bash
AIC_ALLOW_DIRTY=1 uv run python -m aic_collector.team_preset requeue-low-score \
    --preset configs/team/preset.yaml \
    --ledger configs/team/seed_ledger.yaml \
    --queue-root configs/train \
    --template configs/community_random_config.yaml \
    --member M0
```

This appends N replacement configs (N = that member's `low_score_indices`
count) into `pending/` at the next free index inside that member's slot, with
the same trial fixed_target. Run the worker again.

Repeat **steps 2 → 3 → 4** until every entry's
`score_validated_count == count` (i.e., `low-score == 0` and `missing == 0`).
At that point the round has 1800 episodes all ≥ 94.

## 5. Final tally

```bash
uv run python - <<'PY'
import yaml
from pathlib import Path
d = yaml.safe_load(Path("configs/team/seed_ledger.yaml").read_text())
totals = {"validated": 0, "low": 0, "missing": 0, "claimed": 0}
for e in d["entries"]:
    if "score_validated_count" not in e:
        continue
    totals["claimed"] += e["count"]
    totals["validated"] += e["score_validated_count"]
    totals["low"] += len(e.get("low_score_indices") or [])
    totals["missing"] += len(e.get("missing_indices") or [])
print(totals)
PY
```

Goal: `{"validated": 1800, ...}` with `low == 0` and `missing == 0`.

## Multi-PC HF upload coordination

When members upload from different PCs to the same HF dataset repo, three
helpers (under `python -m aic_collector.team_preset`) keep the round honest:

### `aggregate-manifests` — round-wide progress

Each PC has its own `manifest.jsonl`. Pull them all to one host (rsync) and
get a state rollup:

```bash
python -m aic_collector.team_preset aggregate-manifests \
    --manifest /path/to/pc1/manifest.jsonl \
    --manifest /path/to/pc2/manifest.jsonl \
    ...
```

Output lists state counts (uploaded / remote_verified / upload_failed / ...)
plus the offending item_ids for any in a failure state.

### `verify-repo` — what's actually on HF Hub

After uploads finish, list the repo's real file inventory and cross-check
the ledger's expected sample_index window:

```bash
python -m aic_collector.team_preset verify-repo \
    --ledger configs/team/seed_ledger.yaml \
    --repo-id <org>/<dataset>
```

Reports per-task `expected / present / missing / extra / below_min`. Missing
indices are the ones to retry; non-zero `extra` would indicate uploads
outside the ledger (probably leftovers from a previous round).

The verifier is **fail-closed**: a missing ledger, malformed entries, or an
empty `entries:` list always produces `ok: false` with a `ledger errors:`
block. An empty repo paired with a missing ledger will not silently pass.

`--min-files-per-item N` (default 1) raises the per-index threshold for
"present". With the default, `ok` only guarantees at-least-one-file per
expected index — **not** artifact completeness. To gate on completeness,
set N to your expected per-item artifact count:

```bash
python -m aic_collector.team_preset verify-repo \
    --ledger configs/team/seed_ledger.yaml \
    --repo-id <org>/<dataset> \
    --min-files-per-item 4    # e.g. parquet + meta + episode + tags
```

Indices with fewer files than the threshold are listed under
`below-min indices` in the output.

`huggingface_hub` 설치와 인증된 token이 필요합니다(`HF_TOKEN` 또는
`huggingface-cli login`). 운영자가 쉽게 설정할 수 있도록 repo id를 셸 변수
하나에 두고, 라운드 시작 전에 접근을 확인합니다.

```bash
export AIC_HF_REPO_ID=org_or_user/dataset
export HF_TOKEN=hf_...

uv run python - <<'PY'
import os
from huggingface_hub import HfApi

repo_id = os.environ["AIC_HF_REPO_ID"]
files = HfApi().list_repo_files(repo_id=repo_id, repo_type="dataset")
print(f"HF 접근 확인 완료: {repo_id} ({len(files)}개 파일 확인)")
PY

uv run python -m aic_collector.team_preset verify-repo \
    --ledger configs/team/seed_ledger.yaml \
    --repo-id "$AIC_HF_REPO_ID" \
    --min-files-per-item 4
```

### `retry-uploads` — re-issue failed pushes

Run on each PC where a manifest contains `upload_failed` /
`remote_verify_failed` / `stage_failed` events. Looks at every such item's
last event for a still-on-disk staged folder and re-invokes
`record_upload_and_verify` with bounded retries:

```bash
python -m aic_collector.team_preset retry-uploads \
    --manifest ~/aic_round_manifest.jsonl \
    --repo-id <org>/<dataset> \
    --max-attempts 3 \
    --backoff-seconds 2
```

If the staged folder was already cleaned up after a successful upload, the
helper reports `MISS` and skips that item — those need full re-collection
through the worker, not just an upload retry.

## Common pitfalls

- **Don't run more than one worker on the same `--root` simultaneously per
  task type.** Atomic claim is per file and works across processes, but
  Prefect server contention from multiple workers on one machine causes
  spurious timeouts.
- **`AIC_ALLOW_DIRTY=1`** is required if the working tree has uncommitted
  changes (the ledger gate refuses dirty submits otherwise). Land changes
  on `main` once the round is verified.
- **Output root drift.** Score reconcile only sees runs in the directory you
  pass; if a member writes to a different path their indices will appear as
  `missing`. Sync first, reconcile second.
- **Re-queue index growth.** Every retry consumes new indices in the
  member's 100k-wide slot, so even with worst-case 50% retry rate a member
  can retry ≈99,400 times before slot exhaustion.
