# Team 1800-episode Round (Trial-sharded, 95+ Gate)

This round collects **1800 validated episodes** (≈30h at ~1m/episode) split
across three trial scenarios and six members. Every kept episode must score
**≥ 95** on its trial total; anything below is automatically re-queued.

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
    --threshold 95
```

Output (one line per ledger entry):

```
M0/sfp: 300 claimed, 287 validated (>= 95.0), 11 low-score, 2 missing
M1/sfp: 300 claimed, 296 validated (>= 95.0), 4 low-score, 0 missing
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
At that point the round has 1800 episodes all ≥ 95.

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
