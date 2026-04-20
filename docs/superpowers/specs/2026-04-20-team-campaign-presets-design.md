# Team Campaign Presets for Trials 1-3

- **Date:** 2026-04-20
- **Scope:** `configs/team/presets/*.yaml`, `src/aic_collector/team_preset.py`, `src/aic_collector/webapp.py`, team-mode ledger/progress logic
- **Status:** Design approved, pending implementation plan

## Problem

Current team mode is a single hard-coded campaign:

- It reads only `configs/team/preset.yaml`.
- It is effectively `trial_1`-only.
- It allows only SFP generation in team mode.
- The numeric default (`sfp_default_count: 1000`) is used as a per-submit default, not an explicit campaign target.

The requested workflow is different:

1. Ship three selectable team presets for `trial_1`, `trial_2`, and `trial_3`.
2. Let operators select the active preset from the webapp instead of renaming files.
3. Keep a campaign goal of `1000` samples, but default each submit to smaller batches of `100`.
4. Support `trial_3` as an SC campaign while preserving seed-sharding, slot safety, and ledger auditability.

## Goals

1. Add preset selection UI backed by `configs/team/presets/*.yaml`.
2. Model each preset as a single team campaign: one `trial_id`, one `task_type`, one fixed target, one total goal.
3. Preserve disjoint per-member slot allocation and append-safe submit behavior.
4. Make campaign progress explicit: team members should see goal, claimed count, and remaining count.
5. Preserve backward compatibility for existing users:
   - no `presets/` directory -> current single-file team mode still works
   - no team preset at all -> solo mode remains unchanged

## Non-Goals

- Multi-trial preset bundles in a single YAML file.
- Automatic preset creation/editing from the web UI.
- Per-member target quotas beyond the existing slot partition.
- Changing ledger storage location or introducing one ledger file per trial.
- Retrofitting legacy `configs/team/preset.yaml` into the new campaign UI if the new preset catalog is absent.

## Decisions

### D1. Preset Catalog

New campaign presets live in:

```text
configs/team/presets/trial_1.yaml
configs/team/presets/trial_2.yaml
configs/team/presets/trial_3.yaml
```

Webapp precedence:

1. If `configs/team/presets/` contains one or more `.yaml` files, attempt campaign preset mode.
2. If at least one catalog preset validates, use campaign preset mode.
3. If the catalog directory is absent or contains zero `.yaml` files, and `configs/team/preset.yaml` exists, use the current legacy single-preset team mode.
4. If catalog `.yaml` files exist but none validate, show a preset error and do not silently fall back to legacy mode.
5. Else use solo mode.

This avoids ambiguous behavior when both systems exist.

### D2. Campaign Semantics

Each catalog preset represents one campaign only:

- one `trial_id`
- one `task_type`
- one `fixed_target`
- one `total_target_count`
- one `batch_default_count`

This keeps `trial_1`, `trial_2`, and `trial_3` separate in both UI and ledger progress accounting.

### D3. Batch vs Total

`total_target_count=1000` is the campaign goal.

`batch_default_count=100` is only the initial value shown in the UI for each submit. Users can reduce it or increase it up to the remaining allowed capacity. Repeated submits accumulate until the campaign goal is reached.

### D4. Trial Target Mapping

The shipped presets follow the local AIC sample config:

- `trial_1`: `task_type=sfp`, target `(rail=0, port=sfp_port_0)`
- `trial_2`: `task_type=sfp`, target `(rail=1, port=sfp_port_0)`
- `trial_3`: `task_type=sc`, target `(rail=1, port=sc_port_1)`

The first two map to `nic_card_mount_0` / `nic_card_mount_1` with `sfp_port_0`; the third maps to `sc_port_1`.

## Architecture

### Layering

```text
configs/team/presets/*.yaml
  -> campaign preset catalog

Streamlit webapp.py
  -> scans available presets
  -> lets user choose preset + member
  -> shows slot progress + campaign progress
  -> submits one batch at a time

aic_collector.team_preset
  -> loads catalog presets
  -> validates campaign schema
  -> computes slot and campaign state
  -> appends ledger claims under lock

configs/team/seed_ledger.yaml
  -> append-only audit trail shared across all campaigns
```

### Invariants

1. A single submit belongs to exactly one preset campaign.
2. `start_index` remains derived from member slot allocation, never from free-form UI input.
3. Campaign progress is counted per `preset_hash`, not merely per `trial_id`.
4. The authoritative capacity check happens under ledger lock at submit time.

## Data Model

### Campaign Preset Schema

New campaign presets use this shape:

```yaml
version: 1
campaign:
  trial_id: trial_1
  task_type: sfp
  total_target_count: 1000
  batch_default_count: 100
team:
  base_seed: 42
  shard_stride: 100000
  index_width: 6
sampling:
  strategy: uniform
  ranges:
    nic_translation: [-0.0215, 0.0234]
    nic_yaw: [-0.1745, 0.1745]
    sc_translation: [-0.06, 0.055]
    gripper_xy: 0.002
    gripper_z: 0.002
    gripper_rpy: 0.04
scene:
  nic_count_range: [1, 1]
  sc_count_range: [1, 1]
  target_cycling: false
  fixed_target:
    sfp: {rail: 0, port: "sfp_port_0"}
    sc: null
members:
  - {id: M0, name: "alice"}
  - {id: M1, name: "bob"}
  - {id: M2, name: "carol"}
  - {id: M3, name: "dave"}
  - {id: M4, name: "eve"}
  - {id: M5, name: "frank"}
```

Validation rules:

- `campaign.trial_id` must be one of `trial_1`, `trial_2`, `trial_3`
- `campaign.task_type` must be `sfp` or `sc`
- `campaign.total_target_count` must be a positive int
- `campaign.batch_default_count` must be a positive int and `<= total_target_count`
- `scene.fixed_target` must be present for the active `task_type`
- non-active task type may be `null`

### Recommended `TeamPreset` Extension

```python
@dataclass(frozen=True)
class TeamPreset:
    preset_name: str
    preset_path: Path
    preset_hash: str
    base_seed: int
    shard_stride: int
    index_width: int
    strategy: Literal["uniform", "lhs"]
    ranges: Mapping[str, Any]
    scene: Mapping[str, Any]
    members: tuple[Mapping[str, str], ...]
    trial_id: Literal["trial_1", "trial_2", "trial_3"] | None
    task_type: Literal["sfp", "sc"] | None
    total_target_count: int | None
    batch_default_count: int | None
    is_catalog_preset: bool
```

`None` values are reserved for the legacy single-file compatibility path. Catalog presets must populate all campaign fields.

### Loader API

```python
@dataclass(frozen=True)
class CatalogPresetIssue:
    path: Path
    message: str

def load_preset(path: Path) -> TeamPreset | None:
    """Legacy loader. Existing behavior remains unchanged."""

def load_presets(
    dir_path: Path,
) -> tuple[tuple[TeamPreset, ...], tuple[CatalogPresetIssue, ...]]:
    """Load valid catalog presets plus per-file validation issues."""
```

Invalid catalog files are excluded from the returned preset list and surfaced to the UI through `CatalogPresetIssue`. If the selected preset is invalid or disappears, submit is disabled.

## Shipped Presets

### `trial_1.yaml`

- `trial_id: trial_1`
- `task_type: sfp`
- `fixed_target.sfp = {rail: 0, port: "sfp_port_0"}`
- `total_target_count: 1000`
- `batch_default_count: 100`

### `trial_2.yaml`

- `trial_id: trial_2`
- `task_type: sfp`
- `fixed_target.sfp = {rail: 1, port: "sfp_port_0"}`
- `total_target_count: 1000`
- `batch_default_count: 100`

### `trial_3.yaml`

- `trial_id: trial_3`
- `task_type: sc`
- `fixed_target.sc = {rail: 1, port: "sc_port_1"}`
- `total_target_count: 1000`
- `batch_default_count: 100`

## Webapp Behavior

### Activation

When catalog presets exist, team mode top section becomes:

1. `Preset` dropdown
2. `Member` dropdown
3. Slot summary
4. Campaign summary

Display label for preset options:

```text
trial_1.yaml - trial_1 - SFP
trial_2.yaml - trial_2 - SFP
trial_3.yaml - trial_3 - SC
```

### Count Inputs

The active preset determines which task input is enabled:

- `task_type=sfp` -> enable `SFP configs`, force `SC configs = 0`
- `task_type=sc` -> enable `SC configs`, force `SFP configs = 0`

The enabled input uses:

- default value = `batch_default_count`
- max value = `min(member_slot_remaining, campaign_remaining)`

### Progress Captions

Two captions appear in campaign mode:

```text
Member slot: 100000 ~ 199999 - used 240 - remaining slot 99760
Campaign: trial_2 - SFP - goal 1000 - claimed 600 - remaining goal 400
```

If `campaign_remaining == 0`, the submit button is disabled and a completion message is shown.

### Preview

Preview rendering uses the selected preset's validated fixed target and task type:

- `trial_1` / `trial_2` preview calls `render_scene_svg(..., fixed_target=..., task_type="sfp")`
- `trial_3` preview calls the SC path with the SC fixed target

## Ledger and Submit Semantics

### Ledger Shape

Ledger file stays at `configs/team/seed_ledger.yaml`.

New entries add campaign-identifying fields:

```yaml
entries:
  - member_id: M1
    task_type: sfp
    trial_id: trial_2
    preset_name: trial_2
    base_seed: 42
    start_index: 100240
    count: 100
    strategy: uniform
    queue_root: "/tmp/queue"
    preset_hash: "sha256:..."
    git_sha: "abc1234"
    created_at: "2026-04-20T06:15:00Z"
```

`preset_hash` remains the authoritative grouping key. `trial_id` and `preset_name` are readability fields.

### Campaign Progress Accounting

Campaign claimed count is:

```text
sum(entry.count for entry in ledger if entry.preset_hash == selected_preset.preset_hash)
```

This counts actual written totals after any partial-write correction. Progress is therefore tied to the exact preset contents, not just the trial label.

### Authoritative Submit Check

Preview state may become stale if another member submits first. Therefore `submit_team_claim()` must re-check both constraints under the ledger lock:

1. next member slot index is still available
2. campaign remaining is still positive
3. requested batch size does not exceed campaign remaining

If the request exceeds the remaining campaign capacity, submit fails cleanly with a user-visible error rather than silently over-allocating.

## Error Handling

- Invalid catalog preset file: show error banner naming the file; omit it from selection.
- No valid catalog presets but directory exists: show team preset error and disable campaign mode.
- Selected preset reaches `total_target_count`: disable submit and show completion message.
- Selected preset points to malformed `fixed_target`: disable preview and submit with the existing preset error path.
- Concurrent submit fills campaign between preview and click: submit fails with a clear "campaign target reached" or "remaining capacity changed" message.
- Partial `write_plans` failure: preserve written files and adjust ledger `count` to actual written count, as current team mode already does.

## Backward Compatibility

### Catalog Present

If `configs/team/presets/*.yaml` exists, the new campaign selection flow is used and the legacy single-file preset is ignored.

### Catalog Absent

If no catalog presets exist but `configs/team/preset.yaml` exists, retain the current legacy team mode behavior. No new campaign summary or preset selection is required in that path.

### No Team Preset

If neither catalog nor legacy preset exists, solo mode behaves exactly as today.

## Test Plan

### Loader Tests

- loads `trial_1.yaml`, `trial_2.yaml`, `trial_3.yaml`
- rejects invalid `batch_default_count > total_target_count`
- rejects missing active-task `fixed_target`
- reports malformed catalog preset filenames cleanly
- preserves legacy `load_preset()` behavior

### Webapp State Tests

- preset catalog list is sorted and rendered correctly
- `trial_1` / `trial_2` enable only SFP count input
- `trial_3` enables only SC count input
- default count uses `batch_default_count`
- effective max count is clamped by slot remaining and campaign remaining
- campaign completion disables submit

### Submit / Ledger Tests

- `trial_1` submit writes SFP configs with fixed target `(0, sfp_port_0)`
- `trial_2` submit writes SFP configs with fixed target `(1, sfp_port_0)`
- `trial_3` submit writes SC configs with fixed target `(1, sc_port_1)`
- ledger entries contain `trial_id`, `preset_name`, and `preset_hash`
- campaign progress sums only matching `preset_hash`
- concurrent or stale submit cannot exceed `total_target_count`

## Risks and Tradeoffs

- Supporting both catalog presets and legacy preset mode adds branching in `webapp.py`, but it keeps old workflows intact.
- Grouping by `preset_hash` means small preset edits intentionally create a new campaign identity; this is desired for reproducibility, but operators need to understand that editing a preset restarts progress accounting for that preset hash.
- Keeping one shared ledger file is simpler operationally, but progress calculations must filter precisely by `preset_hash` to avoid cross-campaign contamination.

## Implementation Outline

1. Add catalog preset files under `configs/team/presets/`.
2. Extend `team_preset.py` with catalog loading and campaign metadata.
3. Extend ledger helpers with campaign progress queries.
4. Update webapp team mode to add preset selection, SC campaign support, and campaign-progress UI.
5. Add regression tests for `trial_1`, `trial_2`, `trial_3`, batching, and completion.
