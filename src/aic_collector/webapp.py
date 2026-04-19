# /// script
# dependencies = ["streamlit>=1.30", "pyyaml", "numpy"]
# ///
"""
AIC Community Data Collector — Web UI

커뮤니티 구성원이 브라우저에서 데이터를 수집하는 관리 도구.
Prefect flow(aic_collector.prefect) 위에 Streamlit UI를 얹은 구조.

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
    # Prefect 서버 선기동 (fire-and-forget).
    # Streamlit 페이지는 websocket 연결 전까지 Python 코드가 안 돌기 때문에,
    # 브라우저 없이도 워커가 쓸 수 있도록 여기서 미리 띄운다.
    _PREFECT_PID_FILE = Path("/tmp/e2e_prefect_server.pid")
    _PREFECT_LOG_FILE = Path("/tmp/e2e_prefect_server.log")

    def _bootstrap_prefect() -> None:
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:4200/api/health", timeout=1.5
            ) as r:
                if 200 <= r.status < 300:
                    return  # already up
        except (urllib.error.URLError, OSError, TimeoutError):
            pass
        # 이전 pid가 살아있으면 중복 기동 방지
        if _PREFECT_PID_FILE.exists():
            try:
                os.kill(int(_PREFECT_PID_FILE.read_text().strip()), 0)
                return
            except (ValueError, ProcessLookupError, PermissionError):
                _PREFECT_PID_FILE.unlink(missing_ok=True)

        _PREFECT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(_PREFECT_LOG_FILE, "a")
        # 0.0.0.0 바인딩 — webapp이 LAN으로 노출된 경우(--server.address 0.0.0.0)
        # Prefect 대시보드 링크도 같은 호스트에서 열려야 하므로 외부 접근 허용.
        p = subprocess.Popen(
            ["uv", "run", "prefect", "server", "start",
             "--host", "0.0.0.0", "--port", "4200"],
            stdout=log_fh, stderr=subprocess.STDOUT,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
            start_new_session=True,
        )
        _PREFECT_PID_FILE.write_text(str(p.pid))
        print(f"[webapp] Prefect 서버 시작 요청 (pid={p.pid}) — 로그: {_PREFECT_LOG_FILE}")

    _bootstrap_prefect()
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

# streamlit run으로 실행될 때는 패키지 설치 없이도 import 가능해야 함
_SRC_DIR = str(PROJECT_DIR / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

POLICIES_DIR = PROJECT_DIR / "policies"
PIXI_POLICIES_DIR = (
    Path.home()
    / "ws_aic/src/aic/.pixi/envs/default/lib/python3.12/site-packages/aic_example_policies/ros"
)
OUTPUT_ROOT = Path.home() / "aic_community_e2e"

HIDDEN_POLICIES = {
    "__init__", "CollectWrapper", "CollectDispatchWrapper", "CheatCodeInner",
}


# 큐 워커 상태 파일 (Phase 2b)
WORKER_STATE_FILE = Path("/tmp/aic_worker_state.json")
WORKER_PID_FILE = Path("/tmp/aic_worker_pid.txt")
WORKER_LOG_FILE = Path("/tmp/aic_worker_run.log")

# Prefect 서버
PREFECT_SERVER_URL = "http://127.0.0.1:4200"
PREFECT_PORT = 4200
PREFECT_PID_FILE = Path("/tmp/e2e_prefect_server.pid")
PREFECT_LOG_FILE = Path("/tmp/e2e_prefect_server.log")


def get_prefect_ui_url() -> str:
    """브라우저에서 접근 가능한 Prefect UI URL을 반환.

    Streamlit의 Host 헤더에서 호스트명을 추출해 :4200 으로 연결.
    원격 접속(예: 192.168.x.y:8501)에서도 같은 호스트의 4200 포트로 연결 가능.
    """
    try:
        host_header = st.context.headers.get("Host", "")
        if host_header:
            host = host_header.rsplit(":", 1)[0] if ":" in host_header else host_header
            return f"http://{host}:{PREFECT_PORT}"
    except Exception:
        pass
    return PREFECT_SERVER_URL


def _prefect_server_healthy(timeout_sec: float = 1.5) -> bool:
    """4200 포트에 prefect 서버가 응답하는지 확인."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"{PREFECT_SERVER_URL}/api/health", timeout=timeout_sec
        ) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def ensure_prefect_server(wait_sec: int = 30) -> bool:
    """4200 서버가 안 떠 있으면 백그라운드로 기동 후 ready될 때까지 대기.

    Streamlit rerun마다 호출돼도 안전: 이미 떠 있으면 즉시 True 반환.
    """
    if _prefect_server_healthy():
        return True

    # 이전 PID가 살아있는지 확인 (죽은 서버 감지)
    if PREFECT_PID_FILE.exists():
        try:
            old_pid = int(PREFECT_PID_FILE.read_text().strip())
            os.kill(old_pid, 0)  # signal 0 = alive check
            # 살아있는데 health 실패 → 기동 중이거나 문제. 추가 대기 후 재판정.
            for _ in range(wait_sec):
                time.sleep(1)
                if _prefect_server_healthy():
                    return True
            # 그래도 안 되면 죽은 걸로 간주하고 아래서 새로 띄움
        except (ValueError, ProcessLookupError, PermissionError):
            PREFECT_PID_FILE.unlink(missing_ok=True)

    # 새로 기동 — webapp이 LAN 노출 구성이면 Prefect UI도 외부 접근 필요
    PREFECT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(PREFECT_LOG_FILE, "a")
    proc = subprocess.Popen(
        [
            "uv", "run", "prefect", "server", "start",
            "--host", "0.0.0.0", "--port", str(PREFECT_PORT),
        ],
        stdout=log_fh, stderr=subprocess.STDOUT,
        cwd=str(PROJECT_DIR),
        start_new_session=True,
    )
    PREFECT_PID_FILE.write_text(str(proc.pid))

    for _ in range(wait_sec):
        time.sleep(1)
        if _prefect_server_healthy():
            return True
    return False


def render_queue_bar_html(c: Any) -> str:
    """task_type 하나의 pending/running/done/failed를 1줄 수평 스택바 HTML로.

    Args:
        c: QueueCounts 호환 오브젝트 (pending·running·done·failed 속성).
           순환 import 회피를 위해 Any로 타입링.
    """
    pending = int(getattr(c, "pending", 0))
    running = int(getattr(c, "running", 0))
    done = int(getattr(c, "done", 0))
    failed = int(getattr(c, "failed", 0))
    total = pending + running + done + failed
    if total == 0:
        return (
            '<div style="height:22px;display:flex;align-items:center;'
            'padding-left:8px;color:#888;font-size:12px;'
            'border:1px dashed #ccc;border-radius:4px;">비어있음</div>'
        )
    segments = [
        ("pending", pending, "#6c757d"),
        ("running", running, "#fd7e14"),
        ("done",    done,    "#198754"),
        ("failed",  failed,  "#dc3545"),
    ]
    parts: list[str] = []
    for name, n, color in segments:
        if n == 0:
            continue
        pct = 100.0 * n / total
        label = f"{name} {n}" if pct >= 12 else str(n)
        parts.append(
            f'<div title="{name}: {n}" style="background:{color};'
            f'width:{pct:.3f}%;display:flex;align-items:center;'
            f'justify-content:center;color:white;font-size:11px;'
            f'overflow:hidden;white-space:nowrap;">{label}</div>'
        )
    return (
        '<div style="display:flex;height:22px;border-radius:4px;'
        'overflow:hidden;font-family:sans-serif;">'
        + "".join(parts) + '</div>'
    )


def render_queue_status_block(
    counts: dict[str, Any],
    task_types: tuple[str, ...],
) -> None:
    """task_type별 1줄 스택바 + 총 active count 라벨을 렌더.

    두 탭(작업 관리·작업 실행)에서 동일 UI로 쓰기 위한 헬퍼.
    `counts`는 `{task_type: QueueCounts | None}` 형태.
    """
    for tt in task_types:
        c = counts.get(tt)
        lbl_col, bar_col = st.columns([1, 6])
        with lbl_col:
            if c is None:
                st.markdown(f"**{tt.upper()}**")
                st.caption("(큐 루트 없음)")
            else:
                active = c.pending + c.running + c.done + c.failed
                st.markdown(f"**{tt.upper()}** · {active}")
        with bar_col:
            if c is None:
                st.caption("—")
            else:
                st.markdown(render_queue_bar_html(c), unsafe_allow_html=True)
                if getattr(c, "legacy", 0):
                    st.caption(
                        f"⚠ legacy {c.legacy}개 — 상태 디렉토리 밖. 아래에서 이동 가능."
                    )


def render_parameters_svg(user_ranges: dict) -> str:
    """Parameters의 물리적 의미를 도식화한 SVG 반환.

    각 파라미터가 실제 시뮬에서 어떤 움직임/편차를 유발하는지 표현:
      - NIC/SC translation: rail 위에서 슬라이드 (min/max 위치 ghost 카드)
      - NIC yaw: 회전된 카드 outline (±yaw 각도)
      - Gripper xy: top-down 원 (반경 = ±spread)
      - Gripper z: 세로 바 (±spread)
      - Gripper rpy: 회전 arc (±각도)
    점선 = AIC 공식 최대 허용, 색칠 = 현재 선택.
    """
    import math

    AIC_NIC_TR   = (-0.0215, 0.0234)
    AIC_NIC_YAW  = (-0.1745, 0.1745)
    AIC_SC_TR    = (-0.06, 0.055)
    AIC_GRIP_XY  = 0.002
    AIC_GRIP_Z   = 0.002
    AIC_GRIP_RPY = 0.04

    u_nic_tr  = user_ranges["nic_translation"]
    u_nic_yaw = user_ranges["nic_yaw"]
    u_sc_tr   = user_ranges["sc_translation"]
    u_gr_xy   = user_ranges["gripper_xy"]
    u_gr_z    = user_ranges["gripper_z"]
    u_gr_rpy  = user_ranges["gripper_rpy"]

    C_NIC = "#0d6efd"; C_NIC_L = "#cfe2ff"
    C_SC  = "#198754"; C_SC_L  = "#d1e7dd"
    C_GR  = "#fd7e14"; C_GR_L  = "#ffe5d0"
    C_RAIL = "#adb5bd"
    C_TEXT = "#212529"; C_MUTED = "#868e96"

    e: list[str] = []

    # ───── Section 1: NIC card ─────
    sec1_top = 0
    e.append(
        f'<text x="20" y="{sec1_top + 22}" font-size="12" font-weight="bold" fill="{C_NIC}">'
        f'📍 NIC card — rail 위에서 슬라이드 + 회전</text>'
    )

    rx1, rx2, ry = 80, 520, sec1_top + 130
    tr_lo, tr_hi = AIC_NIC_TR
    def nic_x(tr: float) -> float:
        return rx1 + (tr - tr_lo) / (tr_hi - tr_lo) * (rx2 - rx1)
    x_nom = nic_x(0.0)
    x_u1, x_u2 = nic_x(u_nic_tr[0]), nic_x(u_nic_tr[1])

    e.append(f'<line x1="{rx1}" y1="{ry}" x2="{rx2}" y2="{ry}" stroke="{C_RAIL}" stroke-width="3" stroke-linecap="round" />')
    for x in (rx1, rx2):
        e.append(f'<line x1="{x}" y1="{ry - 10}" x2="{x}" y2="{ry + 10}" stroke="{C_MUTED}" stroke-width="2" />')
    e.append(f'<line x1="{x_nom}" y1="{ry - 14}" x2="{x_nom}" y2="{ry + 14}" stroke="{C_TEXT}" stroke-width="1" stroke-dasharray="2 2" />')
    e.append(f'<text x="{x_nom}" y="{ry + 26}" font-size="8" fill="{C_MUTED}" text-anchor="middle">0</text>')
    e.append(f'<text x="{rx1}" y="{ry + 26}" font-size="8" fill="{C_MUTED}" text-anchor="middle" font-family="monospace">{tr_lo*1000:+.1f}mm</text>')
    e.append(f'<text x="{rx2}" y="{ry + 26}" font-size="8" fill="{C_MUTED}" text-anchor="middle" font-family="monospace">{tr_hi*1000:+.1f}mm</text>')
    e.append(f'<line x1="{x_u1}" y1="{ry}" x2="{x_u2}" y2="{ry}" stroke="{C_NIC}" stroke-width="6" stroke-linecap="round" />')

    cw, ch = 50, 22
    cy_top = ry - ch - 2
    e.append(f'<rect x="{x_nom - cw/2}" y="{cy_top}" width="{cw}" height="{ch}" rx="3" fill="{C_NIC_L}" stroke="{C_NIC}" stroke-width="1.5" />')
    for dx in (-cw/4, cw/4):
        e.append(f'<circle cx="{x_nom + dx}" cy="{ry - ch/2 - 2}" r="3" fill="{C_NIC}" />')
    e.append(f'<text x="{x_nom}" y="{cy_top - 4}" font-size="8" fill="{C_TEXT}" text-anchor="middle" font-weight="bold">NIC</text>')

    for x_ghost, tr_val in ((x_u1, u_nic_tr[0]), (x_u2, u_nic_tr[1])):
        if abs(tr_val) > 1e-6:
            e.append(f'<rect x="{x_ghost - cw/2}" y="{cy_top}" width="{cw}" height="{ch}" rx="3" fill="none" stroke="{C_NIC}" stroke-width="1" stroke-dasharray="3 2" opacity="0.5" />')

    tr_span_mm = (u_nic_tr[1] - u_nic_tr[0]) * 1000
    e.append(f'<text x="{(x_u1 + x_u2)/2}" y="{cy_top - 14}" font-size="10" fill="{C_NIC}" text-anchor="middle" font-weight="600">← translation ≤ {tr_span_mm:.1f} mm →</text>')

    # yaw 시각화 (오른쪽)
    yaw_abs = max(abs(u_nic_yaw[0]), abs(u_nic_yaw[1]))
    yaw_deg = yaw_abs * 180 / math.pi
    yax, yay = 620, sec1_top + 100
    mw, mh = 36, 18
    if yaw_abs > 1e-4:
        for ang in (-yaw_deg, yaw_deg):
            e.append(f'<rect x="{yax - mw/2}" y="{yay - mh/2}" width="{mw}" height="{mh}" rx="2" fill="none" stroke="{C_NIC}" stroke-width="1.2" stroke-dasharray="3 2" opacity="0.6" transform="rotate({ang} {yax} {yay})" />')
    e.append(f'<rect x="{yax - mw/2}" y="{yay - mh/2}" width="{mw}" height="{mh}" rx="2" fill="{C_NIC_L}" stroke="{C_NIC}" stroke-width="1.2" />')
    if yaw_abs > 1e-4:
        arc_r = 32
        a_x1 = yax + arc_r * math.sin(-yaw_abs); a_y1 = yay - arc_r * math.cos(-yaw_abs)
        a_x2 = yax + arc_r * math.sin(+yaw_abs); a_y2 = yay - arc_r * math.cos(+yaw_abs)
        e.append(f'<path d="M {a_x1:.2f} {a_y1:.2f} A {arc_r} {arc_r} 0 0 1 {a_x2:.2f} {a_y2:.2f}" fill="none" stroke="{C_NIC}" stroke-width="2" />')
    aic_yaw_v = AIC_NIC_YAW[1]
    arc_r_aic = 46
    a_x1 = yax + arc_r_aic * math.sin(-aic_yaw_v); a_y1 = yay - arc_r_aic * math.cos(-aic_yaw_v)
    a_x2 = yax + arc_r_aic * math.sin(+aic_yaw_v); a_y2 = yay - arc_r_aic * math.cos(+aic_yaw_v)
    e.append(f'<path d="M {a_x1:.2f} {a_y1:.2f} A {arc_r_aic} {arc_r_aic} 0 0 1 {a_x2:.2f} {a_y2:.2f}" fill="none" stroke="{C_MUTED}" stroke-width="1" stroke-dasharray="3 2" />')
    e.append(f'<text x="{yax}" y="{yay + 58}" font-size="10" fill="{C_NIC}" text-anchor="middle" font-weight="600">yaw ≤ ±{yaw_deg:.1f}°</text>')
    e.append(f'<text x="{yax}" y="{yay + 71}" font-size="8" fill="{C_MUTED}" text-anchor="middle">AIC ±10°</text>')

    e.append(f'<line x1="10" y1="{sec1_top + 185}" x2="720" y2="{sec1_top + 185}" stroke="#dee2e6" stroke-width="1" />')

    # ───── Section 2: SC port ─────
    sec2_top = 200
    e.append(f'<text x="20" y="{sec2_top + 22}" font-size="12" font-weight="bold" fill="{C_SC}">📍 SC port — rail 위에서 슬라이드 (yaw 고정 = 0)</text>')

    srx1, srx2, sry = 80, 520, sec2_top + 95
    sc_lo, sc_hi = AIC_SC_TR
    def sc_x_of(tr: float) -> float:
        return srx1 + (tr - sc_lo) / (sc_hi - sc_lo) * (srx2 - srx1)
    sx_nom = sc_x_of(0.0)
    sx_u1, sx_u2 = sc_x_of(u_sc_tr[0]), sc_x_of(u_sc_tr[1])

    e.append(f'<line x1="{srx1}" y1="{sry}" x2="{srx2}" y2="{sry}" stroke="{C_RAIL}" stroke-width="3" stroke-linecap="round" />')
    for x in (srx1, srx2):
        e.append(f'<line x1="{x}" y1="{sry - 10}" x2="{x}" y2="{sry + 10}" stroke="{C_MUTED}" stroke-width="2" />')
    e.append(f'<line x1="{sx_nom}" y1="{sry - 14}" x2="{sx_nom}" y2="{sry + 14}" stroke="{C_TEXT}" stroke-width="1" stroke-dasharray="2 2" />')
    e.append(f'<text x="{sx_nom}" y="{sry + 26}" font-size="8" fill="{C_MUTED}" text-anchor="middle">0</text>')
    e.append(f'<text x="{srx1}" y="{sry + 26}" font-size="8" fill="{C_MUTED}" text-anchor="middle" font-family="monospace">{sc_lo*1000:+.0f}mm</text>')
    e.append(f'<text x="{srx2}" y="{sry + 26}" font-size="8" fill="{C_MUTED}" text-anchor="middle" font-family="monospace">{sc_hi*1000:+.0f}mm</text>')
    e.append(f'<line x1="{sx_u1}" y1="{sry}" x2="{sx_u2}" y2="{sry}" stroke="{C_SC}" stroke-width="6" stroke-linecap="round" />')

    sw, sh = 30, 20
    sc_y_top = sry - sh - 2
    e.append(f'<rect x="{sx_nom - sw/2}" y="{sc_y_top}" width="{sw}" height="{sh}" rx="3" fill="{C_SC_L}" stroke="{C_SC}" stroke-width="1.5" />')
    e.append(f'<circle cx="{sx_nom}" cy="{sry - sh/2 - 2}" r="3" fill="{C_SC}" />')
    e.append(f'<text x="{sx_nom}" y="{sc_y_top - 4}" font-size="8" fill="{C_TEXT}" text-anchor="middle" font-weight="bold">SC</text>')
    for x_ghost, tr_val in ((sx_u1, u_sc_tr[0]), (sx_u2, u_sc_tr[1])):
        if abs(tr_val) > 1e-6:
            e.append(f'<rect x="{x_ghost - sw/2}" y="{sc_y_top}" width="{sw}" height="{sh}" rx="3" fill="none" stroke="{C_SC}" stroke-width="1" stroke-dasharray="3 2" opacity="0.5" />')

    sc_span_mm = (u_sc_tr[1] - u_sc_tr[0]) * 1000
    e.append(f'<text x="{(sx_u1 + sx_u2)/2}" y="{sc_y_top - 14}" font-size="10" fill="{C_SC}" text-anchor="middle" font-weight="600">← translation ≤ {sc_span_mm:.0f} mm →</text>')

    e.append(f'<line x1="10" y1="{sec2_top + 150}" x2="720" y2="{sec2_top + 150}" stroke="#dee2e6" stroke-width="1" />')

    # ───── Section 3: Gripper offset ─────
    sec3_top = 370
    e.append(f'<text x="20" y="{sec3_top + 22}" font-size="12" font-weight="bold" fill="{C_GR}">✋ Gripper offset — 케이블 잡는 위치·자세 편차 (nominal 주변)</text>')

    # 스케일: AIC xy (0.002m) → 44px
    scale = 22000

    # xy
    xy_cx, xy_cy = 160, sec3_top + 115
    r_aic = AIC_GRIP_XY * scale
    r_user = u_gr_xy * scale
    e.append(f'<text x="{xy_cx}" y="{sec3_top + 50}" font-size="10" fill="{C_GR}" text-anchor="middle" font-weight="bold">xy 편차 (top-down)</text>')
    e.append(f'<circle cx="{xy_cx}" cy="{xy_cy}" r="{r_aic}" fill="none" stroke="{C_MUTED}" stroke-width="1" stroke-dasharray="3 2" />')
    if r_user > 0.5:
        e.append(f'<circle cx="{xy_cx}" cy="{xy_cy}" r="{r_user}" fill="{C_GR_L}" stroke="{C_GR}" stroke-width="1.5" opacity="0.85" />')
    e.append(f'<line x1="{xy_cx - 55}" y1="{xy_cy}" x2="{xy_cx + 55}" y2="{xy_cy}" stroke="{C_MUTED}" stroke-width="0.6" />')
    e.append(f'<line x1="{xy_cx}" y1="{xy_cy - 55}" x2="{xy_cx}" y2="{xy_cy + 55}" stroke="{C_MUTED}" stroke-width="0.6" />')
    e.append(f'<text x="{xy_cx + 52}" y="{xy_cy + 4}" font-size="8" fill="{C_MUTED}">x</text>')
    e.append(f'<text x="{xy_cx + 4}" y="{xy_cy - 50}" font-size="8" fill="{C_MUTED}">y</text>')
    e.append(f'<circle cx="{xy_cx}" cy="{xy_cy}" r="2.5" fill="{C_TEXT}" />')
    e.append(f'<text x="{xy_cx}" y="{sec3_top + 183}" font-size="10" fill="{C_GR}" text-anchor="middle" font-weight="600">반경 ±{u_gr_xy*1000:.1f} mm</text>')
    e.append(f'<text x="{xy_cx}" y="{sec3_top + 196}" font-size="8" fill="{C_MUTED}" text-anchor="middle">AIC ±{AIC_GRIP_XY*1000:.0f} mm</text>')

    # z
    z_cx, z_cy = 360, sec3_top + 115
    z_aic = AIC_GRIP_Z * scale
    z_user = u_gr_z * scale
    e.append(f'<text x="{z_cx}" y="{sec3_top + 50}" font-size="10" fill="{C_GR}" text-anchor="middle" font-weight="bold">z 편차 (side view)</text>')
    e.append(f'<line x1="{z_cx - 30}" y1="{z_cy - z_aic}" x2="{z_cx + 30}" y2="{z_cy - z_aic}" stroke="{C_MUTED}" stroke-width="1" stroke-dasharray="3 2" />')
    e.append(f'<line x1="{z_cx - 30}" y1="{z_cy + z_aic}" x2="{z_cx + 30}" y2="{z_cy + z_aic}" stroke="{C_MUTED}" stroke-width="1" stroke-dasharray="3 2" />')
    if z_user > 0.5:
        e.append(f'<rect x="{z_cx - 15}" y="{z_cy - z_user}" width="30" height="{2*z_user}" fill="{C_GR_L}" stroke="{C_GR}" stroke-width="1.5" opacity="0.85" />')
    e.append(f'<line x1="{z_cx - 22}" y1="{z_cy}" x2="{z_cx + 22}" y2="{z_cy}" stroke="{C_TEXT}" stroke-width="1" stroke-dasharray="2 2" />')
    e.append(f'<circle cx="{z_cx}" cy="{z_cy}" r="2.5" fill="{C_TEXT}" />')
    e.append(f'<text x="{z_cx + 35}" y="{z_cy - 30}" font-size="8" fill="{C_MUTED}">+z</text>')
    e.append(f'<text x="{z_cx + 35}" y="{z_cy + 34}" font-size="8" fill="{C_MUTED}">-z</text>')
    e.append(f'<text x="{z_cx}" y="{sec3_top + 183}" font-size="10" fill="{C_GR}" text-anchor="middle" font-weight="600">±{u_gr_z*1000:.1f} mm</text>')
    e.append(f'<text x="{z_cx}" y="{sec3_top + 196}" font-size="8" fill="{C_MUTED}" text-anchor="middle">AIC ±{AIC_GRIP_Z*1000:.0f} mm</text>')

    # rpy
    rpy_cx, rpy_cy = 560, sec3_top + 120
    rpy_deg = u_gr_rpy * 180 / math.pi
    rpy_deg_aic = AIC_GRIP_RPY * 180 / math.pi
    e.append(f'<text x="{rpy_cx}" y="{sec3_top + 50}" font-size="10" fill="{C_GR}" text-anchor="middle" font-weight="bold">rpy (회전 편차)</text>')

    # 시각적으로 보이려면 각도 scale up (0.04 rad = 2.3°는 너무 작아 보여서)
    vis_scale = 8  # 시각용만, 라벨은 실제값
    vis_aic = AIC_GRIP_RPY * vis_scale
    vis_user = u_gr_rpy * vis_scale
    r_arc_aic = 55
    ax1 = rpy_cx + r_arc_aic * math.sin(-vis_aic); ay1 = rpy_cy - r_arc_aic * math.cos(-vis_aic)
    ax2 = rpy_cx + r_arc_aic * math.sin(+vis_aic); ay2 = rpy_cy - r_arc_aic * math.cos(+vis_aic)
    e.append(f'<path d="M {ax1:.2f} {ay1:.2f} A {r_arc_aic} {r_arc_aic} 0 0 1 {ax2:.2f} {ay2:.2f}" fill="none" stroke="{C_MUTED}" stroke-width="1" stroke-dasharray="3 2" />')
    if u_gr_rpy > 1e-4:
        r_arc_u = 40
        ux1 = rpy_cx + r_arc_u * math.sin(-vis_user); uy1 = rpy_cy - r_arc_u * math.cos(-vis_user)
        ux2 = rpy_cx + r_arc_u * math.sin(+vis_user); uy2 = rpy_cy - r_arc_u * math.cos(+vis_user)
        e.append(f'<path d="M {ux1:.2f} {uy1:.2f} A {r_arc_u} {r_arc_u} 0 0 1 {ux2:.2f} {uy2:.2f}" fill="none" stroke="{C_GR}" stroke-width="3" />')
    e.append(f'<line x1="{rpy_cx}" y1="{rpy_cy}" x2="{rpy_cx}" y2="{rpy_cy - 62}" stroke="{C_TEXT}" stroke-width="1" stroke-dasharray="2 2" />')
    e.append(f'<circle cx="{rpy_cx}" cy="{rpy_cy}" r="3" fill="{C_GR}" />')
    e.append(f'<text x="{rpy_cx}" y="{rpy_cy + 5}" font-size="14" fill="{C_GR}" text-anchor="middle">↻</text>')
    e.append(f'<text x="{rpy_cx}" y="{sec3_top + 183}" font-size="10" fill="{C_GR}" text-anchor="middle" font-weight="600">±{rpy_deg:.2f}°</text>')
    e.append(f'<text x="{rpy_cx}" y="{sec3_top + 196}" font-size="8" fill="{C_MUTED}" text-anchor="middle">AIC ±{rpy_deg_aic:.2f}°</text>')
    e.append(f'<text x="{rpy_cx}" y="{sec3_top + 208}" font-size="7" fill="{C_MUTED}" text-anchor="middle" font-style="italic">(각도 시각은 {vis_scale}× 과장)</text>')

    # 범례
    e.append(f'<text x="20" y="{sec3_top + 230}" font-size="9" fill="{C_MUTED}" font-style="italic">점선 = AIC 공식 최대 허용 · 색칠 = 현재 선택 · 0 위치 = nominal (원점)</text>')

    total_w = 740
    total_h = sec3_top + 250

    return (
        f'<svg viewBox="0 0 {total_w} {total_h}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;max-width:780px;">'
        f'{"".join(e)}'
        f'</svg>'
    )


def render_sampling_strategy_svg(selected: str, n: int = 16) -> str:
    """uniform/lhs 샘플링 패턴을 2-panel 2D로 비교. 선택 전략만 강조.

    각 패널은 [0,1]^2에서 n점을 해당 전략으로 뽑아 그린다:
      - uniform: np.random.default_rng(42).random
      - lhs:     qmc.LatinHypercube(d=2, seed=42)

    연속 pose 축 2개를 투영한 그림으로 해석하면 직관적.
    """
    import numpy as np
    try:
        from scipy.stats import qmc
    except ImportError:
        return (
            '<svg viewBox="0 0 500 40" xmlns="http://www.w3.org/2000/svg">'
            '<text x="10" y="25" font-size="12" fill="#868e96">'
            'scipy 미설치 — 전략 비교 그림 생략</text></svg>'
        )

    rng = np.random.default_rng(42)
    pts_map = {
        "uniform": rng.random((n, 2)),
        "lhs": qmc.LatinHypercube(d=2, seed=42).random(n=n),
    }

    descriptions = {
        "uniform": "독립 균등 난수 — 간단, 군집/공백 발생 가능",
        "lhs": "층화 샘플링 — 각 축 N구간에 정확히 1점",
    }

    PANEL = 220
    PAD_X = 30
    PAD_TOP = 30
    W = PAD_X * 3 + PANEL * 2
    H = 300

    C_TEXT = "#212529"
    C_MUTED = "#868e96"
    C_BORDER = "#dee2e6"
    C_SEL = "#0d6efd"
    C_DOT = "#868e96"
    C_DOT_SEL = "#0d6efd"
    C_GRID = "#f1f3f5"

    elements: list[str] = []

    for i, strat in enumerate(("uniform", "lhs")):
        x0 = PAD_X + i * (PANEL + PAD_X)
        y0 = PAD_TOP
        is_sel = strat == selected
        border = C_SEL if is_sel else C_BORDER
        dot_color = C_DOT_SEL if is_sel else C_DOT
        bw = 2 if is_sel else 1

        label_color = C_SEL if is_sel else C_TEXT
        label_weight = "bold" if is_sel else "normal"
        elements.append(
            f'<text x="{x0 + PANEL/2}" y="{y0 - 10}" font-size="12" '
            f'font-weight="{label_weight}" fill="{label_color}" '
            f'text-anchor="middle">{strat}</text>'
        )
        elements.append(
            f'<rect x="{x0}" y="{y0}" width="{PANEL}" height="{PANEL}" '
            f'fill="white" stroke="{border}" stroke-width="{bw}" rx="4" />'
        )
        if strat == "lhs":
            step = PANEL / n
            for k in range(1, n):
                gx = x0 + step * k
                gy = y0 + step * k
                elements.append(
                    f'<line x1="{gx:.2f}" y1="{y0}" x2="{gx:.2f}" '
                    f'y2="{y0 + PANEL}" stroke="{C_GRID}" stroke-width="1" />'
                )
                elements.append(
                    f'<line x1="{x0}" y1="{gy:.2f}" x2="{x0 + PANEL}" '
                    f'y2="{gy:.2f}" stroke="{C_GRID}" stroke-width="1" />'
                )
        inset = 6
        for px, py in pts_map[strat]:
            cx = x0 + inset + float(px) * (PANEL - 2 * inset)
            cy = y0 + inset + float(py) * (PANEL - 2 * inset)
            elements.append(
                f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="3.5" '
                f'fill="{dot_color}" />'
            )
        elements.append(
            f'<text x="{x0 + PANEL/2}" y="{y0 + PANEL + 22}" font-size="10" '
            f'fill="{C_MUTED}" text-anchor="middle">{descriptions[strat]}</text>'
        )

    return (
        f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
        f'style="font-family: sans-serif;">'
        + "".join(elements) +
        '</svg>'
    )


def render_scene_svg(
    nic_range: tuple[int, int],
    sc_range: tuple[int, int],
    target_cycling: bool,
    ranges: dict | None = None,
    seed: int = 42,
    task_type: str = "sfp",
    sample_count: int = 3,
) -> str:
    """Scene 설정으로 실제 샘플 N개를 생성해서 mini task board 3개를 SVG로 렌더.

    각 샘플(=1 config 파일 = 1 trial)마다 활성 rail 조합이 **다르다**는 점을
    눈으로 바로 보여준다. target rail은 색상으로 강조.
    """
    from aic_collector.sampler import sample_scenes

    nic_min, nic_max = int(nic_range[0]), int(nic_range[1])
    sc_min, sc_max = int(sc_range[0]), int(sc_range[1])

    # 현재 UI 설정으로 실제 샘플 생성
    try:
        cfg = {
            "training": {
                "scene": {
                    "nic_count_range": [nic_min, nic_max],
                    "sc_count_range":  [sc_min, sc_max],
                    "target_cycling":  target_cycling,
                },
                "ranges": ranges or {},
            }
        }
        plans = sample_scenes(cfg, task_type, sample_count, seed)
    except Exception as e:
        return (
            f'<svg viewBox="0 0 560 80" xmlns="http://www.w3.org/2000/svg">'
            f'<text x="10" y="40" font-size="12" fill="#dc3545">샘플 생성 실패: {e}</text></svg>'
        )

    # 색상
    NIC_COLOR = "#0d6efd"
    NIC_INACTIVE = "#e9ecef"
    SC_COLOR = "#198754"
    SC_INACTIVE = "#e9ecef"
    TARGET_COLOR = "#dc3545"
    TEXT = "#495057"
    MUTED = "#adb5bd"

    # mini board 레이아웃
    board_w = 170
    board_gap = 14
    total_w = board_w * sample_count + board_gap * (sample_count - 1) + 40
    total_h = 370

    elements: list[str] = []

    # 전체 프레임 + 타이틀
    elements.append(
        f'<rect x="6" y="6" width="{total_w - 12}" height="{total_h - 12}" '
        f'rx="10" fill="#fafbfc" stroke="#6c757d" stroke-width="1.5" />'
    )
    elements.append(
        f'<text x="16" y="26" font-size="12" font-weight="bold" fill="#212529">'
        f'🎬 실제 샘플 예시 3개 — 각 trial(=config 파일)마다 rail 조합이 다름'
        f'</text>'
    )
    elements.append(
        f'<text x="16" y="42" font-size="10" fill="{MUTED}">'
        f'task={task_type.upper()} · seed={seed} · target cycling={"ON" if target_cycling else "OFF"}'
        f'</text>'
    )

    # 각 sample mini board
    for col, plan in enumerate(plans):
        bx = 20 + col * (board_w + board_gap)
        by = 56
        trial = plan.trials[0]

        # board 테두리
        elements.append(
            f'<rect x="{bx}" y="{by}" width="{board_w}" height="295" '
            f'rx="6" fill="#ffffff" stroke="#dee2e6" stroke-width="1.2" />'
        )

        # Sample 헤더
        elements.append(
            f'<text x="{bx + 10}" y="{by + 18}" font-size="11" font-weight="bold" fill="#212529">'
            f'Sample #{plan.sample_index}'
            f'</text>'
        )
        elements.append(
            f'<text x="{bx + 10}" y="{by + 32}" font-size="9" fill="{TARGET_COLOR}">'
            f'🎯 rail {trial.target_rail}, {trial.target_port_name}'
            f'</text>'
        )

        # Zone 1 — NIC (5 rails)
        zone1_y = by + 48
        elements.append(
            f'<text x="{bx + 10}" y="{zone1_y}" font-size="10" font-weight="bold" fill="{NIC_COLOR}">'
            f'Zone 1 — NIC'
            f'</text>'
        )
        for rail_idx in range(5):
            ry = zone1_y + 18 + rail_idx * 20
            is_active = rail_idx in trial.nic_rails
            is_target = (rail_idx == trial.target_rail and trial.task_type == "sfp")

            # rail 레이블
            elements.append(
                f'<text x="{bx + 18}" y="{ry + 4}" font-size="9" fill="{TEXT}" '
                f'font-family="monospace">rail{rail_idx}</text>'
            )

            # 2 SFP port 원
            for p in range(2):
                px = bx + 62 + p * 20
                if is_active:
                    fill = NIC_COLOR
                    stroke = NIC_COLOR
                    dash = ""
                else:
                    fill = NIC_INACTIVE
                    stroke = MUTED
                    dash = ' stroke-dasharray="2 1.5"'
                elements.append(
                    f'<circle cx="{px}" cy="{ry}" r="5" fill="{fill}" '
                    f'stroke="{stroke}" stroke-width="1"{dash} />'
                )

            # target 마크 또는 비활성 표시
            if is_target:
                elements.append(
                    f'<text x="{bx + 108}" y="{ry + 4}" font-size="11">🎯</text>'
                )
                elements.append(
                    f'<rect x="{bx + 54}" y="{ry - 10}" width="50" height="20" rx="3" '
                    f'fill="none" stroke="{TARGET_COLOR}" stroke-width="1.5" />'
                )
            elif is_active:
                elements.append(
                    f'<text x="{bx + 108}" y="{ry + 4}" font-size="8" fill="{NIC_COLOR}">활성</text>'
                )
            else:
                elements.append(
                    f'<text x="{bx + 108}" y="{ry + 4}" font-size="8" fill="{MUTED}">—</text>'
                )

        # Zone 2 — SC (2 rails)
        zone2_y = by + 170
        elements.append(
            f'<text x="{bx + 10}" y="{zone2_y}" font-size="10" font-weight="bold" fill="{SC_COLOR}">'
            f'Zone 2 — SC'
            f'</text>'
        )
        for rail_idx in (0, 1):
            ry = zone2_y + 18 + rail_idx * 20
            is_active = rail_idx in trial.sc_rails
            is_target = (rail_idx == trial.target_rail and trial.task_type == "sc")

            elements.append(
                f'<text x="{bx + 18}" y="{ry + 4}" font-size="9" fill="{TEXT}" '
                f'font-family="monospace">rail{rail_idx}</text>'
            )

            px = bx + 72
            if is_active:
                fill = SC_COLOR
                stroke = SC_COLOR
                dash = ""
            else:
                fill = SC_INACTIVE
                stroke = MUTED
                dash = ' stroke-dasharray="2 1.5"'
            elements.append(
                f'<circle cx="{px}" cy="{ry}" r="5" fill="{fill}" '
                f'stroke="{stroke}" stroke-width="1"{dash} />'
            )

            if is_target:
                elements.append(
                    f'<text x="{bx + 108}" y="{ry + 4}" font-size="11">🎯</text>'
                )
                elements.append(
                    f'<rect x="{bx + 60}" y="{ry - 10}" width="38" height="20" rx="3" '
                    f'fill="none" stroke="{TARGET_COLOR}" stroke-width="1.5" />'
                )
            elif is_active:
                elements.append(
                    f'<text x="{bx + 108}" y="{ry + 4}" font-size="8" fill="{SC_COLOR}">활성</text>'
                )
            else:
                elements.append(
                    f'<text x="{bx + 108}" y="{ry + 4}" font-size="8" fill="{MUTED}">—</text>'
                )

        # Summary at bottom
        summary_y = by + 258
        elements.append(
            f'<line x1="{bx + 8}" y1="{summary_y - 10}" x2="{bx + board_w - 8}" '
            f'y2="{summary_y - 10}" stroke="{MUTED}" stroke-width="0.5" />'
        )
        elements.append(
            f'<text x="{bx + 10}" y="{summary_y + 3}" font-size="9" fill="{TEXT}">'
            f'NIC {len(trial.nic_rails)}개 · SC {len(trial.sc_rails)}개'
            f'</text>'
        )
        elements.append(
            f'<text x="{bx + 10}" y="{summary_y + 18}" font-size="8" fill="{MUTED}" '
            f'font-family="monospace">rails: {trial.nic_rails} + {trial.sc_rails}</text>'
        )

    # 하단 설명
    desc_lines = [
        f"💡 같은 seed/설정이라도 sample_index마다 활성 rail 조합이 다릅니다. "
        f"target(🎯)은 반드시 활성 rail에 포함.",
    ]
    if task_type == "sfp":
        desc_lines.append(
            "※ SC task 샘플도 동일 원리 — 위는 SFP 예시이고, 실제 SC task에서도 "
            "SC rail이 1~2개로 랜덤 선택됩니다."
        )
    for i, line in enumerate(desc_lines):
        elements.append(
            f'<text x="16" y="{total_h - 24 + i * 12}" font-size="9.5" '
            f'fill="#495057">{line}</text>'
        )

    return (
        f'<svg viewBox="0 0 {total_w} {total_h}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;max-width:820px;">'
        f'{"".join(elements)}'
        f'</svg>'
    )


HISTORY_FILE = Path("/tmp/e2e_webapp_history.json")


def _load_run_history() -> list[dict]:
    """실행 이력 로드."""
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception:
        return []


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


def _has_nvidia_gpu() -> bool:
    """NVIDIA GPU 존재 여부 확인."""
    try:
        r = subprocess.run(["nvidia-smi"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _aic_eval_create_hint() -> str:
    """aic_eval 컨테이너 생성 안내 문구 반환."""
    nvidia_flag = " --nvidia" if _has_nvidia_gpu() else ""
    return (
        f"docker pull ghcr.io/intrinsic-dev/aic/aic_eval:latest && "
        f"distrobox create{nvidia_flag} -i ghcr.io/intrinsic-dev/aic/aic_eval:latest aic_eval"
    )


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
                                "fix": None if ok else _aic_eval_create_hint()})
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
                            "fix": f"uv pip install {pip_name}"})

    # scipy
    try:
        __import__("scipy")
        checks.append({"name": "scipy", "ok": True, "msg": "설치됨 (LHS 가능)", "fix": None})
    except ImportError:
        checks.append({"name": "scipy", "ok": False, "msg": "미설치 (LHS 사용 시 필요)",
                        "fix": "uv pip install scipy"})

    return checks


# ---------------------------------------------------------------------------
# 결과 로드
# ---------------------------------------------------------------------------


def load_run_validations(output_root: Path = OUTPUT_ROOT) -> list[dict]:
    """run 디렉토리의 validation.json을 스캔해 경고가 있는 run만 반환."""
    warnings_list = []
    if not output_root.exists():
        return warnings_list
    for run_dir in sorted(output_root.glob("run_*"), reverse=True)[:20]:
        v_path = run_dir / "validation.json"
        if not v_path.exists():
            continue
        try:
            v = json.loads(v_path.read_text())
            if v.get("warnings"):
                warnings_list.append({
                    "run": run_dir.name,
                    "passed": v.get("passed_count", 0),
                    "total": v.get("total_count", 0),
                    "warnings": v.get("warnings", []),
                })
        except Exception:
            continue
    return warnings_list


def load_results(output_root: Path = OUTPUT_ROOT) -> list[dict]:
    """output_root의 run들에서 tags.json을 읽어 rows 반환.

    두 가지 구조를 모두 지원:
    - legacy:   run_*/trial_*_score*/tags.json  (Sweep 또는 구 큐)
    - flat:     run_*/tags.json                  (신 큐 — 1 config = 1 trial)
    """
    rows = []
    if not output_root.exists():
        return rows

    def _row(tags: dict, run_name: str, run_time: str) -> dict:
        dur_raw = tags.get("trial_duration_sec")
        return {
            "time": run_time,
            "run": run_name,
            "trial": tags.get("trial", "?"),
            "score": round(tags.get("scoring", {}).get("total", 0), 1),
            "success": "✅" if tags.get("success") else "❌",
            "duration": round(dur_raw, 1) if dur_raw is not None else None,
            "policy": tags.get("policy", "?"),
            "조기종료": "⚡" if tags.get("early_terminated") else "",
        }

    for run_dir in sorted(output_root.glob("run_*")):
        # run 디렉토리명에서 시각 추출.
        #   legacy:    run_01_20260408_233709
        #   queue(구): run_01_20260418_234014_sfp_0000
        #   queue(신): run_20260419_120000_sfp_0000
        run_time = ""
        ts_match = re.search(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", run_dir.name)
        if ts_match:
            y, mo, d, h, mi, s = ts_match.groups()
            run_time = f"{y}-{mo}-{d} {h}:{mi}:{s}"

        # flat 구조 우선 검사
        flat_tags = run_dir / "tags.json"
        if flat_tags.exists():
            try:
                with open(flat_tags) as f:
                    rows.append(_row(json.load(f), run_dir.name, run_time))
            except Exception:
                pass
            continue

        # legacy: trial_*_score* 하위
        for trial_dir in sorted(run_dir.glob("trial_*_score*")):
            tags_path = trial_dir / "tags.json"
            if not tags_path.exists():
                continue
            try:
                with open(tags_path) as f:
                    rows.append(_row(json.load(f), run_dir.name, run_time))
            except Exception:
                continue
    return rows


# ---------------------------------------------------------------------------
# Config 생성
# ---------------------------------------------------------------------------


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

# Prefect 서버 자동 기동 (이미 떠 있으면 skip).
# Streamlit은 매 rerun마다 이 스크립트 전체를 재실행하지만, cache_resource로
# 세션당 단 한 번만 호출되게 한다.
@st.cache_resource
def _start_prefect_once() -> bool:
    return ensure_prefect_server(wait_sec=30)


_prefect_up = _start_prefect_once()
if not _prefect_up:
    st.warning(
        f"⚠️ Prefect 서버 기동에 실패했어요. 수동으로 `uv run prefect server start "
        f"--host 0.0.0.0 --port {PREFECT_PORT}` 실행 후 새로고침하세요. "
        f"로그: `{PREFECT_LOG_FILE}`"
    )

policies = discover_policies()

tab_env, tab_manage, tab_execute, tab_results = st.tabs(
    ["🔍 환경 점검", "📋 작업 관리", "🏃 작업 실행", "📊 결과"]
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

# --- 작업 관리 탭 (Phase 2a — Producer) ---
with tab_manage:
    from aic_collector.job_queue import (
        QueueCounts,
        QueueState,
        TASK_TYPES,
        all_counts,
        ensure_queue_dirs,
        list_configs,
        migrate_legacy_to_pending,
        write_plans,
    )
    from aic_collector.sampler import sample_scenes

    st.subheader("📋 작업 관리")
    st.caption(
        "Config 파일을 생성해 **pending/ 큐**에 적재합니다. "
        "실행은 🏃 작업 실행 탭에서 진행합니다."
    )

    # 큐 루트 — 작업 관리·작업 실행 탭이 shared_queue_root 세션 키로 양방향 싱크
    _DEFAULT_QUEUE_ROOT = str(PROJECT_DIR / "configs/train")
    if "shared_queue_root" not in st.session_state:
        st.session_state["shared_queue_root"] = _DEFAULT_QUEUE_ROOT

    def _sync_queue_root_from(source_key: str) -> None:
        st.session_state["shared_queue_root"] = st.session_state[source_key]

    # pre-render seed — 상대 탭이 업데이트한 값이 있으면 위젯 상태에 반영
    if st.session_state.get("mgr_queue_root") != st.session_state["shared_queue_root"]:
        st.session_state["mgr_queue_root"] = st.session_state["shared_queue_root"]

    mgr_queue_root_str = st.text_input(
        "큐 루트",
        key="mgr_queue_root",
        help=(
            "`<루트>/sfp/pending/` 형태로 task·state별 디렉토리를 둡니다. "
            "state는 pending/running/done/failed. 작업 실행 탭과 값이 공유됩니다."
        ),
        on_change=_sync_queue_root_from, args=("mgr_queue_root",),
    )
    mgr_queue_root = Path(mgr_queue_root_str)

    st.divider()

    # 큐 상태 (task_type별 가로 stacked bar 1줄)
    hdr_col, btn_col = st.columns([5, 1])
    with hdr_col:
        st.markdown("### 📊 큐 상태")
    with btn_col:
        if st.button("🔄 새로고침", key="mgr_refresh_counts"):
            st.rerun()

    counts = all_counts(mgr_queue_root) if mgr_queue_root.exists() else {
        t: None for t in TASK_TYPES
    }
    render_queue_status_block(counts, TASK_TYPES)

    # Legacy 마이그레이션
    total_legacy = sum(
        c.legacy for c in counts.values() if c is not None
    )
    if total_legacy > 0:
        st.warning(
            f"⚠️ Legacy 파일 **{total_legacy}개**가 상태 디렉토리 밖에 있습니다. "
            f"pending/으로 이동하면 큐 관리가 일관됩니다."
        )
        if st.button(
            f"🔀 Legacy {total_legacy}개 → pending/으로 이동",
            key="mgr_migrate_legacy",
        ):
            moved = migrate_legacy_to_pending(mgr_queue_root)
            total_moved = sum(moved.values())
            if total_moved > 0:
                detail = ", ".join(f"{k}={v}" for k, v in moved.items() if v > 0)
                st.success(f"이동 완료: {detail}")
            else:
                st.info("이동된 파일 없음 (pending에 이미 같은 이름이 있는 경우 건너뜁니다)")
            st.rerun()

    st.divider()

    # 생성 설정
    st.markdown("### ➕ 큐에 추가")

    # 공식 문서 URL (help 툴팁에 근거 링크로 사용)
    AIC_TASK_BOARD_URL = (
        "https://github.com/intrinsic-dev/aic/blob/main/docs/task_board_description.md"
    )
    AIC_QUAL_PHASE_URL = (
        "https://github.com/intrinsic-dev/aic/blob/main/docs/qualification_phase.md"
    )

    # AIC 공식 기본값 (task_board_description.md / qualification_phase.md 기준)
    MGR_DEFAULT_NIC_COUNT_RANGE = (1, 5)
    MGR_DEFAULT_SC_COUNT_RANGE = (1, 2)
    MGR_DEFAULT_TARGET_CYCLING = True
    MGR_DEFAULT_RANGES = {
        "nic_translation": (-0.0215, 0.0234),
        "nic_yaw":         (-0.1745, 0.1745),
        "sc_translation":  (-0.06, 0.055),
        "gripper_xy":      0.002,
        "gripper_z":       0.002,
        "gripper_rpy":     0.04,
    }
    # AIC 공식 최대 범위 (초과 시 경고용 — 현재 default와 동일하지만 의도 명확화)
    MGR_AIC_BOUNDS = dict(MGR_DEFAULT_RANGES)

    # 기본 파라미터
    col_sfp, col_sc = st.columns(2)
    with col_sfp:
        mgr_sfp_count = st.number_input(
            "SFP configs",
            min_value=0, max_value=10000, value=20, step=10,
            key="mgr_sfp_count",
            help=(
                "target cycling ON 시 SFP 10종 target (5 rail × 2 port)을 "
                "균등 순환. 10의 배수 권장.  \n"
                f"📖 [task_board_description.md Zone 1]({AIC_TASK_BOARD_URL}#zone-1-network-interface-cards-nic)"
            ),
        )
    with col_sc:
        mgr_sc_count = st.number_input(
            "SC configs",
            min_value=0, max_value=10000, value=10, step=2,
            key="mgr_sc_count",
            help=(
                "target cycling ON 시 SC 2종 target (rail 0·1)을 "
                "균등 순환. 2의 배수 권장.  \n"
                f"📖 [qualification_phase.md Trial 3]({AIC_QUAL_PHASE_URL}#trial-3-generalization-sc)"
            ),
        )

    # 🎬 Scene expander — 엔티티 개수 및 target 전략
    with st.expander("🎬 Scene — 엔티티 개수 및 target 전략", expanded=False):
        # NIC — 체크박스 + 단일 슬라이더
        col_nic_fix, col_nic_slider = st.columns([1, 3])
        with col_nic_fix:
            st.markdown("###### NIC")
            mgr_nic_fixed = st.checkbox(
                "고정 개수", value=False, key="mgr_nic_fixed",
                help="on: 매 샘플 정확히 N개 / off: 1~N개 랜덤",
            )
        with col_nic_slider:
            mgr_nic_max = st.slider(
                "NIC 개수 (고정)" if mgr_nic_fixed else "NIC 최대 개수 (1~N 랜덤)",
                min_value=1, max_value=5, value=5, step=1,
                key="mgr_nic_max",
                help=(
                    "scene에 배치할 NIC 카드 개수. rail 0~4 중에서 선택.  \n"
                    f"📖 [task_board_description.md Zone 1]({AIC_TASK_BOARD_URL}#zone-1-network-interface-cards-nic)"
                ),
            )
        mgr_nic_count_range = (mgr_nic_max if mgr_nic_fixed else 1, mgr_nic_max)

        # SC — 동일 패턴
        col_sc_fix, col_sc_slider = st.columns([1, 3])
        with col_sc_fix:
            st.markdown("###### SC")
            mgr_sc_fixed = st.checkbox(
                "고정 개수", value=False, key="mgr_sc_fixed",
                help="on: 매 샘플 정확히 N개 / off: 1~N개 랜덤",
            )
        with col_sc_slider:
            mgr_sc_max = st.slider(
                "SC 개수 (고정)" if mgr_sc_fixed else "SC 최대 개수 (1~N 랜덤)",
                min_value=1, max_value=2, value=2, step=1,
                key="mgr_sc_max",
                help=(
                    "scene에 배치할 SC 포트 개수. rail 0·1 중에서 선택.  \n"
                    f"📖 [qualification_phase.md Trial 3]({AIC_QUAL_PHASE_URL}#trial-3-generalization-sc)"
                ),
            )
        mgr_sc_count_range = (mgr_sc_max if mgr_sc_fixed else 1, mgr_sc_max)

        mgr_target_cycling = st.checkbox(
            "Target cycling (결정적 순환으로 균등 분배)",
            value=MGR_DEFAULT_TARGET_CYCLING,
            key="mgr_target_cycling",
            help=(
                "on: SFP 10종·SC 2종 target을 sample_index 기반으로 정확히 균등 분배. "
                "off: 매 샘플 uniform 랜덤 추첨 (개수 적을 때 불균등).\n\n"
                f"📖 SFP 10종 구조: [task_board_description.md Zone 1]({AIC_TASK_BOARD_URL}#zone-1-network-interface-cards-nic)  \n"
                f"📖 SC 2종 구조: [qualification_phase.md Trial 3]({AIC_QUAL_PHASE_URL}#trial-3-generalization-sc)"
            ),
        )

        # 시각 다이어그램 — 현재 Scene 설정으로 샘플 3개 생성해 실제 조합을 보여줌
        # ranges는 pose 값이라 다이어그램에 무관하므로 기본값으로 생성.
        # st.markdown(unsafe_allow_html)은 SVG를 살균 → components.v1.html 사용.
        _scene_svg = render_scene_svg(
            nic_range=tuple(mgr_nic_count_range),
            sc_range=tuple(mgr_sc_count_range),
            target_cycling=bool(mgr_target_cycling),
            ranges=None,  # pose 값은 시각에 영향 없음 → 기본값
            seed=int(st.session_state.get("mgr_seed", 42)),
            task_type="sfp",
            sample_count=3,
        )
        st.components.v1.html(
            f'<div style="display:flex;justify-content:center;padding:4px;">'
            f'{_scene_svg}</div>',
            height=560,
            scrolling=True,
        )

    # 📏 Parameters expander — 랜덤화 범위 + 샘플링 전략
    with st.expander("📏 Parameters — 랜덤화 범위", expanded=False):
        st.caption(
            "슬라이더의 min/max는 **AIC 공식 최대 허용 범위**입니다 — 초과 불가. "
            "범위를 좁히면 더 제한적인 학습 데이터가 생성됩니다. "
            "출처: "
            "[task_board_description.md]"
            "(https://github.com/intrinsic-dev/aic/blob/main/docs/task_board_description.md), "
            "[qualification_phase.md]"
            "(https://github.com/intrinsic-dev/aic/blob/main/docs/qualification_phase.md)."
        )

        strategy_opts = ["uniform", "lhs"]
        mgr_param_strategy = st.selectbox(
            "샘플링 전략",
            strategy_opts,
            index=0,
            key="mgr_param_strategy",
            help=(
                "pose 연속값(NIC/SC translation·yaw, gripper offset) 분포 전략. "
                "target·entity 개수는 적용 대상 아님.\n\n"
                "**uniform** — 각 샘플 독립 균등 난수 (기본).\n\n"
                "**lhs** — Latin Hypercube. 공간 채움 우수, 샘플 수 적을 때 유리. "
                "단 append 배치마다 독립 재추첨."
            ),
        )

        _strategy_svg = render_sampling_strategy_svg(str(mgr_param_strategy))
        st.components.v1.html(
            f'<div style="display:flex;justify-content:center;padding:4px;">'
            f'{_strategy_svg}</div>',
            height=420,
            scrolling=False,
        )

        # (label, key, (aic_min, aic_max), step, format, source_url)
        # NIC translation/yaw·SC translation → task_board_description.md Zone 1/2
        range_fields = [
            ("NIC translation (m)", "nic_translation", (-0.0215, 0.0234), 0.0001, "%.4f", AIC_TASK_BOARD_URL),
            ("NIC yaw (rad)",       "nic_yaw",         (-0.1745, 0.1745), 0.0010, "%.4f", AIC_TASK_BOARD_URL),
            ("SC translation (m)",  "sc_translation",  (-0.06,   0.055),  0.0010, "%.4f", AIC_TASK_BOARD_URL),
        ]
        # Gripper ± 편차 → qualification_phase.md §1 "~2mm, ~0.04 rad"
        # (label, key, aic_max, step, format, source_url)
        spread_fields = [
            ("Gripper x / y (±m)",   "gripper_xy",  0.002, 0.0001, "%.4f", AIC_QUAL_PHASE_URL),
            ("Gripper z (±m)",       "gripper_z",   0.002, 0.0001, "%.4f", AIC_QUAL_PHASE_URL),
            ("Gripper rpy (±rad)",   "gripper_rpy", 0.04,  0.0010, "%.4f", AIC_QUAL_PHASE_URL),
        ]

        user_ranges: dict[str, Any] = {}

        st.markdown("**Range (min ~ max)** — 핸들 두 개로 범위 지정")
        for label, key, (lo, hi), step, fmt, url in range_fields:
            v = st.slider(
                label,
                min_value=float(lo), max_value=float(hi),
                value=(float(lo), float(hi)),
                step=float(step),
                format=fmt,
                key=f"mgr_range_{key}_range",
                help=f"AIC 공식 허용: [{lo}, {hi}]  \n📖 [task_board_description.md]({url})",
            )
            user_ranges[key] = [float(v[0]), float(v[1])]

        st.markdown("**Spread (nominal ± value)** — 그리퍼 offset 편차")
        for label, key, aic_max, step, fmt, url in spread_fields:
            v = st.slider(
                label,
                min_value=0.0, max_value=float(aic_max),
                value=float(aic_max),
                step=float(step),
                format=fmt,
                key=f"mgr_range_{key}_spread",
                help=f"AIC 공식 허용: ± {aic_max}  \n📖 [qualification_phase.md §1]({url})",
            )
            user_ranges[key] = float(v)

        # 시각 미리보기 — 선택 범위 vs AIC 최대 허용
        _params_svg = render_parameters_svg(user_ranges)
        st.components.v1.html(
            f'<div style="display:flex;justify-content:center;padding:4px;">'
            f'{_params_svg}</div>',
            height=680,
            scrolling=True,
        )

        # 변경 여부 판정 (모든 값이 AIC 공식 기본값=최대 범위와 같은지)
        def _tol_eq(a, b, tol: float = 1e-9) -> bool:
            if isinstance(a, (list, tuple)):
                return all(abs(x - y) <= tol for x, y in zip(a, b))
            return abs(a - b) <= tol

        ranges_is_custom = not all(
            _tol_eq(user_ranges[k], MGR_DEFAULT_RANGES[k])
            for k in MGR_DEFAULT_RANGES
        )

        col_badge, col_reset = st.columns([3, 1])
        with col_badge:
            if ranges_is_custom:
                st.info("⚙️ 사용자 정의 범위 — 기본값보다 좁게 설정됨")
            else:
                st.success("✅ AIC 공식 기본값 (최대 허용 범위)")
        with col_reset:
            if st.button("🔄 범위 리셋", key="mgr_reset_ranges"):
                for k in list(st.session_state.keys()):
                    if k.startswith("mgr_range_"):
                        del st.session_state[k]
                st.rerun()

    # ⚙️ 고급 — 재현용 seed
    with st.expander("⚙️ 고급", expanded=False):
        mgr_seed = st.number_input(
            "Seed",
            min_value=0, value=42,
            key="mgr_seed",
            help="재현용 base seed. 같은 seed + 같은 설정 → 같은 config 생성.",
        )

    # Scene 사용자 정의 여부 (Scene 섹션 하단에 뱃지)
    scene_is_custom = (
        tuple(mgr_nic_count_range) != MGR_DEFAULT_NIC_COUNT_RANGE
        or tuple(mgr_sc_count_range) != MGR_DEFAULT_SC_COUNT_RANGE
        or mgr_target_cycling != MGR_DEFAULT_TARGET_CYCLING
    )
    if scene_is_custom:
        if mgr_target_cycling:
            st.caption("🎬 Scene: 사용자 정의 설정")
        else:
            st.caption(
                "🎬 Scene: 사용자 정의 설정 · ⚠ target cycling off — "
                "개수 적을 때 target 분포 불균등"
            )

    # 생성될 번호 미리보기 — 항상 기존 큐 번호에 이어서 생성 (덮어쓰기 방지)
    start_sfp = 0
    start_sc = 0
    if mgr_queue_root.exists():
        from aic_collector.job_queue import next_sample_index as _next_idx
        start_sfp = _next_idx(mgr_queue_root, "sfp")
        start_sc = _next_idx(mgr_queue_root, "sc")

        preview_parts = []
        if mgr_sfp_count > 0:
            preview_parts.append(
                f"SFP: config_sfp_{start_sfp:04d} ~ {start_sfp + mgr_sfp_count - 1:04d}"
            )
        if mgr_sc_count > 0:
            preview_parts.append(
                f"SC: config_sc_{start_sc:04d} ~ {start_sc + mgr_sc_count - 1:04d}"
            )
        if preview_parts:
            st.info("생성될 파일: " + " · ".join(preview_parts))
        else:
            st.info("생성할 count가 0입니다.")

    # 생성 버튼 — slider가 min≤max를 구조적으로 보장하므로 추가 검증 불필요
    total_new = mgr_sfp_count + mgr_sc_count
    btn_disabled = total_new == 0
    if st.button(
        f"📁 큐에 추가 ({total_new}개 생성)",
        type="primary",
        disabled=btn_disabled,
        key="mgr_generate",
    ):
        template = PROJECT_DIR / "configs/community_random_config.yaml"
        if not template.exists():
            st.error(f"템플릿 없음: {template}")
        else:
            # UI 값으로 sampler용 cfg 구성
            training_cfg = {
                "scene": {
                    "nic_count_range": [int(mgr_nic_count_range[0]), int(mgr_nic_count_range[1])],
                    "sc_count_range":  [int(mgr_sc_count_range[0]), int(mgr_sc_count_range[1])],
                    "target_cycling":  bool(mgr_target_cycling),
                },
                "ranges": dict(user_ranges),
                "param_strategy": str(mgr_param_strategy),
            }
            sampler_cfg = {"training": training_cfg}

            ensure_queue_dirs(mgr_queue_root)
            try:
                written_all: list[Path] = []
                if mgr_sfp_count > 0:
                    plans = sample_scenes(
                        sampler_cfg, "sfp", int(mgr_sfp_count), int(mgr_seed),
                        start_index=int(start_sfp),
                    )
                    written_all += write_plans(plans, mgr_queue_root, template)
                if mgr_sc_count > 0:
                    plans = sample_scenes(
                        sampler_cfg, "sc", int(mgr_sc_count), int(mgr_seed),
                        start_index=int(start_sc),
                    )
                    written_all += write_plans(plans, mgr_queue_root, template)
                st.success(
                    f"✅ {len(written_all)}개 config를 pending/에 추가했습니다."
                )
                with st.expander("📝 생성된 파일 목록"):
                    for p in written_all:
                        st.code(str(p.relative_to(PROJECT_DIR)), language=None)
                st.rerun()
            except Exception as e:
                st.error(f"생성 실패: {type(e).__name__}: {e}")

    # 큐 목록 — 테이블 + 선택 삭제 + Target 분포
    st.divider()
    st.markdown("### 📁 큐 목록")
    if not mgr_queue_root.exists():
        st.caption("큐 루트가 아직 존재하지 않습니다.")
    else:
        import pandas as pd
        from datetime import datetime

        rows: list[dict[str, Any]] = []
        for tt in TASK_TYPES:
            for state in QueueState:
                for f in list_configs(mgr_queue_root, tt, state):
                    stv = f.stat()
                    rows.append({
                        "파일명": f.name,
                        "task": tt,
                        "state": state.value,
                        "size": stv.st_size,
                        "수정일시": datetime.fromtimestamp(stv.st_mtime),
                        "_path": str(f),
                        "_mtime": stv.st_mtime,
                    })

        if not rows:
            st.caption("큐가 비어있습니다.")
        else:
            df_all = pd.DataFrame(rows)

            f_col1, f_col2 = st.columns(2)
            with f_col1:
                filter_tt = st.multiselect(
                    "task 필터",
                    list(TASK_TYPES),
                    default=list(TASK_TYPES),
                    key="mgr_list_filter_tt",
                    help="아래 테이블에 표시할 task 종류 (실행 대상 필터가 아님).",
                )
            with f_col2:
                filter_state = st.multiselect(
                    "state 필터",
                    [s.value for s in QueueState],
                    default=[s.value for s in QueueState],
                    key="mgr_list_filter_state",
                    help="아래 테이블에 표시할 state. pending/failed만 삭제 가능.",
                )

            df = df_all[
                df_all["task"].isin(filter_tt)
                & df_all["state"].isin(filter_state)
            ].reset_index(drop=True)
            df.insert(0, "#", range(1, len(df) + 1))

            if df.empty:
                st.caption("필터 조건에 맞는 파일이 없습니다.")
            else:
                event = st.dataframe(
                    df[["#", "파일명", "task", "state", "size", "수정일시"]],
                    selection_mode="multi-row",
                    on_select="rerun",
                    hide_index=True,
                    width="stretch",
                    key="mgr_list_table",
                    column_config={
                        "#": st.column_config.NumberColumn(width="small"),
                        "수정일시": st.column_config.DatetimeColumn(
                            format="YYYY-MM-DD HH:mm",
                        ),
                        "size": st.column_config.NumberColumn(format="%d B"),
                    },
                )
                selected_rows: list[int] = list(event.selection.rows)

                if selected_rows:
                    sel_df = df.iloc[selected_rows]
                    deletable = sel_df[sel_df["state"].isin(["pending", "failed"])]
                    blocked = sel_df[~sel_df["state"].isin(["pending", "failed"])]

                    info_parts = [f"선택 {len(sel_df)}개"]
                    if len(blocked):
                        info_parts.append(
                            f"⚠ running/done {len(blocked)}개는 안전을 위해 삭제 불가 (제외)"
                        )
                    st.caption(" · ".join(info_parts))

                    if len(deletable):
                        with st.popover(f"🗑️ {len(deletable)}개 삭제"):
                            st.warning(
                                "pending/failed 파일을 삭제합니다. 되돌릴 수 없습니다."
                            )
                            with st.expander("삭제 대상 목록", expanded=False):
                                for name in deletable["파일명"].tolist()[:100]:
                                    st.code(name, language=None)
                                if len(deletable) > 100:
                                    st.caption(f"… 외 {len(deletable) - 100}개")
                            if st.button(
                                "삭제 실행",
                                type="primary",
                                key="mgr_list_delete_confirm",
                            ):
                                deleted = 0
                                for p in deletable["_path"]:
                                    try:
                                        Path(p).unlink()
                                        deleted += 1
                                    except FileNotFoundError:
                                        pass
                                st.success(f"{deleted}개 삭제됨")
                                st.rerun()

            # Target 분포 (pending 한정) — 파일 파싱 비용 있어 expander로
            with st.expander("🎯 Target 분포 (pending 기준)", expanded=False):
                pending_df = df_all[df_all["state"] == "pending"]
                if pending_df.empty:
                    st.caption("pending 파일이 없습니다.")
                else:
                    @st.cache_data(show_spinner=False)
                    def _parse_target(
                        path_str: str, mtime: float
                    ) -> tuple[str, str] | None:
                        try:
                            with open(path_str) as fh:
                                cfg = yaml.safe_load(fh)
                            for _, v in (cfg.get("trials", {}) or {}).items():
                                t1 = (v or {}).get("tasks", {}).get("task_1", {}) or {}
                                tm = t1.get("target_module_name", "")
                                pn = t1.get("port_name", "")
                                if tm and pn:
                                    return tm, pn
                            return None
                        except Exception:
                            return None

                    tgt_rows: list[dict[str, Any]] = []
                    for _, r in pending_df.iterrows():
                        tgt = _parse_target(r["_path"], r["_mtime"])
                        if tgt is None:
                            continue
                        tm, pn = tgt
                        tgt_rows.append({
                            "task": r["task"],
                            "target": f"{tm} / {pn}",
                        })

                    if not tgt_rows:
                        st.caption("target 정보를 읽을 수 있는 config가 없습니다.")
                    else:
                        tgt_df = pd.DataFrame(tgt_rows)
                        for tt in TASK_TYPES:
                            sub = tgt_df[tgt_df["task"] == tt]
                            if sub.empty:
                                continue
                            counts_sr = (
                                sub["target"].value_counts().sort_index()
                            )
                            st.markdown(f"**{tt.upper()}** · 총 {len(sub)}개")
                            st.bar_chart(counts_sr, horizontal=True)

# --- 작업 실행 탭 (Phase 2b — Consumer) ---
with tab_execute:
    from aic_collector.job_queue import (
        QueueState as _QS,
        TASK_TYPES as _TT,
        all_counts as _all_counts,
        recover_running_to_pending as _recover,
    )

    def _worker_status() -> dict | None:
        """워커 상태: 실행 중이면 dict, 아니면 None."""
        if not WORKER_PID_FILE.exists():
            return None
        try:
            pid = int(WORKER_PID_FILE.read_text().strip())
        except Exception:
            return None
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        state = {}
        if WORKER_STATE_FILE.exists():
            try:
                state = json.loads(WORKER_STATE_FILE.read_text())
            except Exception:
                state = {}
        state["pid"] = pid
        state["running"] = True
        return state

    def _worker_start(
        root: str,
        task: str,
        limit: int | None,
        policy: str,
        act_model_path: str | None,
        ground_truth: bool,
        use_compressed: bool,
        collect_episode: bool,
        output_root: str,
        timeout: int | None,
        recover_first: bool,
        policy_sfp: str | None = None,
        policy_sc: str | None = None,
    ) -> None:
        """aic-collector-worker를 백그라운드 subprocess로 기동."""
        if _worker_status():
            raise RuntimeError("이미 워커가 실행 중입니다. 중지 후 다시 시작하세요.")

        WORKER_LOG_FILE.write_text("")
        cmd = [
            "uv", "run", "aic-collector-worker",
            "--root", root,
            "--task", task,
            "--policy", policy,
            "--ground-truth", str(ground_truth).lower(),
            "--use-compressed", str(use_compressed).lower(),
            "--collect-episode", str(collect_episode).lower(),
            "--output-root", output_root,
            "--log", str(WORKER_LOG_FILE),
        ]
        if limit is not None and limit > 0:
            cmd += ["--limit", str(limit)]
        if timeout is not None and timeout > 0:
            cmd += ["--timeout", str(timeout)]
        if act_model_path:
            cmd += ["--act-model-path", act_model_path]
        if policy_sfp:
            cmd += ["--policy-sfp", policy_sfp]
        if policy_sc:
            cmd += ["--policy-sc", policy_sc]
        if recover_first:
            cmd += ["--recover"]

        # 워커가 webapp이 띄운 영구 Prefect 서버에 연결되도록 env 주입.
        # 이게 없으면 매 flow run마다 ephemeral 임시 서버가 띄워져 UI에 안 보임.
        worker_env = os.environ.copy()
        worker_env["PREFECT_API_URL"] = f"{PREFECT_SERVER_URL}/api"

        proc = subprocess.Popen(
            cmd,
            stdout=open(WORKER_LOG_FILE, "a"),
            stderr=subprocess.STDOUT,
            cwd=str(PROJECT_DIR),
            start_new_session=True,
            env=worker_env,
        )
        WORKER_PID_FILE.write_text(str(proc.pid))

    def _worker_stop() -> bool:
        ws = _worker_status()
        if not ws:
            return False
        pid = ws["pid"]
        try:
            import signal as _signal
            os.killpg(os.getpgid(pid), _signal.SIGTERM)
            time.sleep(1)
            try:
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except OSError:
                pass
            return True
        except OSError:
            return False

    st.subheader("🏃 작업 실행")
    st.caption(
        "pending/ 큐를 소비하는 Consumer 워커를 제어합니다. "
        "정상 종료는 done/으로, 실패는 failed/로 이동합니다. "
        "실시간 모니터링은 **Prefect 대시보드**를 참고하세요."
    )

    # 큐 루트 — 작업 관리 탭과 shared_queue_root로 양방향 싱크
    if "shared_queue_root" not in st.session_state:
        st.session_state["shared_queue_root"] = str(PROJECT_DIR / "configs/train")
    if st.session_state.get("exec_queue_root") != st.session_state["shared_queue_root"]:
        st.session_state["exec_queue_root"] = st.session_state["shared_queue_root"]

    def _exec_queue_root_changed() -> None:
        st.session_state["shared_queue_root"] = st.session_state["exec_queue_root"]

    exec_queue_root_str = st.text_input(
        "큐 루트",
        key="exec_queue_root",
        help="작업 관리 탭과 값이 공유됩니다.",
        on_change=_exec_queue_root_changed,
    )
    exec_queue_root = Path(exec_queue_root_str)

    # 큐 현황 (작업 관리 탭과 동일한 가로 스택바)
    st.markdown("### 📊 큐 현황")
    if exec_queue_root.exists():
        ecounts = _all_counts(exec_queue_root)
        render_queue_status_block(ecounts, _TT)
    else:
        st.caption("큐 루트가 존재하지 않습니다.")

    st.divider()

    def _render_running_body() -> None:
        """실행 중 상태 렌더 — fragment에서 3초마다 재호출."""
        from datetime import datetime as _dt
        current_w = _worker_status()
        if current_w is None:
            # 워커가 방금 종료 → 전체 페이지 리런으로 idle 뷰 전환
            st.rerun(scope="app")
            return

        st.success(f"● 실행 중 (PID: {current_w['pid']})")
        processed = int(current_w.get("processed", 0))
        done_c = int(current_w.get("done", 0))
        fail_c = int(current_w.get("failed", 0))
        total_at_start = int(current_w.get("total_at_start", 0) or 0)

        if total_at_start > 0:
            ratio = min(1.0, processed / total_at_start)
            st.progress(
                ratio,
                text=f"처리 {processed} / {total_at_start} "
                     f"(done {done_c}, failed {fail_c})",
            )
            try:
                started_iso = current_w.get("started_at")
                if started_iso and processed > 0:
                    elapsed = (
                        _dt.now() - _dt.fromisoformat(started_iso)
                    ).total_seconds()
                    per_item = elapsed / processed
                    remaining = max(0, total_at_start - processed)
                    eta_sec = int(per_item * remaining)
                    eta_h, _r = divmod(eta_sec, 3600)
                    eta_m, eta_s = divmod(_r, 60)
                    eta_str = (
                        f"{eta_h}h {eta_m}m" if eta_h else
                        f"{eta_m}m {eta_s}s" if eta_m else f"{eta_s}s"
                    )
                    st.caption(
                        f"⏱ 평균 {per_item:.1f}s/config · 남은 "
                        f"{remaining}개 · ETA ~{eta_str}"
                    )
            except Exception:
                pass
        else:
            st.write(
                f"처리 {processed}개 (done {done_c}, failed {fail_c})"
            )

        cur = current_w.get("current")
        if cur:
            cur_started = current_w.get("current_started_at")
            dur_str = ""
            if cur_started:
                try:
                    dur_sec = int(
                        (_dt.now() - _dt.fromisoformat(cur_started)).total_seconds()
                    )
                    dur_str = f" · {dur_sec}s 경과"
                except Exception:
                    pass
            st.write(f"🔹 현재 실행: `{cur}`{dur_str}")

        recent = current_w.get("recent") or []
        if recent:
            with st.expander(f"📋 최근 처리 {len(recent)}개", expanded=True):
                for r in recent:
                    icon = "✅" if r.get("result") == "done" else "❌"
                    st.write(
                        f"{icon} `{r.get('name', '?')}` · "
                        f"{r.get('duration_sec', 0)}s"
                    )

        col_stop, col_refresh = st.columns([1, 1])
        with col_stop:
            if st.button("⏹ 워커 정지", key="exec_stop"):
                if _worker_stop():
                    WORKER_PID_FILE.unlink(missing_ok=True)
                    st.success("워커 중지됨")
                    st.rerun(scope="app")
                else:
                    st.warning("워커 프로세스를 찾지 못했습니다.")
        with col_refresh:
            if st.button("🔄 상태 새로고침", key="exec_refresh_running"):
                st.rerun(scope="app")

    # 워커 상태 — 실행 중이면 3초마다 자동 갱신되는 fragment
    st.markdown("### ⚙️ 워커 상태")
    w = _worker_status()
    if w:
        st.caption("⟳ 실행 중 — 3초마다 자동 갱신")

        @st.fragment(run_every=3)
        def _live_running_status() -> None:
            _render_running_body()

        _live_running_status()
    else:
        # 마지막 종료 상태
        if WORKER_STATE_FILE.exists():
            try:
                last = json.loads(WORKER_STATE_FILE.read_text())
                if last.get("status") in ("completed", "interrupted"):
                    st.info(
                        f"마지막 실행: {last.get('status')} · "
                        f"processed {last.get('processed', 0)} "
                        f"(done {last.get('done', 0)}, failed {last.get('failed', 0)}, "
                        f"{last.get('elapsed_sec', 0)}s)"
                    )
            except Exception:
                pass

        # 실행 설정
        st.markdown("### 🚀 실행 설정")
        col_task, col_limit = st.columns([1, 1])
        # ── 기본 (자주 조절) ──
        with col_task:
            exec_task = st.selectbox(
                "task 필터", ["all", "sfp", "sc"], index=0, key="exec_task",
                help="all: sfp→sc 순서로 전부 소비. sfp/sc: 해당 task만.",
            )
        with col_limit:
            exec_limit = st.number_input(
                "limit (0=무제한)", min_value=0, max_value=100000,
                value=5, step=1, key="exec_limit",
                help="최대 처리 config 수. 0이면 큐가 빌 때까지.",
            )

        _policy_options = policies or ["cheatcode"]
        _policy_default_idx = (
            _policy_options.index("cheatcode") if "cheatcode" in _policy_options else 0
        )
        _split_active = st.session_state.get("exec_policy_split", False)
        _policy_dir_lines = [
            f"- `{PIXI_POLICIES_DIR}` "
            f"({'✅ 존재' if PIXI_POLICIES_DIR.exists() else '❌ 없음'})",
            f"- `{POLICIES_DIR}` "
            f"({'✅ 존재' if POLICIES_DIR.exists() else '❌ 없음'})",
        ]
        _policy_dir_help = "Policy 탐색 경로:\n" + "\n".join(_policy_dir_lines)
        exec_policy = st.selectbox(
            "Policy (기본)" + (" — 분리 모드 사용 중" if _split_active else ""),
            _policy_options,
            index=_policy_default_idx,
            key="exec_policy",
            disabled=_split_active,
            help=(
                "양쪽 task에 공통 적용되는 기본 policy. "
                "아래 체크박스로 SFP/SC를 분리할 수 있습니다.\n\n"
                + _policy_dir_help
            ),
        )
        st.caption("📁 Policy 탐색 경로  \n" + "  \n".join(_policy_dir_lines))
        exec_policy_split = st.checkbox(
            "SFP / SC에 다른 policy 사용",
            value=False,
            key="exec_policy_split",
            help=(
                "on 시 SFP·SC 각각 policy를 따로 지정. 기본 Policy는 비활성화됩니다. "
                "예: 'SFP=act (학습된 모델), SC=cheatcode'."
            ),
        )
        exec_policy_sfp = None
        exec_policy_sc = None
        if exec_policy_split:
            col_ps, col_pc = st.columns(2)
            with col_ps:
                exec_policy_sfp = st.selectbox(
                    "SFP policy", _policy_options,
                    index=_policy_options.index(exec_policy)
                    if exec_policy in _policy_options else _policy_default_idx,
                    key="exec_policy_sfp",
                    help="sfp task 전용 policy. 워커 내부에서 task별로 dispatch.",
                )
            with col_pc:
                exec_policy_sc = st.selectbox(
                    "SC policy", _policy_options,
                    index=_policy_options.index(exec_policy)
                    if exec_policy in _policy_options else _policy_default_idx,
                    key="exec_policy_sc",
                    help="sc task 전용 policy. 워커 내부에서 task별로 dispatch.",
                )

        # ── 고급 (첫 설정 후 보통 고정) ──
        with st.expander("⚙️ 고급", expanded=False):
            col_gt, col_comp, col_ep = st.columns(3)
            with col_gt:
                exec_ground_truth = st.checkbox(
                    "ground_truth", value=True, key="exec_gt",
                    help="시뮬레이터의 정확한 TF 사용 (수집용). 끄면 평가 모드.",
                )
            with col_comp:
                exec_use_compressed = st.checkbox(
                    "use_compressed", value=True, key="exec_comp",
                    help="카메라 이미지 JPEG 압축 (~3GB/run). 끄면 raw (~58GB/run).",
                )
            with col_ep:
                exec_collect_episode = st.checkbox(
                    "collect_episode", value=False, key="exec_ep",
                    help="이미지+npy를 episode 디렉토리에 저장. 끄면 bag+scoring만.",
                )

            col_timeout, col_recover = st.columns([1, 1])
            with col_timeout:
                exec_timeout = st.number_input(
                    "timeout (초, 0=무제한)", min_value=0, max_value=3600,
                    value=300, step=30, key="exec_timeout",
                    help="config 1개당 최대 실행 시간. 넘으면 failed로 이동.",
                )
            with col_recover:
                exec_recover = st.checkbox(
                    "시작 전 running/→pending 복구",
                    value=True, key="exec_recover",
                    help="비정상 종료로 남은 파일을 복구합니다.",
                )

            # Output root — 결과 탭과 세션으로 공유
            if "shared_output_root" not in st.session_state:
                st.session_state["shared_output_root"] = str(OUTPUT_ROOT)
            if st.session_state.get("exec_output_root") != st.session_state["shared_output_root"]:
                st.session_state["exec_output_root"] = st.session_state["shared_output_root"]

            def _exec_output_root_changed() -> None:
                st.session_state["shared_output_root"] = st.session_state["exec_output_root"]

            exec_output_root = st.text_input(
                "Output root",
                key="exec_output_root",
                help="실행 결과(bag, scoring)가 저장되는 루트 경로. 결과 탭과 값이 공유됩니다.",
                on_change=_exec_output_root_changed,
            )

            act_model_path = None
            _uses_act = exec_policy in ("act", "hybrid") or (
                exec_policy_split and (
                    (exec_policy_sfp in ("act", "hybrid"))
                    or (exec_policy_sc in ("act", "hybrid"))
                )
            )
            if _uses_act:
                _default_act = str(
                    Path.home()
                    / "ws_aic/src/aic/outputs/train/act_aic_v1_backup/checkpoints/last/pretrained_model"
                )
                act_model_path = st.text_input(
                    "ACT 모델 경로", value=_default_act, key="exec_act_model",
                    help="act/hybrid 선택 시 사용. SFP·SC 공용.",
                )

        if st.button("▶ 워커 시작", type="primary", key="exec_start"):
            if not exec_queue_root.exists():
                st.error(f"큐 루트가 존재하지 않습니다: {exec_queue_root}")
            else:
                try:
                    _worker_start(
                        root=str(exec_queue_root),
                        task=exec_task,
                        limit=int(exec_limit) if int(exec_limit) > 0 else None,
                        policy=exec_policy,
                        policy_sfp=exec_policy_sfp,
                        policy_sc=exec_policy_sc,
                        act_model_path=act_model_path,
                        ground_truth=exec_ground_truth,
                        use_compressed=exec_use_compressed,
                        collect_episode=exec_collect_episode,
                        output_root=str(exec_output_root),
                        timeout=int(exec_timeout) if int(exec_timeout) > 0 else None,
                        recover_first=exec_recover,
                    )
                    st.success("워커 시작됨. 아래 로그에서 진행 확인.")
                    time.sleep(1)
                    st.rerun()
                except Exception as e:
                    st.error(f"워커 시작 실패: {type(e).__name__}: {e}")

        # 수동 복구 (워커 안 켜고 pending 복구만)
        if exec_queue_root.exists():
            running_total = sum(_all_counts(exec_queue_root)[t].running for t in _TT)
            if running_total > 0:
                if st.button(f"↩ running/ {running_total}개 → pending/ 복구 (워커 시작 없이)",
                             key="exec_manual_recover"):
                    moved_total = 0
                    for tt in _TT:
                        moved_total += _recover(exec_queue_root, tt)
                    st.success(f"{moved_total}개 복구됨")
                    st.rerun()

    # 실시간 로그
    st.divider()
    st.markdown("### 📜 워커 로그")

    # 툴바 (검색·다운로드·클리어) — auto-refresh 밖
    col_search, col_dl, col_clear = st.columns([4, 1, 1])
    with col_search:
        exec_log_search = st.text_input(
            "🔍 검색",
            key="exec_log_search",
            placeholder="🔍 substring 검색 (대소문자 무시) — 예: ERROR, fail, config_sfp_0003",
            label_visibility="collapsed",
        )
    with col_dl:
        try:
            _full_log = WORKER_LOG_FILE.read_text() if WORKER_LOG_FILE.exists() else ""
        except Exception:
            _full_log = ""
        from datetime import datetime as _dt_dl
        st.download_button(
            "⬇ 다운로드",
            data=_full_log or "(empty)\n",
            file_name=f"worker_log_{_dt_dl.now().strftime('%Y%m%d_%H%M%S')}.log",
            mime="text/plain",
            use_container_width=True,
            disabled=not _full_log,
            key="exec_log_dl",
        )
    with col_clear:
        with st.popover("🗑 비우기", use_container_width=True):
            st.warning("로그 파일 내용을 삭제합니다.")
            if st.button("실행", key="exec_log_clear_confirm", type="primary"):
                if WORKER_LOG_FILE.exists():
                    WORKER_LOG_FILE.write_text("")
                st.rerun(scope="app")

    def _render_log_body() -> None:
        import html as _html
        import re as _re
        if not WORKER_LOG_FILE.exists():
            st.caption("(아직 실행 이력 없음)")
            return
        try:
            log_text = WORKER_LOG_FILE.read_text()
        except Exception:
            log_text = ""
        if not log_text.strip():
            st.caption("(로그 비어있음)")
            return

        all_lines = log_text.splitlines()
        search = (st.session_state.get("exec_log_search") or "").strip()
        if search:
            filtered = [ln for ln in all_lines if search.lower() in ln.lower()]
        else:
            filtered = all_lines
        display = filtered[-200:]

        # 레벨 색상
        pat_err = _re.compile(r"\b(ERROR|FAIL|FATAL|Traceback|Exception)\b", _re.IGNORECASE)
        pat_warn = _re.compile(r"\b(WARN|WARNING)\b", _re.IGNORECASE)
        pat_done = _re.compile(r"\[done ?\]")
        pat_fail_tag = _re.compile(r"\[fail ?\]")

        rendered: list[str] = []
        for ln in display:
            esc = _html.escape(ln) or "&nbsp;"
            if pat_err.search(ln):
                color = "#dc3545"
                bg = "rgba(220,53,69,0.08)"
            elif pat_warn.search(ln):
                color = "#b8860b"
                bg = "rgba(184,134,11,0.08)"
            elif pat_fail_tag.search(ln):
                color = "#dc3545"
                bg = "transparent"
            elif pat_done.search(ln):
                color = "#198754"
                bg = "transparent"
            else:
                color = "inherit"
                bg = "transparent"
            rendered.append(
                f'<div style="color:{color};background:{bg};'
                f'white-space:pre-wrap;word-break:break-all;'
                f'padding:0 4px;">{esc}</div>'
            )
        body_html = "".join(rendered)
        st.markdown(
            f'<div style="background:#0b1021;color:#e7eaf6;padding:10px;'
            f'border-radius:6px;max-height:400px;overflow:auto;'
            f'font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;'
            f'line-height:1.45;">{body_html}</div>',
            unsafe_allow_html=True,
        )

        filter_note = f" · 필터 '{search}'" if search else ""
        st.caption(f"표시: {len(display)}/{len(filtered)}줄{filter_note}")

    # 로그 본문 — 워커 실행 중에만 3초마다 자동 갱신
    _log_refresh = 3 if _worker_status() else None

    @st.fragment(run_every=_log_refresh)
    def _live_log() -> None:
        _render_log_body()

    _live_log()

    st.caption(
        "실시간 상세 로그는 Prefect 대시보드에서 확인할 수 있습니다: "
        f"[Open Prefect]({get_prefect_ui_url()})"
    )

# --- 결과 탭 ---
with tab_results:
    st.subheader("수집 결과")

    # 저장 경로 — 작업 실행 탭과 세션으로 공유
    if "shared_output_root" not in st.session_state:
        st.session_state["shared_output_root"] = str(OUTPUT_ROOT)
    if st.session_state.get("result_output_root") != st.session_state["shared_output_root"]:
        st.session_state["result_output_root"] = st.session_state["shared_output_root"]

    def _result_output_root_changed() -> None:
        st.session_state["shared_output_root"] = st.session_state["result_output_root"]

    result_output_root_str = st.text_input(
        "📁 저장 경로",
        key="result_output_root",
        help="run_*/trial_* 결과를 스캔할 루트. 작업 실행 탭과 값이 공유됩니다.",
        on_change=_result_output_root_changed,
    )
    result_output_root = Path(result_output_root_str).expanduser()
    if not result_output_root.exists():
        st.caption(f"⚠ 경로가 존재하지 않습니다: `{result_output_root}`")

    col_refresh, col_prefect = st.columns([1, 2])
    with col_refresh:
        if st.button("새로고침", key="refresh_results"):
            pass  # rerun 트리거
    with col_prefect:
        st.link_button(
            "🔍 Prefect 대시보드",
            get_prefect_ui_url(),
            help="과거 수집 실행 이력과 task별 상세 로그 확인",
        )

    rows = load_results(result_output_root)
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

        # 검증 경고
        validations = load_run_validations(result_output_root)
        if validations:
            with st.expander(f"⚠️  검증 경고 ({len(validations)}개 run)", expanded=False):
                for v in validations:
                    st.markdown(
                        f"**{v['run']}** — {v['passed']}/{v['total']} 체크 통과"
                    )
                    for w in v["warnings"]:
                        st.markdown(f"  - ⚠️ {w}")

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
                    target_path = result_output_root / del_target
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

