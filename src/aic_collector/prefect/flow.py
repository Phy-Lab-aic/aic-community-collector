from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

import yaml
from prefect import flow, task

from aic_collector.build_engine_config import build as build_engine_cfg
from aic_collector.postprocess_run import process_run
from aic_collector.sampler import sample_parameters

from .policy_env import POLICY_CLASS, build_policy_env, deploy_policies
from .shell_runner import (
    kill_process_tree,
    pkill_pattern,
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


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env["DBX_CONTAINER_MANAGER"] = "docker"
    env["PATH"] = str(Path.home() / ".pixi/bin") + ":" + env.get("PATH", "")
    return env


def _write_progress(completed: int, total: int, label: str = "") -> None:
    PROGRESS_FILE.write_text(json.dumps({
        "completed": completed,
        "total": total,
        "current_label": label,
        "status": "running",
    }))


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
    samples = sample_parameters(params_cfg, strategy, runs, seed)
    print(f"[sampler] {len(samples)}개 샘플 생성")
    return samples


@task(name="deploy-policies")
def deploy_policies_task(project_dir: str) -> None:
    """Policy 파일을 pixi env에 배포."""
    deploy_policies(project_dir)


@task(name="build-engine-config", retries=3, retry_delay_seconds=2)
def build_engine_config_task(
    template_path: str,
    trial_ids: list[str],
    sample: dict[str, float],
    out_path: str,
) -> str:
    """엔진 config 생성 (템플릿 + trial 필터 + 파라미터 치환)."""
    cfg_text = build_engine_cfg(Path(template_path), trial_ids, sample)
    Path(out_path).write_text(cfg_text)
    print(f"[ok] wrote {out_path}")
    return out_path


@task(name="restart-docker", retries=2, retry_delay_seconds=10)
def restart_docker_task(container: str = "aic_eval") -> None:
    """Docker 컨테이너 재시작 + distrobox init 보장."""
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
        backup = ENGINE_RESULTS.parent / f"aic_results_e2e_backup_{int(time.time())}"
        shutil.move(str(ENGINE_RESULTS), str(backup))

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
) -> dict:
    """엔진 프로세스 백그라운드 기동. {pid, log_path} 반환."""
    gt_arg = "ground_truth:=true" if ground_truth else "ground_truth:=false"
    log_path = f"/tmp/e2e_engine_{run_tag}_run{run_idx}.log"
    env = _base_env()

    pid = run_process_background(
        [
            "distrobox", "enter", "aic_eval", "--",
            "/entrypoint.sh",
            gt_arg,
            "start_aic_engine:=true",
            f"aic_engine_config_file:={engine_cfg}",
        ],
        log_path=log_path,
        env=env,
    )
    print(f"[engine] 기동 (pid={pid}, {gt_arg})")
    time.sleep(startup_wait)
    return {"pid": pid, "log_path": log_path}


@task(name="launch-republish")
def launch_republish_task(
    use_compressed: bool,
    run_tag: str,
    run_idx: int,
) -> list[int]:
    """카메라 compressed republish 프로세스 기동. PID 리스트 반환."""
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


@task(name="run-policy", timeout_seconds=360)
def run_policy_task(
    policy_env: dict[str, str],
    demo_dir: str,
    run_tag: str,
    run_idx: int,
    policy_timeout: int = 300,
) -> bool:
    """Policy 실행, on_shutdown 대기. 성공 여부 반환."""
    log_path = f"/tmp/e2e_policy_{run_tag}_run{run_idx}.log"

    env = _base_env()
    env.update(policy_env)
    env["AIC_DEMO_DIR"] = demo_dir
    env["AIC_F5_ENABLED"] = os.environ.get("AIC_F5_ENABLED", "1")

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
    """프로세스 정리 (republish → policy → engine)."""
    for pid in republish_pids:
        kill_process_tree(pid)

    pkill_pattern("aic_model")

    if engine_handle and engine_handle.get("pid", -1) > 0:
        kill_process_tree(engine_handle["pid"], grace_sec=3)


@task(name="postprocess", retries=2, retry_delay_seconds=5)
def postprocess_task(
    run_dir: str,
    demo_dir: str,
    engine_cfg: str,
    policy: str,
    seed: int,
    params: dict[str, float],
) -> dict:
    """run 산출물 재편."""
    if not ENGINE_RESULTS.exists():
        print(f"[error] {ENGINE_RESULTS} 없음 — 엔진/policy 실행 실패")
        return {"success": False, "run_dir": run_dir}

    print(f"[postprocess] {run_dir} 로 재편...")
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
    )

    params_json_path.unlink(missing_ok=True)

    if rc == 0:
        _append_log(f"[done] run 재편 완료: {run_dir}")
        return {"success": True, "run_dir": run_dir}
    else:
        print("[warn] postprocess 실패")
        return {"success": False, "run_dir": run_dir}


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
        engine_handle = launch_engine_task(engine_cfg_path, ground_truth, run_tag, run_idx)

        # 4. Republish (compressed only)
        republish_pids = launch_republish_task(use_compressed, run_tag, run_idx)

        # 5. Policy 실행
        per_trial = policy_cfg.get("per_trial")
        act_model_path = policy_cfg.get("act_model_path")
        if act_model_path:
            act_model_path = os.path.expanduser(act_model_path)
        policy_env = build_policy_env(policy_default, per_trial, act_model_path)

        run_policy_task(policy_env, demo_dir, run_tag, run_idx)

    finally:
        # 6. 정리
        cleanup_task(engine_handle, republish_pids)

    # 7. 후처리
    result = postprocess_task(run_dir, demo_dir, engine_cfg_path, policy_default, seed, sample)

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
) -> dict:
    """E2E 수집 파이프라인."""
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    collection = cfg.get("collection", {}) or {}
    params_cfg = cfg.get("parameters", {}) or {}
    sampling = cfg.get("sampling", {}) or {}

    runs = runs_override or collection.get("runs", 10)
    seed = seed_override or collection.get("seed", 42)
    strategy = sampling.get("strategy", "uniform")

    print("=== EXP-009 E2E 수집 ===")
    print(f"config: {config_path}")
    print(f"  runs: {runs}, seed: {seed}, strategy: {strategy}")

    # 프로젝트 디렉토리
    project_dir = str(Path(config_path).resolve().parent.parent)
    if not (Path(project_dir) / "policies").exists():
        project_dir = str(Path.cwd())

    # 진행 상태 초기화
    LOG_FILE.write_text("")
    _write_progress(0, runs)

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
    PROGRESS_FILE.write_text(json.dumps({
        "completed": runs,
        "total": runs,
        "status": "completed" if fail_count < runs else "failed",
        "fail_count": fail_count,
        "elapsed_sec": elapsed,
    }))

    return {
        "runs": runs,
        "fail_count": fail_count,
        "elapsed_sec": elapsed,
        "output_root": output_root,
    }
