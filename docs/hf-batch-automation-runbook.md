# Hugging Face batch automation runbook

This runbook covers the approved worker-integrated automation path that stages collected ROS2 data, converts it with `third_party/rosbag-to-lerobot`, uploads a finalized LeRobot dataset to Hugging Face, verifies the remote result, and only then cleans up local data.

## Package entry points

- Install the project normally so `aic-collector-worker` is available.
- `aic-collector-worker --hf-repo-id ...` owns collection, conversion, Hugging Face upload, remote verification, and cleanup gates in one worker execution path.
- `aic-automation-batch` remains a thin wrapper that delegates to the worker with upload flags; it must not implement a competing separate upload path.
- The Streamlit UI should expose this through the normal worker controls. The worker `limit` is the total requested episode count, and `upload_batch_size` is the collection/conversion/upload/cleanup group size. Do not add a separate "Batch → LeRobot → Hugging Face" panel.
- The runtime dependency for Hugging Face publishing is `huggingface_hub`.

## Hugging Face authentication

Use standard Hugging Face authentication outside the Streamlit UI:

1. Preferred for shells and CI:
   ```bash
   export HF_TOKEN=hf_...
   ```
2. Preferred for an operator workstation:
   ```bash
   huggingface-cli login
   ```

Do not paste an HF token into the UI, dataset form fields, manifests, logs, or config files. The UI must not store tokens; operators should provide credentials through `HF_TOKEN` or the local Hugging Face login cache only.

## Converter submodule/path setup

The converter is expected at `third_party/rosbag-to-lerobot`.

```bash
git submodule update --init --recursive third_party/rosbag-to-lerobot
```

Before a run, confirm the path exists and the converter's documented entry point/config format is available. Stage converter input non-destructively by copying, hardlinking, or symlinking source MCAP/rosbag artifacts into batch-owned staging directories. Do not move or delete original run directories during conversion.

## State and recovery

The append-only batch manifest is the source of truth. The normal lifecycle is:

`planned -> worker_started -> worker_finished -> reconciled -> collected_validated -> staged -> converted -> uploaded -> remote_verified -> cleanup_eligible -> cleanup_done`

Recovery rules:

- If a run stops before `uploaded`, resume from the latest successful local state and reuse existing staged/converted paths when their recorded digests still match.
- If a run stops at `uploaded`, resume at remote verification using the recorded repo id, dataset repo type, commit/revision, upload timestamp, local digest, and file count. Do not blindly upload a duplicate batch.
- If remote verification fails, keep local artifacts intact and retry verification or upload with explicit operator review.
- If cleanup fails after `cleanup_eligible`, retry cleanup only for manifest-listed paths and append a new result event.

## Cleanup safety

Cleanup only after `remote_verified` evidence exists for the exact batch and uploaded revision. Never delete collected data before remote_verified. The worker may delete raw run directories, staging folders, and temporary LeRobot folders after each verified upload batch, but only for paths explicitly listed in the manifest for that verified batch; then append `cleanup_done` with tombstones for the deleted paths and timestamp.

Never delete manifests, logs, or unrelated queue roots. There is no unchecked destructive cleanup mode in the approved MVP.

## Security verification findings

Static review of this worker slice found no implementation code that persists Hugging Face credentials. The package/docs checks enforce that authentication remains via `HF_TOKEN` or `huggingface-cli login`, and that operator-facing docs explicitly forbid UI token persistence.

Static review also confirms the documented cleanup gate: cleanup only follows `remote_verified`, with `cleanup_eligible` and `cleanup_done` recorded afterward. Any implementation change that adds cleanup before `remote_verified` must be rejected or covered by a failing regression test before merge.
