from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

import yaml
from prefect import flow, task
from prefect.artifacts import create_markdown_artifact

from aic_collector.build_engine_config import build as build_engine_cfg
from aic_collector.postprocess_run import process_run
from aic_collector.sampler import sample_parameters

from .policy_env import POLICY_CLASS, build_policy_env, deploy_policies
from .shell_runner import (
    graceful_cleanup,
    run_process_background,
    run_process_until_log_match,
    run_shell_process,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENGINE_RESULTS = Path.home() / "aic_results"
READY_FLAG = Path.home() / "aic_ready"
DONE_FLAG = Path.home() / "aic_done"
PIXI_CWD = str(Path.home() / "ws_aic/src/aic")
CAMERAS = ["left_camera", "center_camera", "right_camera"]

PROGRESS_FILE = Path("/tmp/e2e_prefect_progress.json")
LOG_FILE = Path("/tmp/e2e_webapp_run.log")

# task 순서 (UI 표시용)
TASK_ORDER = [
    "sample-parameters",
    "deploy-policies",
    "build-engine-config",
    "restart-docker",
    "launch-engine",
    "launch-republish",
    "run-policy",
    "cleanup-run",
    "postprocess",
    "validate-run",
]


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env["DBX_CONTAINER_MANAGER"] = "docker"
    env["PATH"] = str(Path.home() / ".pixi/bin") + ":" + env.get("PATH", "")
    return env


def _read_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text())
        except Exception:
            pass
    return {}


# run 단위 task (각 run마다 반복되는 task들)
RUN_TASK_ORDER = [
    "build-engine-config",
    "restart-docker",
    "launch-engine",
    "launch-republish",
    "run-policy",
    "cleanup-run",
    "postprocess",
    "validate-run",
]


def _init_progress_all(total: int) -> None:
    """flow 시작 시점: 전체 task 상태 초기화."""
    PROGRESS_FILE.write_text(json.dumps({
        "completed": 0,
        "total": total,
        "current_label": "",
        "current_task": "",
        "status": "running",
        "tasks": {name: "pending" for name in TASK_ORDER},
        "task_durations_ms": {},
    }))


def _write_progress(completed: int, total: int, label: str = "") -> None:
    """run 진입 시점: run 단위 task만 pending으로 리셋."""
    progress = _read_progress()
    progress.update({
        "completed": completed,
        "total": total,
        "current_label": label,
        "status": "running",
    })
    # run 단위 task만 리셋 (sample, deploy는 보존)
    tasks = progress.setdefault("tasks", {name: "pending" for name in TASK_ORDER})
    for name in RUN_TASK_ORDER:
        tasks[name] = "pending"
    progress["current_task"] = ""
    PROGRESS_FILE.write_text(json.dumps(progress))


def _update_task_state(name: str, state: str, duration_ms: int | None = None) -> None:
    """task 상태 업데이트. state: pending | running | completed | failed."""
    progress = _read_progress()
    tasks = progress.setdefault("tasks", {name: "pending" for name in TASK_ORDER})
    tasks[name] = state
    if state == "running":
        progress["current_task"] = name
    if duration_ms is not None:
        progress.setdefault("task_durations_ms", {})[name] = duration_ms
    PROGRESS_FILE.write_text(json.dumps(progress))


def _task_timer(name: str):
    """task 시작/종료를 기록하는 컨텍스트 매니저."""
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        start = time.time()
        _update_task_state(name, "running")
        try:
            yield
            _update_task_state(name, "completed", int((time.time() - start) * 1000))
        except Exception:
            _update_task_state(name, "failed", int((time.time() - start) * 1000))
            raise

    return _ctx()


def _append_log(msg: str) -> None:
    with open(LOG_FILE, "a") as f:
        f.write(msg + "\n")
        f.flush()


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

@task(name="sample-parameters")
def sample_parameters_task(
    params_cfg: dict,
    strategy: str,
    runs: int,
    seed: int,
) -> list[dict[str, float]]:
    """파라미터 샘플링."""
    with _task_timer("sample-parameters"):
        samples = sample_parameters(params_cfg, strategy, runs, seed)
        print(f"[sampler] {len(samples)}개 샘플 생성")
        return samples


@task(name="deploy-policies")
def deploy_policies_task(project_dir: str) -> None:
    """Policy 파일을 pixi env에 배포."""
    with _task_timer("deploy-policies"):
        deploy_policies(project_dir)


@task(name="build-engine-config", retries=3, retry_delay_seconds=2)
def build_engine_config_task(
    template_path: str,
    trial_ids: list[str],
    sample: dict[str, float],
    out_path: str,
) -> str:
    """엔진 config 생성 (템플릿 + trial 필터 + 파라미터 치환)."""
    with _task_timer("build-engine-config"):
        cfg_text = build_engine_cfg(Path(template_path), trial_ids, sample)
        Path(out_path).write_text(cfg_text)
        print(f"[ok] wrote {out_path}")
        return out_path


@task(name="restart-docker", retries=2, retry_delay_seconds=10)
def restart_docker_task(container: str = "aic_eval") -> None:
    """Docker 컨테이너 재시작 + distrobox init 보장."""
    with _task_timer("restart-docker"):
        env = _base_env()

        # distrobox init 보장
        user = os.environ.get("USER", "root")
        ret, _ = run_shell_process(
            ["docker", "exec", container, "id", user],
            log_path="/tmp/e2e_docker_init.log",
            env=env,
        )
        if ret != 0:
            print("[init] distrobox 최초 초기화 중...")
            run_shell_process(
                ["distrobox", "enter", container, "--", "true"],
                log_path="/tmp/e2e_docker_init.log",
                env=env,
            )
            print("[init] 초기화 완료")

        # 이전 결과 치우기
        READY_FLAG.unlink(missing_ok=True)
        DONE_FLAG.unlink(missing_ok=True)
        if ENGINE_RESULTS.exists():
            if ENGINE_RESULTS.is_dir():
                shutil.rmtree(ENGINE_RESULTS)
            else:
                ENGINE_RESULTS.unlink()

        print("[engine] 컨테이너 재시작...")
        run_shell_process(
            ["docker", "restart", container],
            log_path="/tmp/e2e_docker_restart.log",
            env=env,
        )
        time.sleep(5)


@task(name="launch-engine")
def launch_engine_task(
    engine_cfg: str,
    ground_truth: bool,
    run_tag: str,
    run_idx: int,
    startup_wait: int = 25,
    headless: bool = False,
) -> dict:
    """엔진 프로세스 백그라운드 기동. {pid, log_path} 반환.

    headless=True면 aic_gz_bringup launch에 ``gazebo_gui:=false`` 와
    ``launch_rviz:=false`` 인자를 추가해 GUI 창을 띄우지 않는다.
    """
    with _task_timer("launch-engine"):
        gt_arg = "ground_truth:=true" if ground_truth else "ground_truth:=false"
        log_path = f"/tmp/e2e_engine_{run_tag}_run{run_idx}.log"
        env = _base_env()

        cmd = [
            "distrobox", "enter", "aic_eval", "--",
            "/entrypoint.sh",
            gt_arg,
            "start_aic_engine:=true",
            f"aic_engine_config_file:={engine_cfg}",
        ]
        if headless:
            cmd.extend(["gazebo_gui:=false", "launch_rviz:=false"])

        pid = run_process_background(cmd, log_path=log_path, env=env)
        gui_state = "headless" if headless else "gui"
        print(f"[engine] 기동 (pid={pid}, {gt_arg}, {gui_state})")
        time.sleep(startup_wait)
        return {"pid": pid, "log_path": log_path}


@task(name="launch-republish")
def launch_republish_task(
    use_compressed: bool,
    run_tag: str,
    run_idx: int,
) -> list[int]:
    """카메라 compressed republish 프로세스 기동. PID 리스트 반환."""
    with _task_timer("launch-republish"):
        if not use_compressed:
            print("[republish] 이미지 압축 비활성화 — raw 이미지 사용")
            return []

        env = _base_env()
        pids = []
        print("[republish] 카메라 compressed 시작...")
        for cam in CAMERAS:
            log_path = f"/tmp/e2e_republish_{cam}_{run_tag}_run{run_idx}.log"
            pid = run_process_background(
                [
                    "distrobox", "enter", "aic_eval", "--", "bash", "-c",
                    f"source /ws_aic/install/setup.bash && "
                    f"export RMW_IMPLEMENTATION=rmw_zenoh_cpp && "
                    f"ros2 run image_transport republish "
                    f"--ros-args -p in_transport:=raw -p out_transport:=compressed "
                    f"-r in:=/{cam}/image -r out/compressed:=/{cam}/image/compressed "
                    f"-p use_sim_time:=true",
                ],
                log_path=log_path,
                env=env,
            )
            pids.append(pid)
        time.sleep(3)
        return pids


@task(name="run-policy", timeout_seconds=900)
def run_policy_task(
    policy_env: dict[str, str],
    demo_dir: str,
    run_tag: str,
    run_idx: int,
    policy_timeout: int = 300,
    collect_episode: bool = False,
) -> bool:
    """Policy 실행, on_shutdown 대기. 성공 여부 반환."""
    with _task_timer("run-policy"):
        log_path = f"/tmp/e2e_policy_{run_tag}_run{run_idx}.log"

        env = _base_env()
        env.update(policy_env)
        env["AIC_DEMO_DIR"] = demo_dir
        env["AIC_F5_ENABLED"] = os.environ.get("AIC_F5_ENABLED", "1")
        env["AIC_COLLECT_EPISODE"] = "1" if collect_episode else "0"

        policy_class = policy_env.get("POLICY_CLASS", POLICY_CLASS)
        print(f"[policy] {policy_class} 실행...")

        matched, pid = run_process_until_log_match(
            [
                "pixi", "run", "ros2", "run", "aic_model", "aic_model",
                "--ros-args", "-p", "use_sim_time:=true",
                "-p", f"policy:={policy_class}",
            ],
            log_path=log_path,
            pattern="on_shutdown",
            timeout_sec=policy_timeout,
            env=env,
            cwd=PIXI_CWD,
        )

        if not matched:
            print(f"[warn] Policy 타임아웃 ({policy_timeout}초)")
        return matched


@task(name="cleanup-run")
def cleanup_task(
    engine_handle: dict | None,
    republish_pids: list[int],
) -> None:
    """프로세스 정리 — 일괄 SIGTERM + 공용 grace 윈도우 (polling).

    기존엔 대상마다 `SIGTERM→sleep 3s→SIGKILL`를 순차 수행해 총 ~15s 소요.
    지금은 모든 대상에 TERM을 한 번에 보내고 단일 3s 윈도우에서 polling →
    대부분 ~0.2–1s에 끝남, 최악 3s.
    """
    with _task_timer("cleanup-run"):
        pids: list[int] = [p for p in republish_pids if p and p > 0]
        if engine_handle and engine_handle.get("pid", -1) > 0:
            pids.append(engine_handle["pid"])

        graceful_cleanup(
            pids=pids,
            patterns=["aic_model"],
            grace_sec=3.0,
            poll_interval=0.1,
        )


def _build_run_summary_markdown(
    run_dir: str,
    policy: str,
    seed: int,
    params: dict[str, float],
    success: bool,
    validation: dict | None = None,
) -> str:
    """run 결과를 Prefect 아티팩트용 markdown으로 변환."""
    run_name = Path(run_dir).name
    status = "✅ 성공" if success else "❌ 실패"

    # trial별 점수 스캔 — legacy(trial_*_score*) 우선, 없으면 flat(tags.json)
    trial_rows = []
    if success:
        run_path = Path(run_dir)
        trial_dirs = sorted(run_path.glob("trial_*_score*"))
        if trial_dirs:
            for trial_dir in trial_dirs:
                m = re.match(r"trial_(\d+)_score(\d+)", trial_dir.name)
                if m:
                    trial_num = m.group(1)
                    score = m.group(2)
                    trial_rows.append(f"| trial_{trial_num} | {score} |")
        else:
            # flat 구조: run_dir/tags.json
            tags_path = run_path / "tags.json"
            if tags_path.exists():
                try:
                    tags = json.loads(tags_path.read_text())
                    trial_num = tags.get("trial", "?")
                    score_val = (tags.get("scoring") or {}).get("total")
                    score = round(score_val) if isinstance(score_val, (int, float)) else "?"
                    trial_rows.append(f"| trial_{trial_num} | {score} |")
                except Exception:
                    pass

    params_rows = "\n".join(
        f"| `{k}` | {v:.4f} |" for k, v in sorted(params.items())
    )

    # 검증 결과 섹션
    validation_md = ""
    if validation:
        checks = validation.get("checks", [])
        warnings_list = validation.get("warnings", [])
        validation_md = "\n## Validation\n\n"
        if checks:
            validation_md += "| Check | Result |\n|-------|--------|\n"
            for c in checks:
                icon = "✅" if c["passed"] else "❌"
                validation_md += f"| {c['name']} | {icon} |\n"
        if warnings_list:
            validation_md += "\n**경고:**\n"
            for w in warnings_list:
                validation_md += f"- ⚠️ {w}\n"

    md = f"""# Run {run_name}

**상태**: {status}
**Policy**: `{policy}`
**Seed**: {seed}

## Trials

| Trial | Score |
|-------|-------|
{chr(10).join(trial_rows) if trial_rows else "| - | - |"}

## Parameters

| Name | Value |
|------|-------|
{params_rows}
{validation_md}
## Output

`{run_dir}`
"""
    return md


# ---------------------------------------------------------------------------
# 검증 태스크
# ---------------------------------------------------------------------------

def _validate_run_dir(run_dir: Path, collect_episode: bool = False) -> dict:
    """run 산출물 구조/크기 검증. {checks, warnings, passed_count, total_count} 반환."""
    checks = []
    warnings_list = []

    def _check(name: str, passed: bool, warn: str | None = None):
        checks.append({"name": name, "passed": passed})
        if not passed and warn:
            warnings_list.append(warn)

    # 1. run_dir 존재
    _check("run 디렉토리 존재", run_dir.exists(), f"{run_dir} 없음")
    if not run_dir.exists():
        return {
            "checks": checks, "warnings": warnings_list,
            "passed_count": 0, "total_count": len(checks),
        }

    # 2. 메타 파일들
    _check("config.yaml", (run_dir / "config.yaml").exists(), "엔진 config 복사 안 됨")
    _check("scoring_run.yaml", (run_dir / "scoring_run.yaml").exists(), "전체 scoring 없음")
    _check("policy.txt", (run_dir / "policy.txt").exists(), "policy 메타 없음")
    _check("seed.txt", (run_dir / "seed.txt").exists(), "seed 메타 없음")

    # 3. trial 디렉토리 (레거시) 또는 flat 구조
    trial_dirs = sorted(run_dir.glob("trial_*_score*"))
    if trial_dirs:
        _check("trial 디렉토리 ≥ 1개", True, None)
    else:
        # flat 구조: tags.json이 run_dir 바로 아래
        flat_ok = (run_dir / "tags.json").exists()
        _check(
            "tags.json (flat) 또는 trial 디렉토리",
            flat_ok,
            "trial 디렉토리도 없고 run_dir/tags.json도 없음",
        )
        if flat_ok:
            trial_dirs = [run_dir]  # 이하 로직 재사용

    for td in trial_dirs:
        prefix = td.name if td != run_dir else "(flat)"

        # bag 존재
        bag_dir = td / "bag"
        mcap_files = list(bag_dir.glob("*.mcap")) if bag_dir.exists() else []
        _check(
            f"{prefix}/bag/*.mcap",
            len(mcap_files) > 0,
            f"{prefix}: bag mcap 없음",
        )
        if mcap_files:
            bag_size = mcap_files[0].stat().st_size
            if bag_size < 1024:  # 1KB 미만은 비정상
                warnings_list.append(f"{prefix}: bag 파일 크기 비정상 ({bag_size} bytes)")

        # episode 존재 (collect_episode 활성 시만 검증)
        if collect_episode:
            episode_dir = td / "episode"
            _check(f"{prefix}/episode/", episode_dir.exists(), f"{prefix}: episode 없음")

            if episode_dir.exists():
                # 필수 npy 파일
                for npy in ["states.npy", "actions.npy", "wrenches.npy"]:
                    _check(
                        f"{prefix}/episode/{npy}",
                        (episode_dir / npy).exists(),
                        f"{prefix}: {npy} 없음",
                    )

                # 이미지 디렉토리
                images_dir = episode_dir / "images"
                if images_dir.exists():
                    for cam in ["left", "center", "right"]:
                        cam_dir = images_dir / cam
                        n_images = len(list(cam_dir.glob("*.png"))) if cam_dir.exists() else 0
                        _check(
                            f"{prefix}/images/{cam}/ ≥ 1 PNG",
                            n_images > 0,
                            f"{prefix}/{cam}: 이미지 없음",
                        )

                # metadata.json
                _check(
                    f"{prefix}/episode/metadata.json",
                    (episode_dir / "metadata.json").exists(),
                    f"{prefix}: metadata.json 없음",
                )

        # scoring/tags (flat 모드에서는 trial scoring 파일명이 trial_scoring.yaml)
        scoring_fn = "trial_scoring.yaml" if td == run_dir else "scoring.yaml"
        _check(f"{prefix}/{scoring_fn}", (td / scoring_fn).exists(), f"{prefix}: {scoring_fn} 없음")
        _check(f"{prefix}/tags.json", (td / "tags.json").exists(), f"{prefix}: tags.json 없음")

    passed = sum(1 for c in checks if c["passed"])
    return {
        "checks": checks,
        "warnings": warnings_list,
        "passed_count": passed,
        "total_count": len(checks),
    }


@task(name="validate-run")
def validate_run_task(run_dir: str, collect_episode: bool = False) -> dict:
    """run 산출물의 구조/크기/완결성 검증. 결과를 run_dir/validation.json에도 저장."""
    with _task_timer("validate-run"):
        result = _validate_run_dir(Path(run_dir), collect_episode=collect_episode)
        passed = result["passed_count"]
        total = result["total_count"]
        if passed == total:
            print(f"[validate] ✅ {passed}/{total} 체크 통과")
        else:
            print(f"[validate] ⚠️  {passed}/{total} 체크 통과, 경고 {len(result['warnings'])}개")
            for w in result["warnings"]:
                print(f"  - {w}")

        # run_dir에 저장 (webapp 결과 탭에서 조회)
        try:
            (Path(run_dir) / "validation.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2)
            )
        except Exception:
            pass

        return result


@task(name="postprocess", retries=2, retry_delay_seconds=5)
def postprocess_task(
    run_dir: str,
    demo_dir: str,
    engine_cfg: str,
    policy: str,
    seed: int,
    params: dict[str, float],
    flatten: bool = False,
) -> dict:
    """run 산출물 재편."""
    with _task_timer("postprocess"):
        if not ENGINE_RESULTS.exists():
            print(f"[error] {ENGINE_RESULTS} 없음 — 엔진/policy 실행 실패")
            return {"success": False, "run_dir": run_dir}

        print(f"[postprocess] {run_dir} 로 재편... (flatten={flatten})")
        params_json_path = Path(f"/tmp/e2e_params_{Path(run_dir).name}.json")
        params_json_path.write_text(json.dumps(params))

        rc = process_run(
            run_dir=Path(run_dir),
            engine_results=ENGINE_RESULTS,
            demo_dir=Path(demo_dir),
            engine_config=Path(engine_cfg),
            policy=policy,
            seed=seed,
            parameters=params,
            flatten=flatten,
        )

        params_json_path.unlink(missing_ok=True)

        success = rc == 0
        if success:
            _append_log(f"[done] run 재편 완료: {run_dir}")
        else:
            print("[warn] postprocess 실패")

        return {"success": success, "run_dir": run_dir}


def _emit_run_artifact(
    run_dir: str,
    policy: str,
    seed: int,
    params: dict[str, float],
    success: bool,
    validation: dict | None,
) -> None:
    """run 요약 markdown artifact를 Prefect에 기록."""
    try:
        desc = f"Run {Path(run_dir).name} 결과 요약"
        if validation and validation.get("warnings"):
            desc += f" (경고 {len(validation['warnings'])}개)"
        create_markdown_artifact(
            key=f"run-{Path(run_dir).name.replace('_', '-')}",
            markdown=_build_run_summary_markdown(
                run_dir, policy, seed, params, success, validation
            ),
            description=desc,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Sub-flow: single run
# ---------------------------------------------------------------------------

def run_one(
    run_idx: int,
    total_runs: int,
    sample: dict[str, float],
    cfg: dict,
    run_tag: str,
    project_dir: str,
) -> dict:
    """단일 run 실행 (엔진 → policy → 후처리)."""
    collection = cfg.get("collection", {}) or {}
    policy_cfg = cfg.get("policy", {}) or {}
    engine_cfg = cfg.get("engine", {}) or {}

    output_root = os.path.expanduser(collection.get("output_root", "~/aic_community_e2e"))
    trials = collection.get("trials", [1, 2, 3])
    trial_ids = [str(t) for t in trials]
    policy_default = policy_cfg.get("default", "cheatcode")
    ground_truth = engine_cfg.get("ground_truth", True)
    use_compressed = engine_cfg.get("use_compressed", False)
    collect_episode = collection.get("collect_episode", False)
    headless = bool(engine_cfg.get("headless", False))
    template = engine_cfg.get("template", "configs/community_random_config.yaml")
    seed = collection.get("seed", 42)

    run_name = f"run_{run_idx:02d}_{run_tag}"
    run_dir = f"{output_root}/{run_name}"
    engine_cfg_path = f"/tmp/e2e_engine_{run_tag}_run{run_idx}.yaml"
    demo_dir = f"/tmp/e2e_demos_{run_tag}_run{run_idx}"

    print()
    msg = f"  RUN {run_idx}/{total_runs}  →  {run_dir}"
    print("=" * 60)
    print(msg)
    print("=" * 60)
    _append_log(f"RUN {run_idx}/{total_runs}")
    _write_progress(run_idx - 1, total_runs, f"RUN {run_idx}/{total_runs}")

    # 준비
    shutil.rmtree(demo_dir, ignore_errors=True)
    Path(demo_dir).mkdir(parents=True)
    Path(run_dir).mkdir(parents=True, exist_ok=True)

    # 1. 엔진 config 생성
    template_path = str(Path(project_dir) / template)
    build_engine_config_task(template_path, trial_ids, sample, engine_cfg_path)

    # 2. 컨테이너 재시작
    restart_docker_task()

    # 3. 엔진 기동
    engine_handle = None
    republish_pids = []

    try:
        engine_handle = launch_engine_task(
            engine_cfg_path, ground_truth, run_tag, run_idx, headless=headless
        )

        # 4. Republish (compressed only)
        republish_pids = launch_republish_task(use_compressed, run_tag, run_idx)

        # 5. Policy 실행
        per_trial = policy_cfg.get("per_trial")
        act_model_path = policy_cfg.get("act_model_path")
        if act_model_path:
            act_model_path = os.path.expanduser(act_model_path)
        policy_env = build_policy_env(policy_default, per_trial, act_model_path)

        # trial당 최대 200초 + 여유 60초
        policy_timeout = len(trials) * 200 + 60
        run_policy_task(policy_env, demo_dir, run_tag, run_idx,
                        policy_timeout=policy_timeout, collect_episode=collect_episode)

    finally:
        # 6. 정리
        cleanup_task(engine_handle, republish_pids)

    # 7. 후처리
    result = postprocess_task(run_dir, demo_dir, engine_cfg_path, policy_default, seed, sample)

    # 8. 검증
    validation = None
    if result.get("success"):
        validation = validate_run_task(run_dir, collect_episode=collect_episode)
        result["validation"] = validation

    # 9. Artifact 기록 (검증 결과 포함)
    _emit_run_artifact(
        run_dir=run_dir,
        policy=policy_default,
        seed=seed,
        params=sample,
        success=result.get("success", False),
        validation=validation,
    )

    # 임시 파일 정리
    Path(engine_cfg_path).unlink(missing_ok=True)
    shutil.rmtree(demo_dir, ignore_errors=True)

    return result


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

@flow(name="aic-collect-e2e", log_prints=True)
def collect_e2e_flow(
    config_path: str,
    runs_override: int | None = None,
    seed_override: int | None = None,
    do_deploy: bool = True,
    dry_run: bool = False,
    headless: bool | None = None,
) -> dict:
    """E2E 수집 파이프라인.

    headless가 명시되면 cfg["engine"]["headless"]를 덮어쓴다 (CLI 우선).
    None이면 cfg 값을 그대로 사용한다.
    """
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    if headless is not None:
        cfg.setdefault("engine", {})["headless"] = bool(headless)

    collection = cfg.get("collection", {}) or {}
    params_cfg = cfg.get("parameters", {}) or {}
    sampling = cfg.get("sampling", {}) or {}

    runs = runs_override or collection.get("runs", 10)
    seed = seed_override or collection.get("seed", 42)
    strategy = sampling.get("strategy", "uniform")

    print("=== AIC E2E 수집 ===")
    print(f"config: {config_path}")
    print(f"  runs: {runs}, seed: {seed}, strategy: {strategy}")

    # 프로젝트 디렉토리
    project_dir = str(Path(config_path).resolve().parent.parent)
    if not (Path(project_dir) / "policies").exists():
        project_dir = str(Path.cwd())

    # 진행 상태 초기화 (전체 task pending)
    LOG_FILE.write_text("")
    _init_progress_all(runs)

    # 1. 샘플링
    samples = sample_parameters_task(params_cfg, strategy, runs, seed)

    if dry_run:
        print("\n=== DRY-RUN: 샘플 시퀀스 ===")
        for i, s in enumerate(samples):
            print(f"  run {i+1}: {s}")
        return {"dry_run": True, "samples": samples}

    # 2. Policy 배포
    if do_deploy:
        deploy_policies_task(project_dir)

    # 3. 각 run 실행
    run_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = os.path.expanduser(collection.get("output_root", "~/aic_community_e2e"))
    Path(output_root).mkdir(parents=True, exist_ok=True)

    # seed를 config에 주입 (run_one에서 사용)
    cfg.setdefault("collection", {})["seed"] = seed

    fail_count = 0
    start_time = time.time()

    for i in range(1, runs + 1):
        try:
            result = run_one(i, runs, samples[i - 1], cfg, run_tag, project_dir)
            if not result.get("success", False):
                fail_count += 1
        except Exception as e:
            print(f"[error] RUN {i} 실패: {e}")
            fail_count += 1

    elapsed = int(time.time() - start_time)

    # 4. 최종 요약
    print()
    print("=" * 60)
    if fail_count == 0:
        print("  E2E 수집 완료")
    elif fail_count == runs:
        print(f"  E2E 수집 실패 (전체 {runs}개 run 실패)")
    else:
        print(f"  E2E 수집 부분 완료 ({fail_count}/{runs}개 run 실패)")
    print("=" * 60)
    print(f"총 소요 시간: {elapsed}초 ({elapsed/3600:.2f} h)")
    print(f"출력 경로: {output_root}")
    print(f"성공: {runs - fail_count} / 실패: {fail_count} / 전체: {runs}")

    _append_log("E2E 수집 완료")
    final_progress = _read_progress()
    final_progress.update({
        "completed": runs,
        "total": runs,
        "status": "completed" if fail_count < runs else "failed",
        "fail_count": fail_count,
        "elapsed_sec": elapsed,
    })
    PROGRESS_FILE.write_text(json.dumps(final_progress))

    return {
        "runs": runs,
        "fail_count": fail_count,
        "elapsed_sec": elapsed,
        "output_root": output_root,
    }


# ---------------------------------------------------------------------------
# Prebuilt engine config 실행 (Phase 2b 큐 consumer용)
# ---------------------------------------------------------------------------
#
# 이미 생성된 엔진 config(YAML) 파일을 받아 1회 실행. build_engine_config_task
# 단계를 생략한다는 점을 제외하면 run_one과 동일한 단계 구성. pending/에서
# claim한 파일을 이 함수로 소비한다.


def _queue_consume_run_name() -> str:
    """flow_run_name — engine_cfg_path의 파일 stem으로 run 라벨 생성.

    예: `config_sfp_0005.yaml` → `config_sfp_0005`. Prefect UI에서 어떤
    config가 실행됐는지 한눈에 구분 가능.
    """
    try:
        from prefect.runtime import flow_run as _fr
        params = _fr.parameters or {}
        p = params.get("engine_cfg_path") or ""
        if p:
            return Path(p).stem
    except Exception:
        pass
    return "queue-consume"


@flow(
    name="aic-queue-consume",
    flow_run_name=_queue_consume_run_name,
    log_prints=True,
)
def run_prebuilt_engine_config(
    engine_cfg_path: str,
    run_tag: str,
    run_idx: int = 1,
    total_runs: int = 1,
    policy_default: str = "cheatcode",
    per_trial: dict | None = None,
    act_model_path: str | None = None,
    ground_truth: bool = True,
    use_compressed: bool = False,
    collect_episode: bool = False,
    output_root: str = "~/aic_community_e2e",
    headless: bool = False,
) -> dict:
    """이미 생성된 엔진 config 파일로 1 run 실행 (큐 소비용).

    Args:
        engine_cfg_path: 소비할 엔진 config 경로 (큐의 running/에서 claim된 파일)
        run_tag: 출력 run_dir 이름에 들어갈 태그 (타임스탬프 권장)
        policy_default/per_trial/act_model_path: policy 구성
        ground_truth/use_compressed/collect_episode: engine/수집 옵션
        output_root: run_dir 루트 (~/aic_community_e2e 등)

    Returns:
        postprocess 결과 dict (success 포함). validation도 포함될 수 있음.
    """
    output_root = os.path.expanduser(output_root)
    # 큐 모드: run_idx는 항상 1이므로 prefix 없이 run_tag만 사용.
    # run_tag는 consumer_cli에서 "{timestamp}_{task}_{NNNN}" 형식으로 생성됨.
    run_name = f"run_{run_tag}"
    run_dir = f"{output_root}/{run_name}"
    demo_dir = f"/tmp/e2e_demos_{run_tag}_run{run_idx}"

    print()
    print("=" * 60)
    print(f"  RUN {run_idx}/{total_runs}  →  {run_dir}")
    print(f"  engine_config: {engine_cfg_path}")
    print("=" * 60)
    _append_log(f"RUN {run_idx}/{total_runs} (prebuilt)")
    _write_progress(run_idx - 1, total_runs, f"RUN {run_idx}/{total_runs}")

    shutil.rmtree(demo_dir, ignore_errors=True)
    Path(demo_dir).mkdir(parents=True)
    Path(run_dir).mkdir(parents=True, exist_ok=True)

    # trial 수 추정 (policy timeout 계산용)
    try:
        with open(engine_cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        trials_count = len(cfg.get("trials", {}) or {}) or 1
    except Exception:
        trials_count = 1

    # 1. 컨테이너 재시작
    restart_docker_task()

    # 2. 엔진 기동
    engine_handle = None
    republish_pids: list[int] = []

    try:
        engine_handle = launch_engine_task(
            engine_cfg_path, ground_truth, run_tag, run_idx, headless=headless
        )
        republish_pids = launch_republish_task(use_compressed, run_tag, run_idx)
        policy_env = build_policy_env(policy_default, per_trial, act_model_path)
        policy_timeout = trials_count * 200 + 60
        run_policy_task(
            policy_env, demo_dir, run_tag, run_idx,
            policy_timeout=policy_timeout, collect_episode=collect_episode,
        )
    finally:
        cleanup_task(engine_handle, republish_pids)

    # 3. 후처리 — sample/seed는 큐 consumer에는 없으므로 0/빈값.
    # flatten=True로 trial 래퍼 없이 run_dir 바로 아래에 bag/episode/tags.json 배치.
    result = postprocess_task(
        run_dir, demo_dir, engine_cfg_path, policy_default, 0, {}, flatten=True
    )

    validation = None
    if result.get("success"):
        validation = validate_run_task(run_dir, collect_episode=collect_episode)
        result["validation"] = validation

    _emit_run_artifact(
        run_dir=run_dir,
        policy=policy_default,
        seed=0,
        params={},
        success=result.get("success", False),
        validation=validation,
    )

    shutil.rmtree(demo_dir, ignore_errors=True)
    return result
