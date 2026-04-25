# Team Seed Sharding — Distribution Blindspot Fixes

- **Date:** 2026-04-26
- **Branch:** `subagent/team-seed-sharding`
- **Builds on:** `docs/superpowers/specs/2026-04-20-team-seed-sharding-design.md`
- **Source analysis:** Notion page "팀 Seed Sharding 전략 — 분포 사각지대 분석 (2026-04-26)"

## Problem

The 2026-04-20 sharding design guarantees disjoint indices across six members but the live preset (`fixed_target.sfp = nic_rail_0/sfp_port_0`, `sc_default_count: 0`, `target_cycling: false`, `strategy: uniform`) collapses all 6,000 samples onto a single (rail, port) corner. Three additional gaps surface in the analysis:

1. **Distribution scope (P0):** 9 of 10 SFP targets and all SC samples receive zero coverage.
2. **Sampling strategy (P1):** independent uniform marginals leave joint corners empty even within the single trial_1 target.
3. **Audit drift (P1, P2):** simulator-stage failures are invisible to the ledger; `dirty:` git trees and mismatched `preset_hash` values are warnings only.
4. **Stale state (P3):** the live `seed_ledger.yaml` already contains a `dirty:` entry at `start_index=20`, leaving 0–19 as a permanent reproducibility hole.

## Goals

1. Cover the full SFP×port grid (10 targets) and add SC coverage in the same collection round.
2. Replace independent uniform draws with Latin Hypercube on the 18-D pose design.
3. Refuse submits that cannot be reproduced (`dirty:` git, drifted `preset_hash`) unless an explicit environment override is set.
4. Reconcile the ledger against the queue's `failed/` directory so per-sample failures are recorded next to the claim that produced them.
5. Reset the existing M0 ledger entry and archive the stale 20–1019 queue files so the new round starts from `start_index=0`.

## Non-Goals

- Cross-batch LHS continuation. Each `submit` re-stratifies its own batch; cross-batch joins remain best-effort.
- A new dashboard for distribution coverage. The reconcile CLI prints per-target counts to stdout; visualisation is deferred.
- Automated git operations or commit-on-submit. The `dirty:` gate refuses; cleanup is the operator's responsibility.
- Migrating existing 4-digit queue files. The 2026-04-20 spec already covers mixed-width handling.

## Architecture

### Invariants (additions to the 2026-04-20 spec)

5. **Distribution invariant:** `target_cycling=true` + no `fixed_target` ⇒ every member's slice covers the full target cycle uniformly when `count % len(cycle) == 0`.
6. **Reproducibility gate:** every appended ledger entry carries a clean `git_sha` (no `dirty:` prefix) and a `preset_hash` that matches the most recent same-`task_type` entry, unless the operator opted in via env.
7. **Audit completeness:** after a collection round, `validated_count = count - len(failed_indices)` is recorded on every entry that has been reconciled.

### Layering

```
configs/team/preset.yaml        ← strategy=lhs, target_cycling=true, sc_default_count=100
configs/team/seed_ledger.yaml   ← reset to entries: []

aic_collector.team_preset
  ├── load_preset                  (validates target_cycling=true when fixed_target absent)
  ├── submit_team_claim            (calls _enforce_repro_gates before append_claim)
  ├── _enforce_repro_gates  NEW    (dirty git + preset_hash drift checks)
  └── reconcile_ledger_with_queue  NEW (failed/ scan → failed_indices, validated_count)

aic_collector.webapp
  └── _team_submit                 (catches PresetError gates, surfaces banner)

scripts/archive_legacy_queue.sh    NEW (move stale 4-digit / out-of-policy files)
```

### Changed Files

| File | Change | ~LoC |
|---|---|---|
| `configs/team/preset.yaml` | strategy/scene/tasks rewrite | 6 |
| `configs/team/seed_ledger.yaml` | reset to `entries: []` | 1 |
| `src/aic_collector/team_preset.py` | gates, reconcile, scene validation | 140 |
| `src/aic_collector/webapp.py` | banner for gate errors | 30 |
| `tests/test_team_preset.py` | gates + reconcile | 120 |
| `tests/test_training_sampler.py` | cycling + LHS distribution checks | 60 |
| `tests/test_webapp_team_mode_state.py` | gate banner surface | 20 |
| `scripts/archive_legacy_queue.sh` | new helper | 20 |

Total ~400 LoC. No public API removals; new `team_preset` symbols are additive.

## Components

### C1. Preset rewrite (`configs/team/preset.yaml`)

```yaml
version: 1
team:
  base_seed: 42
  shard_stride: 100000
  index_width: 6
sampling:
  strategy: lhs                       # was: uniform
  ranges:
    nic_translation: [-0.0215, 0.0234]
    nic_yaw:         [-0.1745, 0.1745]
    sc_translation:  [-0.06,   0.055]
    gripper_xy:      0.002
    gripper_z:       0.002
    gripper_rpy:     0.04
scene:
  nic_count_range: [1, 1]
  sc_count_range:  [1, 1]
  target_cycling:  true               # was: false; fixed_target block removed
tasks:
  sfp_default_count: 1000
  sc_default_count:  100              # was: 0
members:
  - {id: M0, name: "alice"}
  - {id: M1, name: "bob"}
  - {id: M2, name: "carol"}
  - {id: M3, name: "dave"}
  - {id: M4, name: "eve"}
  - {id: M5, name: "frank"}
```

Per-member coverage with the new preset:
- SFP: 1000 / 10 targets = 100 samples per (rail, port). Six members → 600 per target.
- SC: 100 / 2 targets = 50 samples per target. Six members → 300 per target.

### C2. Scene validation (additive)

`load_preset` already validates `scene` as a mapping. Add a single check inside `_validate_scene` (new helper):

- If `fixed_target` is absent or null **and** `target_cycling` is `false`, raise `PresetError("scene.target_cycling must be true when fixed_target is unset")`.
- If `fixed_target` is set, leave the existing trial_1-style behaviour untouched (covers the `lhs + single corner` fallback path in §Goals 2).

This guards against silently regressing back to "everything in one corner" by editing `target_cycling` alone.

### C3. Reproducibility gates (`_enforce_repro_gates`)

Called inside `submit_team_claim` immediately before `_append_claim_locked`:

```python
def _enforce_repro_gates(
    entries: list[dict[str, Any]],
    *,
    task_type: str,
    preset_hash: str,
    git_sha: str,
) -> None:
    if git_sha.startswith("dirty:") and not _env_flag("AIC_ALLOW_DIRTY"):
        raise PresetError(
            "Refusing to submit on a dirty tree. Commit or stash, "
            "or set AIC_ALLOW_DIRTY=1 to override."
        )
    same_task = [e for e in entries if e.get("task_type") == task_type]
    if same_task and same_task[-1].get("preset_hash") != preset_hash:
        if not _env_flag("AIC_ALLOW_PRESET_DRIFT"):
            raise PresetError(
                f"preset_hash drift vs latest {task_type} entry "
                f"({same_task[-1].get('preset_hash')!r} → {preset_hash!r}). "
                "Set AIC_ALLOW_PRESET_DRIFT=1 to override."
            )
```

`_env_flag(name)` reads `os.environ` and returns `True` for `{"1", "true", "yes"}` (case-insensitive). The gate is enforced under `_ledger_lock` so the comparison is race-free.

`uncommitted` (returned when git is unavailable) is **not** treated as dirty — that path is for installs without a git checkout (e.g. wheels) and the existing 2026-04-20 contract permits it.

### C4. Ledger reconciliation (`reconcile_ledger_with_queue`)

```python
def reconcile_ledger_with_queue(
    ledger_path: Path,
    queue_root: Path,
) -> list[dict[str, Any]]:
    """Scan queue_root/<task>/failed/ and annotate every ledger entry whose
    [start_index, start_index + count) window intersects a failed file.

    Idempotent: re-running with no new failures yields the same payload.
    Returns the updated entries list."""
```

Behaviour:
1. Acquire `_ledger_lock`.
2. For each `task_type` present in the ledger, list `failed/config_<task>_NNNNNN.yaml` filenames once and cache the indices.
3. For each entry, compute `failed_in_range = sorted(idx for idx in cache[task_type] if entry.start_index <= idx < entry.start_index + entry.count)`.
4. Set `entry["failed_indices"] = failed_in_range`, `entry["validated_count"] = entry["count"] - len(failed_in_range)`, `entry["reconciled_at"] = _iso_utc_now()`.
5. Atomic rewrite via `_atomic_rewrite`.

Trigger: standalone CLI under the existing module — `python -m aic_collector.team_preset reconcile --ledger configs/team/seed_ledger.yaml --queue-root configs/train`. No prefect-flow hook in this round; the operator runs it after a collection batch finishes.

### C5. Webapp banner

`_team_submit` already converts `PresetError` to a Streamlit error. The added gate messages are user-readable; only addition is a one-line hint linking to `docs/superpowers/specs/2026-04-26-team-seed-sharding-blindspot-fixes-design.md` so future operators know the override env vars exist.

No new widgets. No UI override toggle. The override is environment-only by design — UI clicks should not bypass reproducibility.

### C6. Stale state cleanup

- Rewrite `configs/team/seed_ledger.yaml` to `entries: []`.
- `scripts/archive_legacy_queue.sh`:
  - Source: `configs/train/{sfp,sc}/{pending,running,done,failed,legacy}/`.
  - Target: `configs/train/_archive_2026-04-26/<task>/<state>/`.
  - Move every `config_<task>_NNNN.yaml` (4-digit) and any 6-digit file with index in `[0, 100000)` (the M0 slot) into the archive.
  - Idempotent: missing source dirs are skipped; existing archive dir is appended to.

## Data Flow

### Submit (with gates)

```
1. _ledger_lock acquired
2. start_idx = next_start_index_in_slot(...)
3. assert start_idx + count <= slot_end                     ← unchanged
4. _enforce_repro_gates(entries, task, preset_hash, git_sha) ← NEW
5. entry_id = _append_claim_locked(...)                     ← unchanged
6. plans = sample_scenes(...)  / write_plans(...)           ← unchanged
```

### Reconcile (post-collection)

```
operator: $ python -m aic_collector.team_preset reconcile \
            --ledger configs/team/seed_ledger.yaml \
            --queue-root configs/train

1. lock ledger
2. read failed/ for each task_type
3. for each entry: compute failed_indices and validated_count
4. atomic rewrite
5. print "M0/sfp: 1000 written, 23 failed, 977 validated" lines
```

### Failure surfaces

| Failure | Response |
|---|---|
| dirty git tree, no `AIC_ALLOW_DIRTY` | `PresetError`; banner; submit aborted before ledger touch |
| preset_hash drift, no `AIC_ALLOW_PRESET_DRIFT` | `PresetError`; banner; submit aborted |
| `target_cycling: false` + no `fixed_target` | `PresetError` at `load_preset`; webapp shows top-level banner |
| `reconcile` against missing queue_root | logs warning, skips that task_type, exits 0 |
| `reconcile` against an entry that was archived | archived files no longer in `failed/`, so `failed_indices` shrinks. Already-recorded reconciled entries are overwritten with the smaller list — operator should reconcile **before** archiving. |

## Edge Cases

- **Mid-round preset edit** (e.g. someone tweaks `nic_yaw` range): drift gate refuses next submit. Operator either reverts the edit or accepts the drift via env flag.
- **First-ever submit** (empty ledger): drift gate has no prior entry to compare against, so it passes; the dirty gate still applies.
- **Reconcile re-run**: idempotent — the same failed file count produces the same `failed_indices`. `reconciled_at` updates each run.
- **Mixed reconciled / unreconciled entries**: `validated_count` is absent on legacy entries; downstream readers treat absence as "not yet reconciled".
- **Member submits both SFP and SC**: drift gate compares against the latest entry of the same `task_type` only, so SFP and SC drift independently.
- **`AIC_ALLOW_DIRTY=1` set but tree happens to be clean**: env flag is a permission, not a directive — clean tree submits normally.

## Testing

### Unit (`tests/test_team_preset.py`)

- `_enforce_repro_gates`: dirty SHA raises; clean SHA passes; drift raises; matching hash passes; both env overrides succeed.
- `_validate_scene`: `target_cycling=false` + no `fixed_target` raises; `target_cycling=true` passes; `fixed_target` set + cycling either way passes.
- `reconcile_ledger_with_queue`:
  - empty `failed/` → all entries get `failed_indices: []`, `validated_count == count`.
  - failures spanning slot boundaries → only in-window indices recorded per entry.
  - idempotent: two consecutive runs yield identical payload modulo `reconciled_at`.
- `__main__` CLI: argparse smoke test using `tmp_path`.

### Sampler (`tests/test_training_sampler.py`)

- With `target_cycling=true` and 1000 samples, every (rail, port) pair appears exactly 100 times.
- With `strategy="lhs"` and 1000 samples, no pose dimension's 10-bin histogram has a zero bin (LHS guarantee).
- Regression: `fixed_target` path still produces a length-1 cycle.

### Integration (`tests/test_webapp_team_mode_state.py`)

- Gate `PresetError` propagates as `st.error` banner; submit button stays usable for next attempt.
- Override env present → submit proceeds.

### Manual checklist (pre-merge)

1. `git status` clean → submit M0/SFP 1000 → ledger gains entry, files `000000..000999` exist.
2. Edit `preset.yaml` (range tweak), submit M1/SFP → drift error.
3. `AIC_ALLOW_PRESET_DRIFT=1 python -m … submit` → succeeds.
4. Touch a file (dirty tree), submit → dirty error.
5. After a collection run, copy a few `pending/` configs to `failed/` → run reconcile → entries updated.
6. Re-run reconcile → entries unchanged except `reconciled_at`.

## Rollout

1. Land the preset rewrite + scene validation + ledger reset in one commit. Existing collectors must `git pull` before the next submit (drift gate would block them otherwise — by design).
2. Run `scripts/archive_legacy_queue.sh` once on the operator machine. Verify `configs/train/_archive_2026-04-26/` contains the old files and the live `pending/` is empty.
3. M0 submits the first SFP batch under the new preset; verify ledger entry's `git_sha` is clean and `preset_hash` matches the file.
4. After M0 finishes, run reconcile and post the per-target counts in the team channel.
5. M1–M5 proceed.

## Open Questions

None at design time. The reconcile trigger stays manual in this round; if operators forget to run it, the worst case is a stale `validated_count`, not data loss. Promotion to a prefect-flow hook is a follow-up after we observe how often the manual step is missed.

---
**Status (2026-04-26):** Implemented in commits on `subagent/team-seed-sharding`. Rollout step 2 (`scripts/archive_legacy_queue.sh`) and step 4 (post-collection reconcile) remain manual operator actions.
