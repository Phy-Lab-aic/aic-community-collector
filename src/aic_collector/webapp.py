# /// script
# dependencies = ["streamlit>=1.30", "pyyaml", "numpy"]
# ///
"""
AIC Community Data Collector — Web UI

커뮤니티 구성원이 브라우저에서 데이터를 수집하는 관리 도구.
collect_e2e.sh 위에 Streamlit UI를 얹은 구조.

실행: uv run src/aic_collector/webapp.py
      또는 pyproject.toml이 있으면: uv run aic-collector
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# streamlit을 직접 실행하기 위한 부트스트랩
# streamlit이 이 파일을 로드할 때는 __main__이 아니므로 부트스트랩을 건너뜀
if __name__ == "__main__" and "streamlit" not in sys.modules:
    os.execvp(
        sys.executable,
        [sys.executable, "-m", "streamlit", "run", __file__,
         "--server.headless", "true",
         "--server.address", "0.0.0.0",
         "--browser.gatherUsageStats", "false"],
    )

import streamlit as st
import yaml

# ---------------------------------------------------------------------------
# 경로 상수
# ---------------------------------------------------------------------------

# PROJECT_DIR = aic-community-collector/ (루트)
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
POLICIES_DIR = PROJECT_DIR / "policies"
PIXI_POLICIES_DIR = (
    Path.home()
    / "ws_aic/src/aic/.pixi/envs/default/lib/python3.12/site-packages/aic_example_policies/ros"
)
COLLECT_SCRIPT = PROJECT_DIR / "scripts/collect_e2e.sh"
DEPLOY_SCRIPT = PROJECT_DIR / "scripts/deploy_policies.sh"
DEFAULT_CONFIG = PROJECT_DIR / "configs/e2e_default.yaml"
OUTPUT_ROOT = Path.home() / "aic_community_e2e"

HIDDEN_POLICIES = {
    "__init__", "CollectWrapper", "CollectDispatchWrapper", "CheatCodeInner",
}

# 백그라운드 수집 상태 파일
BG_STATE_FILE = Path("/tmp/e2e_webapp_state.json")
BG_LOG_FILE = Path("/tmp/e2e_webapp_run.log")


# ---------------------------------------------------------------------------
# 백그라운드 수집 프로세스 관리
# ---------------------------------------------------------------------------


def bg_start(cmd: list[str], total_runs: int, config_summary: dict | None = None) -> None:
    """수집을 백그라운드로 시작. 상태를 JSON 파일에 기록."""
    BG_LOG_FILE.write_text("")
    proc = subprocess.Popen(
        cmd,
        stdout=open(BG_LOG_FILE, "w"),
        stderr=subprocess.STDOUT,
        cwd=str(PROJECT_DIR),
        start_new_session=True,  # 부모(streamlit)와 분리
    )
    state = {
        "pid": proc.pid,
        "cmd": cmd,
        "total_runs": total_runs,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config_summary": config_summary or {},
    }
    BG_STATE_FILE.write_text(json.dumps(state))


def bg_stop() -> bool:
    """실행 중인 백그라운드 수집을 중단. 성공 시 True."""
    bg = bg_status()
    if not bg or not bg.get("running"):
        return False
    pid = bg.get("pid")
    try:
        import signal
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        time.sleep(2)
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            pass
        return True
    except OSError:
        return False


def bg_status() -> dict | None:
    """현재 백그라운드 수집 상태 반환. 없으면 None."""
    if not BG_STATE_FILE.exists():
        return None
    try:
        state = json.loads(BG_STATE_FILE.read_text())
    except Exception:
        return None

    pid = state.get("pid")
    if not pid:
        return None

    # 프로세스 생존 확인
    try:
        os.kill(pid, 0)
        state["running"] = True
    except OSError:
        state["running"] = False

    # 로그에서 진행 상황 파싱
    state["completed_runs"] = 0
    state["current_label"] = ""
    state["log_lines"] = []
    state["finished_ok"] = False
    if BG_LOG_FILE.exists():
        try:
            lines = BG_LOG_FILE.read_text().splitlines()
            state["log_lines"] = lines[-100:]  # 최근 100줄
            for line in lines:
                if "run 재편 완료" in line or "[done]" in line:
                    state["completed_runs"] += 1
                if "E2E 수집 완료" in line:
                    state["finished_ok"] = True
                m = re.search(r"RUN (\d+)/(\d+)", line)
                if m:
                    state["current_label"] = f"RUN {m.group(1)}/{m.group(2)}"
                    state["total_runs"] = int(m.group(2))
        except Exception:
            pass

    # 프로세스 죽었고 로그도 거의 없으면 오래된 잔재 → 자동 정리
    if not state["running"] and not state["finished_ok"] and state["completed_runs"] == 0:
        bg_clear()
        return None

    return state


def bg_clear() -> None:
    """백그라운드 상태 파일 정리."""
    BG_STATE_FILE.unlink(missing_ok=True)


HISTORY_FILE = Path("/tmp/e2e_webapp_history.json")


def _save_run_history(config_summary: dict) -> None:
    """실행 이력을 JSON 파일에 추가."""
    history = []
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text())
        except Exception:
            pass
    history.append({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        **config_summary,
    })
    # 최근 50개만 유지
    HISTORY_FILE.write_text(json.dumps(history[-50:], ensure_ascii=False, indent=2))


def _load_run_history() -> list[dict]:
    """실행 이력 로드."""
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception:
        return []


def load_default_config() -> dict:
    """e2e_default.yaml을 로드해 기본 설정값으로 사용."""
    if DEFAULT_CONFIG.exists():
        with open(DEFAULT_CONFIG) as f:
            return yaml.safe_load(f) or {}
    return {}


# ---------------------------------------------------------------------------
# Policy 탐색
# ---------------------------------------------------------------------------


def discover_policies() -> list[str]:
    """사용 가능한 policy 이름 목록 반환."""
    result = ["cheatcode", "hybrid", "act"]
    seen = {"CollectCheatCode", "RunACTHybrid", "RunACTv1"} | HIDDEN_POLICIES

    for d in [PIXI_POLICIES_DIR, POLICIES_DIR]:
        if not d.exists():
            continue
        for f in sorted(d.glob("*.py")):
            name = f.stem
            if name not in seen and name not in HIDDEN_POLICIES:
                seen.add(name)
                result.append(name)
    return result


# ---------------------------------------------------------------------------
# 환경 점검
# ---------------------------------------------------------------------------


def check_environment() -> list[dict]:
    """환경 점검 항목 리스트 반환."""
    checks = []

    # Docker
    try:
        import shutil
        docker_path = shutil.which("docker")
        if not docker_path:
            checks.append({"name": "Docker", "ok": False,
                            "msg": "미설치 (docker 명령어 없음)",
                            "fix": "sudo apt install docker.io 또는 공식 문서 참고"})
        else:
            r = subprocess.run(
                ["docker", "ps", "-a", "--filter", "name=aic_eval", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0 and "permission denied" in (r.stderr or "").lower():
                checks.append({"name": "Docker", "ok": False,
                                "msg": "권한 없음 (docker 그룹 미등록)",
                                "fix": "sudo usermod -aG docker $USER 후 재로그인"})
            elif r.returncode != 0:
                checks.append({"name": "Docker", "ok": False,
                                "msg": f"실행 오류: {(r.stderr or '').strip()[:80]}",
                                "fix": None})
            else:
                ok = "aic_eval" in r.stdout
                checks.append({"name": "Docker (aic_eval)", "ok": ok,
                                "msg": "확인" if ok else "aic_eval 미발견",
                                "fix": None})
    except Exception as e:
        checks.append({"name": "Docker", "ok": False, "msg": str(e)[:80], "fix": None})

    # Distrobox
    try:
        r = subprocess.run(["which", "distrobox"], capture_output=True, timeout=5)
        ok = r.returncode == 0
        checks.append({"name": "Distrobox", "ok": ok,
                        "msg": "설치됨" if ok else "미설치", "fix": None})
    except Exception:
        checks.append({"name": "Distrobox", "ok": False, "msg": "확인 실패", "fix": None})

    # pixi
    ws = Path.home() / "ws_aic/src/aic"
    checks.append({"name": "pixi workspace", "ok": ws.exists(),
                    "msg": "확인" if ws.exists() else f"{ws} 없음", "fix": None})

    # Python packages
    for import_name, pip_name in [("yaml", "pyyaml"), ("numpy", "numpy")]:
        try:
            __import__(import_name)
            checks.append({"name": pip_name, "ok": True, "msg": "설치됨", "fix": None})
        except ImportError:
            checks.append({"name": pip_name, "ok": False, "msg": "미설치",
                            "fix": f"uv pip install --system {pip_name}"})

    # scipy
    try:
        __import__("scipy")
        checks.append({"name": "scipy", "ok": True, "msg": "설치됨 (LHS 가능)", "fix": None})
    except ImportError:
        checks.append({"name": "scipy", "ok": False, "msg": "미설치 (LHS 사용 시 필요)",
                        "fix": "uv pip install --system scipy"})

    return checks


# ---------------------------------------------------------------------------
# 결과 로드
# ---------------------------------------------------------------------------


def load_results() -> list[dict]:
    """~/aic_community_e2e/run_*/trial_*/tags.json 스캔."""
    rows = []
    if not OUTPUT_ROOT.exists():
        return rows
    for run_dir in sorted(OUTPUT_ROOT.glob("run_*")):
        # run 디렉토리명에서 시각 추출: run_01_20260408_233709 → 2026-04-08 23:37:09
        run_time = ""
        ts_match = re.search(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$", run_dir.name)
        if ts_match:
            y, mo, d, h, mi, s = ts_match.groups()
            run_time = f"{y}-{mo}-{d} {h}:{mi}:{s}"

        for trial_dir in sorted(run_dir.glob("trial_*_score*")):
            tags_path = trial_dir / "tags.json"
            if not tags_path.exists():
                continue
            try:
                with open(tags_path) as f:
                    t = json.load(f)
                rows.append({
                    "time": run_time,
                    "run": run_dir.name,
                    "trial": t.get("trial", "?"),
                    "score": round(t.get("scoring", {}).get("total", 0), 1),
                    "success": "✅" if t.get("success") else "❌",
                    "duration": round(t.get("trial_duration_sec", 0), 1),
                    "policy": t.get("policy", "?"),
                    "조기종료": "⚡" if t.get("early_terminated") else "",
                })
            except Exception:
                continue
    return rows


# ---------------------------------------------------------------------------
# Config 생성
# ---------------------------------------------------------------------------


def build_config(
    policy_default: str,
    per_trial: dict[int, str] | None,
    runs: int,
    seed: int,
    trials: list[int],
    sampling: str,
    custom_parameters: dict | None = None,
    act_model_path: str | None = None,
    ground_truth: bool = True,
    use_compressed: bool = True,
) -> dict[str, Any]:
    defaults = load_default_config()
    _default_act = (defaults.get("policy") or {}).get(
        "act_model_path",
        str(Path.home() / "ws_aic/src/aic/outputs/train/act_aic_v1_backup/checkpoints/last/pretrained_model"),
    )
    cfg: dict[str, Any] = {
        "schema_version": "0.1",
        "collection": {
            "runs": runs,
            "trials": trials,
            "output_root": str(OUTPUT_ROOT),
            "seed": seed,
        },
        "policy": {
            "default": policy_default,
            "act_model_path": act_model_path or _default_act,
        },
        "parameters": custom_parameters if custom_parameters else defaults.get("parameters", {}),
        "sampling": {"strategy": sampling},
        "engine": {
            "ground_truth": ground_truth,
            "use_compressed": use_compressed,
            "template": (defaults.get("engine") or {}).get("template", "configs/community_random_config.yaml"),
        },
    }
    if per_trial:
        cfg["policy"]["per_trial"] = per_trial
    return cfg


# ===========================================================================
# Streamlit UI
# ===========================================================================

st.set_page_config(page_title="AIC Community Collector", layout="centered")

# 커스텀 CSS
st.markdown("""
<style>
    /* 최대 폭 제한 */
    .block-container {
        max-width: 900px;
        padding-left: 2rem;
        padding-right: 2rem;
    }
    /* 카드 스타일 */
    div[data-testid="stExpander"] {
        border: 1px solid #e0e0e0;
        border-radius: 8px;
    }
    /* 탭 글자 크기 */
    button[data-baseweb="tab"] > div > p {
        font-size: 1.1rem;
        font-weight: 600;
    }
    /* 수집 시작 버튼 강조 */
    div.stButton > button[kind="primary"] {
        font-size: 1.1rem;
        padding: 0.6rem 2rem;
    }
</style>
""", unsafe_allow_html=True)

st.title("AIC Community Data Collector")

policies = discover_policies()
CONFIGS_DIR = PROJECT_DIR / "configs"

# --- 메인: 탭 4개 ---
tab_env, tab_collect, tab_results, tab_configs = st.tabs(
    ["🔍 환경 점검", "🚀 수집", "📊 결과", "⚙️ Config 관리"]
)

# --- 환경 점검 탭 ---
with tab_env:
    st.subheader("환경 점검")
    checks = check_environment()
    all_ok = True
    fixable = []

    for c in checks:
        if c["ok"]:
            st.markdown(f"✅ **{c['name']}** — {c['msg']}")
        else:
            all_ok = False
            st.markdown(f"❌ **{c['name']}** — {c['msg']}")
            if c["fix"]:
                fixable.append(c)

    if all_ok:
        st.success("모든 환경이 준비되었습니다. '수집' 탭으로 이동하세요.")
    elif fixable:
        st.warning(f"미비 항목 {len(fixable)}개 — 아래에서 자동 설치할 수 있습니다.")
        for c in fixable:
            col_name, col_btn = st.columns([3, 1])
            col_name.code(c["fix"])
            if col_btn.button(f"설치", key=f"fix_{c['name']}"):
                with st.spinner(f"{c['name']} 설치 중..."):
                    r = subprocess.run(c["fix"], shell=True, capture_output=True, text=True, timeout=120)
                if r.returncode == 0:
                    st.success(f"{c['name']} 설치 완료")
                    st.rerun()
                else:
                    st.error(f"설치 실패: {r.stderr[:200]}")
    else:
        st.warning("미비 항목은 수동 설치가 필요합니다.")

# --- 수집 탭 ---
with tab_collect:

    # ── Config 불러오기 ──
    config_files = sorted(CONFIGS_DIR.glob("e2e_*.yaml")) if CONFIGS_DIR.exists() else []
    config_names = ["(직접 설정)"] + [f.stem for f in config_files]

    loaded_cfg = {}
    selected_config = st.selectbox("Config 불러오기", config_names, index=0, key="load_config")
    if selected_config != "(직접 설정)":
        cfg_path = CONFIGS_DIR / f"{selected_config}.yaml"
        if cfg_path.exists():
            with open(cfg_path) as f:
                loaded_cfg = yaml.safe_load(f) or {}
            st.caption(f"📄 `configs/{selected_config}.yaml` 로드됨")

    # 로드된 config에서 기본값 추출
    _lc = loaded_cfg.get("collection", {}) or {}
    _lp = loaded_cfg.get("policy", {}) or {}
    _ls = loaded_cfg.get("sampling", {}) or {}
    _default_runs = _lc.get("runs", 3)
    _default_seed = _lc.get("seed", 42)
    _default_trials = _lc.get("trials", [1, 2, 3])
    _default_policy = _lp.get("default", "cheatcode")
    _default_sampling = _ls.get("strategy", "lhs")
    _default_per_trial = _lp.get("per_trial") or {}

    st.divider()

    # ── 기본 설정 ──
    st.subheader("기본 설정")
    col_policy, col_runs, col_seed, col_sampling = st.columns(4)
    with col_policy:
        _pi = policies.index(_default_policy) if _default_policy in policies else 0
        policy_default = st.selectbox("Policy", policies, index=_pi)
    with col_runs:
        runs = st.number_input("Runs", min_value=1, max_value=100, value=_default_runs)
    with col_seed:
        seed = st.number_input("Seed", min_value=0, value=_default_seed)
    with col_sampling:
        _si = ["lhs", "uniform", "sobol"].index(_default_sampling) if _default_sampling in ["lhs", "uniform", "sobol"] else 0
        sampling = st.selectbox("Sampling", ["lhs", "uniform", "sobol"], index=_si)

    # 고급 옵션 (ACT 모델 경로, ground_truth)
    with st.expander("🔧 고급 옵션", expanded=False):
        _le = loaded_cfg.get("engine", {}) or {}
        _default_gt = _le.get("ground_truth", True)
        _default_act_path = _lp.get(
            "act_model_path",
            str(Path.home() / "ws_aic/src/aic/outputs/train/act_aic_v1_backup/checkpoints/last/pretrained_model"),
        )

        ground_truth = st.toggle("Ground Truth 모드", value=_default_gt,
                                  help="켜면 시뮬레이터의 정확한 TF 정보를 사용 (수집용). 끄면 평가 모드 (CheatCode 사용 불가).")
        use_compressed = st.toggle("이미지 압축 (JPEG)", value=True,
                                    help="켜면 카메라 이미지를 JPEG 압축해서 기록 (~3GB/run). 끄면 raw 이미지 (~58GB/run).")
        act_model_path = st.text_input("ACT 모델 경로", value=_default_act_path,
                                       help="hybrid/act policy 사용 시 학습된 모델 경로")

    # ── Trial 설정 (체크박스 + policy + 파라미터를 trial별로 그룹핑) ──
    st.subheader("Trial 설정")
    st.caption("수집할 trial을 선택하고, 필요하면 policy와 파라미터 범위를 조절하세요.")

    # 파라미터 기본값
    _params = (loaded_cfg.get("parameters") or load_default_config().get("parameters") or {})
    PHYS_LIMITS = {
        "nic_translation": (-0.03, 0.03),
        "nic_yaw": (-0.35, 0.35),
        "sc_translation": (-0.10, 0.10),
        "sc_yaw": (-0.35, 0.35),
    }

    # trial별 삽입 대상 파라미터만 매핑
    trial_info = {
        1: {
            "desc": "NIC 카드 0 — SFP 삽입 (보통)",
            "params": [
                ("nic0_translation", "위치 (m)", PHYS_LIMITS["nic_translation"]),
                ("nic0_yaw", "회전 (rad)", PHYS_LIMITS["nic_yaw"]),
            ],
        },
        2: {
            "desc": "NIC 카드 1 — SFP 삽입 (보통)",
            "params": [
                ("nic1_translation", "위치 (m)", PHYS_LIMITS["nic_translation"]),
                ("nic1_yaw", "회전 (rad)", PHYS_LIMITS["nic_yaw"]),
            ],
        },
        3: {
            "desc": "SC 포트 — SC 삽입 (어려움)",
            "params": [
                ("sc1_translation", "위치 (m)", PHYS_LIMITS["sc_translation"]),
                ("sc1_yaw", "회전 (rad)", PHYS_LIMITS["sc_yaw"]),
            ],
        },
    }

    trials = []
    trial_policies = {}
    custom_params = {}

    for i in (1, 2, 3):
        info = trial_info[i]

        col_check, col_desc, col_pol = st.columns([1, 3, 3])
        with col_check:
            on = st.checkbox(f"Trial {i}", value=(i in _default_trials), key=f"trial_{i}_on")
        with col_desc:
            st.caption(info["desc"])
        with col_pol:
            per_trial_options = ["default (위 설정 사용)"] + policies
            _pt_val = _default_per_trial.get(i) or _default_per_trial.get(str(i))
            _pt_idx = per_trial_options.index(_pt_val) if _pt_val in per_trial_options else 0
            val = st.selectbox(
                "Policy",
                per_trial_options,
                index=_pt_idx,
                key=f"trial_{i}_policy",
                disabled=not on,
                label_visibility="collapsed",
            )
            if val != "default (위 설정 사용)" and on:
                trial_policies[i] = val
        if on:
            trials.append(i)

        # 해당 trial의 삽입 대상 파라미터 범위
        with st.expander(f"📐 Trial {i} 파라미터 범위", expanded=False):
            for key, label, (phys_min, phys_max) in info["params"]:
                p = _params.get(key, {})
                cur_min = float(p.get("min", phys_min))
                cur_max = float(p.get("max", phys_max))
                slider_val = st.slider(
                    label,
                    min_value=phys_min,
                    max_value=phys_max,
                    value=(cur_min, cur_max),
                    step=0.001,
                    format="%.4f",
                    key=f"param_{key}",
                    disabled=not on,
                )
                custom_params[key] = {"min": slider_val[0], "max": slider_val[1]}

    # ── 공통 씬 파라미터 (SC0) ──
    with st.expander("📐 공통 파라미터 — SC 카드 0 (Trial 1, 2 씬 배경)", expanded=False):
        st.caption("Trial 1, 2의 씬에 배치되는 SC 카드 위치. 삽입 대상은 아니지만 씬 다양성에 영향.")
        for key, label, (phys_min, phys_max) in [
            ("sc0_translation", "SC0 위치 (m)", PHYS_LIMITS["sc_translation"]),
            ("sc0_yaw", "SC0 회전 (rad)", PHYS_LIMITS["sc_yaw"]),
        ]:
            p = _params.get(key, {})
            cur_min = float(p.get("min", phys_min))
            cur_max = float(p.get("max", phys_max))
            slider_val = st.slider(
                label,
                min_value=phys_min,
                max_value=phys_max,
                value=(cur_min, cur_max),
                step=0.001,
                format="%.4f",
                key=f"param_{key}",
            )
            custom_params[key] = {"min": slider_val[0], "max": slider_val[1]}

    st.divider()

    # ── 미리보기 ──
    with st.expander("📋 파라미터 미리보기", expanded=False):
        cfg = build_config(policy_default, trial_policies or None, runs, seed, trials, sampling, custom_params, act_model_path, ground_truth, use_compressed)
        try:
            sys.path.insert(0, str(PROJECT_DIR / "src/aic_collector"))
            from sampler import sample_parameters
            samples = sample_parameters(cfg["parameters"], sampling, runs, seed)
            if samples:
                import pandas as pd
                df = pd.DataFrame(samples)
                df.index = [f"run_{i+1}" for i in range(len(df))]
                df = df.round(4)
                st.dataframe(df, width="stretch")
                info_parts = [f"**{runs}** runs", f"trials **{trials}**", f"sampling **{sampling}**", f"seed **{seed}**"]
                if trial_policies:
                    info_parts.append(f"per-trial: {trial_policies}")
                st.markdown(" · ".join(info_parts))
            else:
                st.info("샘플 없음 — trial을 1개 이상 선택하세요.")
        except Exception as e:
            st.error(f"미리보기 실패: {e}")

    # ── Config 저장 ──
    with st.expander("💾 현재 설정을 Config 파일로 저장"):
        save_name = st.text_input(
            "파일 이름 (e2e_ 접두사 자동 추가)",
            placeholder="my_experiment",
            key="save_config_name",
        )
        if st.button("저장", key="btn_save_config"):
            if save_name:
                clean_name = re.sub(r"[^\w\-]", "_", save_name)
                if not clean_name.startswith("e2e_"):
                    clean_name = f"e2e_{clean_name}"
                save_path = CONFIGS_DIR / f"{clean_name}.yaml"
                cfg = build_config(policy_default, trial_policies or None, runs, seed, trials, sampling, custom_params, act_model_path, ground_truth, use_compressed)
                CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
                with open(save_path, "w") as f:
                    yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
                st.success(f"저장됨: `configs/{clean_name}.yaml`")
            else:
                st.warning("파일 이름을 입력하세요.")

    # ── 수집 실행 ──
    st.markdown("")  # spacing

    bg = bg_status()

    if bg and bg["running"]:
        # ── 수집 진행 중 (백그라운드) ──
        total = bg.get("total_runs", 1)
        done = bg.get("completed_runs", 0)
        label = bg.get("current_label", "")
        pct = done / total if total > 0 else 0

        # 상태 표시 (st.status 대신 일반 위젯 — rerun 시 깜빡임 방지)
        if label:
            st.info(f"🔄 수집 진행 중... **{label}**", icon="🔄")
        else:
            st.info("🔄 수집 시작 중...", icon="🔄")

        st.caption(f"시작: {bg.get('started_at', '?')} | PID: {bg.get('pid', '?')}")

        if done > 0:
            st.progress(pct, text=f"✅ {done}/{total} runs 완료")
        else:
            st.progress(0, text=f"⏳ {label} 진행 중..." if label else "⏳ 엔진 기동 중...")

        with st.expander("실행 로그", expanded=True):
            log_lines = bg.get("log_lines", [])
            if log_lines:
                st.code("\n".join(log_lines[-50:]), language="bash")
            else:
                st.caption("로그 대기 중...")

        if st.button("⏹️ 수집 중단", key="btn_stop", type="secondary"):
            if bg_stop():
                st.warning("수집이 중단되었습니다.")
                bg_clear()
                time.sleep(1)
                st.rerun()
            else:
                st.error("프로세스 중단 실패")

        # 자동 새로고침 (3초마다 폴링)
        time.sleep(3)
        st.rerun()

    elif bg and not bg["running"]:
        # ── 수집 완료 (백그라운드 프로세스 종료됨) ──
        total = bg.get("total_runs", 1)
        done = bg.get("completed_runs", 0)

        if bg.get("finished_ok"):
            st.success(f"🎉 수집 완료! ({done}/{total} runs)")
            st.info(f"📁 저장 경로: `{OUTPUT_ROOT}`")
        elif done > 0:
            st.warning(f"수집이 중단됐지만 {done}/{total} runs는 완료됐습니다.")
            st.info(f"📁 저장 경로: `{OUTPUT_ROOT}`")
        else:
            st.error("수집 실패 — 완료된 run이 없습니다. 로그를 확인하세요.")

        # 로그 보기
        with st.expander("실행 로그", expanded=False):
            log_lines = bg.get("log_lines", [])
            if log_lines:
                st.code("\n".join(log_lines[-100:]), language="bash")

        # 상태 자동 정리 — 다음 새로고침 시 대기 상태로 복귀
        bg_clear()

    else:
        # ── 대기 중 → 수집 시작 가능 ──
        if st.button("🚀 수집 시작", key="btn_run", type="primary"):
            if not trials:
                st.error("최소 1개 trial을 선택하세요.")
            else:
                cfg = build_config(policy_default, trial_policies or None, runs, seed, trials, sampling, custom_params, act_model_path, ground_truth, use_compressed)

                # config를 고정 경로에 저장 (백그라운드에서 참조)
                config_path = Path("/tmp/e2e_webapp_config.yaml")
                with open(config_path, "w") as f:
                    yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)

                cmd = [
                    str(COLLECT_SCRIPT),
                    "--config", str(config_path),
                    "--runs", str(runs),
                    "--seed", str(seed),
                ]

                config_summary = {
                    "policy": policy_default,
                    "runs": runs,
                    "seed": seed,
                    "trials": trials,
                    "sampling": sampling,
                    "per_trial": trial_policies or None,
                    "ground_truth": ground_truth,
                }
                bg_start(cmd, total_runs=runs, config_summary=config_summary)

                # 실행 이력 저장
                _save_run_history(config_summary)

                st.rerun()  # 즉시 진행 중 상태로 전환

# --- 결과 탭 ---
with tab_results:
    st.subheader("수집 결과")
    st.caption(f"📁 저장 경로: `{OUTPUT_ROOT}`")

    if st.button("새로고침", key="refresh_results"):
        pass  # rerun 트리거

    rows = load_results()
    if rows:
        import pandas as pd
        df = pd.DataFrame(rows)

        # 요약
        success_count = (df["success"] == "✅").sum()
        total = len(df)
        col_a, col_b, col_c = st.columns(3)
        col_a.metric("총 Trials", total)
        col_b.metric("성공", f"{success_count} ({100*success_count/total:.0f}%)")
        col_c.metric("평균 점수", f"{df['score'].mean():.1f}")

        # 테이블
        st.dataframe(df, width="stretch", hide_index=True)

        # CSV 다운로드 + 삭제
        col_dl, col_del = st.columns(2)
        with col_dl:
            csv_data = df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "📥 결과 CSV 다운로드",
                data=csv_data,
                file_name="aic_collection_results.csv",
                mime="text/csv",
            )
        with col_del:
            with st.popover("🗑️ 결과 정리"):
                st.warning("선택한 run을 삭제합니다. 이 작업은 되돌릴 수 없습니다.")
                run_dirs = sorted(set(df["run"].tolist()))
                del_target = st.selectbox("삭제할 run", ["(선택)"] + run_dirs, key="del_run")
                if st.button("삭제 실행", key="btn_del_run") and del_target != "(선택)":
                    import shutil
                    target_path = OUTPUT_ROOT / del_target
                    if target_path.exists():
                        shutil.rmtree(target_path)
                        st.success(f"{del_target} 삭제됨")
                        st.rerun()
    else:
        st.info("수집된 결과가 없습니다. 수집을 실행하세요.")

    # 실행 이력
    with st.expander("📜 실행 이력", expanded=False):
        history = _load_run_history()
        if history:
            for h in reversed(history):
                per_trial_str = f" | per-trial: {h['per_trial']}" if h.get("per_trial") else ""
                gt_str = "" if h.get("ground_truth", True) else " | GT:off"
                st.caption(
                    f"**{h['time']}** — {h.get('policy','?')} | "
                    f"{h.get('runs','?')} runs | trials {h.get('trials','?')} | "
                    f"{h.get('sampling','?')} | seed {h.get('seed','?')}"
                    f"{per_trial_str}{gt_str}"
                )
        else:
            st.caption("실행 이력이 없습니다.")

# --- Config 관리 탭 ---
with tab_configs:
    st.subheader("Config 파일 관리")
    st.caption(f"📁 경로: `{CONFIGS_DIR}`")

    configs = sorted(CONFIGS_DIR.glob("e2e_*.yaml")) if CONFIGS_DIR.exists() else []

    if not configs:
        st.info("저장된 config 파일이 없습니다.")
    else:
        for cfg_path in configs:
            with st.expander(f"📄 {cfg_path.name}"):
                try:
                    with open(cfg_path) as f:
                        content = f.read()
                        cfg_data = yaml.safe_load(content) or {}

                    # 요약 정보
                    _cc = cfg_data.get("collection", {}) or {}
                    _cp = cfg_data.get("policy", {}) or {}
                    _cs = cfg_data.get("sampling", {}) or {}
                    col1, col2, col3, col4 = st.columns(4)
                    col1.metric("Runs", _cc.get("runs", "?"))
                    col2.metric("Trials", str(_cc.get("trials", "?")))
                    col3.metric("Policy", _cp.get("default", "?"))
                    col4.metric("Sampling", _cs.get("strategy", "?"))

                    if _cp.get("per_trial"):
                        st.caption(f"Per-trial: {_cp['per_trial']}")

                    # YAML 원본
                    st.code(content, language="yaml")

                    # 삭제 버튼
                    if cfg_path.name not in ("e2e_default.yaml",):  # 기본 파일은 보호
                        if st.button(f"삭제", key=f"del_{cfg_path.name}"):
                            cfg_path.unlink()
                            st.success(f"{cfg_path.name} 삭제됨")
                            st.rerun()
                    else:
                        st.caption("(기본 config — 삭제 불가)")
                except Exception as e:
                    st.error(f"파일 읽기 실패: {e}")
