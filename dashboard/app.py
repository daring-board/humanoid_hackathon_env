"""ダッシュボード（Streamlit）。logs/events.jsonl を1秒ごとに読んで再描画する。

起動: pixi run dashboard   （receiver と同時）
仕様: docs/dashboard_spec.md ／ モック: docs/dashboard_mockup.html
"""
from __future__ import annotations

import json
from pathlib import Path
import time
import urllib.request

import pandas as pd
import streamlit as st

import send_dummy
from common import FRAME_PATH, LOG_PATH, RECEIVER_URL, derive_state, read_events, reset_log, summarize

st.set_page_config(page_title="ピックR1！ ダッシュボード", page_icon="🤖", layout="wide")

st.markdown(
    """
    <style>
    .stApp { background: #eef4fd; }
    .block-container { padding-top: 2rem; max-width: 1400px; }
    header[data-testid="stHeader"] { background: transparent; }
    h1 { font-weight: 900 !important; letter-spacing: .01em; color: #0b4db5; }
    h1 .r1 { color: #ff6a0a; }
    h3 { font-size: 15px !important; font-weight: 700 !important; color: #0b3f94;
         text-transform: none; margin-bottom: .25rem !important; }
    /* 右カラム／左カラムをカード化 */
    div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"] > div[data-testid="stElementContainer"]:has(h3) {
        margin-top: 4px; }
    div[data-testid="stMetric"] { background:#fff; border:1px solid #d3e1f7; border-top:4px solid #ff6a0a; border-radius:12px;
        padding:14px 18px; box-shadow:0 1px 3px rgba(11,77,181,.10); }
    div[data-testid="stMetricLabel"] { color:#4a6288; }
    div[data-testid="stMetricValue"] { font-weight:800; color:#0b4db5; }
    div[data-testid="stDataFrame"] { border:1px solid #d3e1f7; border-radius:12px; overflow:hidden;
        box-shadow:0 1px 3px rgba(11,77,181,.10); }
    div[data-testid="stImage"] img { border-radius:12px; border:1px solid #d3e1f7;
        box-shadow:0 1px 3px rgba(11,77,181,.10); }
    section[data-testid="stSidebar"] { background:#fff; border-right:3px solid #ff6a0a; }
    section[data-testid="stSidebar"] .stButton > button { border-radius:10px; font-weight:700; border:2px solid #0b4db5; color:#0b4db5; }
    section[data-testid="stSidebar"] .stButton > button:hover { background:#0b4db5; color:#fff; border-color:#0b4db5; }
    @keyframes pulse { 0%{box-shadow:0 0 0 0 var(--pulse)} 70%{box-shadow:0 0 0 14px transparent}
                       100%{box-shadow:0 0 0 0 transparent} }
    </style>
    """,
    unsafe_allow_html=True,
)

LOGO_PATH = Path(__file__).resolve().parent.parent / "docs" / "B1C63B70-0118-48BA-8E98-DC4ADD1B25CB.png"
STATE_COLORS = {  # (文字色, 背景色)  モックアップのライトテーマと同じ
    "idle": ("#5f7090", "#e6ecf6"),
    "scan": ("#0b57c9", "#dbe8fb"),
    "move": ("#e8650a", "#ffe6d2"),
    "done": ("#1f8a5b", "#d9f1e6"),
    "err": ("#c63a3a", "#f9dede"),
}
EVENT_LABELS = {"scanned": "読取", "moving": "移動", "arrived": "到着", "error": "エラー",
                "demo_start": "デモ開始", "demo_end": "デモ終了",
                "phase_start": "▶ フェーズ開始", "phase_end": "■ フェーズ終了", "step": "手順"}
# タイムラインで目立たせる行（背景色, 文字色）。フェーズの開始・終了とデモの開始・終了
ROW_STYLES = {
    "demo_start": ("#0b2a66", "#ffffff"), "demo_end": ("#0b2a66", "#ffffff"),
    "phase_start": ("#dbe8fb", "#0b3f94"), "phase_end": ("#d9f1e6", "#17694a"),
}
PHASE_END_FAIL = ("#f9dede", "#a82d2d")
# マップ上の位置（モックアップの SVG 座標）
POS = {"start": (70, 135), "A": (330, 62), "B": (330, 208)}


# ---------- サイドバー（運用機能） ----------
def receiver_health() -> dict | None:
    try:
        with urllib.request.urlopen(f"{RECEIVER_URL}/health", timeout=1) as res:
            return json.loads(res.read())
    except Exception:
        return None


with st.sidebar:
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH), width="stretch")
    st.header("デモ操作")
    health = receiver_health()
    if health:
        st.success(f"receiver 接続中（{health['count']} 件）")
        st.caption(f"最終受信: {health['last_received_at'] or '—'}")
    else:
        st.error("receiver に接続できません")
        st.caption(f"{RECEIVER_URL}  ／  `pixi run dashboard` で同時起動")

    def _send(fn, *args):
        try:
            fn(*args)
        except Exception as e:  # receiver 未起動など
            st.toast(f"送信失敗: {e}")

    if st.button("箱を流す → 区画A", width="stretch", disabled=not health):
        _send(send_dummy.send_box, f"B{len(read_events()) % 1000:03d}", "A")
    if st.button("箱を流す → 区画B", width="stretch", disabled=not health):
        _send(send_dummy.send_box, f"B{len(read_events()) % 1000:03d}", "B")
    if st.button("誤出荷（想定外QR）", width="stretch", disabled=not health):
        _send(send_dummy.send_error)

    st.divider()
    if st.button("ログをリセット", width="stretch"):
        backup = reset_log()
        st.toast(f"退避: {backup.name}" if backup else "ログはありません")
    st.caption(f"保存先: {LOG_PATH}")


# ---------- 画面部品 ----------
def status_header(s: dict) -> None:
    fg, bg = STATE_COLORS[s["state"]]
    zone = "—" if not s["zone"] else f"区画 {s['zone']}"
    if s["state"] == "err":
        zone = "不明"
    live = s["state"] in ("scan", "move")
    anim = "animation:pulse 1.6s infinite;" if live else ""
    card = ("background:#fff;border:1px solid #d3e1f7;border-radius:14px;padding:16px 24px;"
            "box-shadow:0 1px 3px rgba(11,77,181,.10);")
    lbl = "font-size:12px;color:#4a6288;letter-spacing:.08em;margin-bottom:2px;"
    st.html(
        f"""
        <div style="display:flex;align-items:stretch;gap:16px;flex-wrap:wrap;margin-bottom:8px;">
          <div style="{card}flex:1.4;min-width:260px;border-left:8px solid {fg};display:flex;align-items:center;gap:16px;">
            <div style="--pulse:{fg}55;{anim}background:{bg};color:{fg};font-size:26px;font-weight:800;
                        padding:10px 26px;border-radius:999px;">● {s['label']}</div>
          </div>
          <div style="{card}flex:1;min-width:180px;">
            <div style="{lbl}">箱ID</div>
            <div style="font-size:30px;font-weight:700;font-family:ui-monospace,Menlo,Consolas,monospace;color:#0b2a66;">
              {s['box_id'] or '—'}</div></div>
          <div style="{card}flex:1;min-width:180px;">
            <div style="{lbl}">分類先</div>
            <div style="font-size:30px;font-weight:700;color:#0b2a66;">{zone}</div></div>
        </div>
        """
    )


def alert_banner(events: list[dict]) -> None:
    """直近のイベントが誤出荷（UNKNOWN_QR）なら赤バナーを出す。"""
    s = derive_state(events)
    last = events[-1] if events else {}
    if s["state"] == "err" and (last.get("error") or {}).get("code") == "UNKNOWN_QR":
        st.error(f"誤出荷の可能性: 想定外のQR（{last.get('box_id')}）を検知しました。仕分けせずに保留します。")


def timeline(events: list[dict]) -> None:
    st.subheader("作業タイムライン")
    if not events:
        st.info("まだイベントがありません。サイドバーのデモ操作か send_dummy.py で送信できます。")
        return
    shown = list(reversed(events[-100:]))  # 新しい順
    rows = [
        {
            "時刻": (e.get("received_at") or e["ts"])[11:19],
            "種別": EVENT_LABELS.get(e["event"], e["event"]),
            "箱ID": e.get("box_id") or "—",
            "分類先": e.get("zone") or "—",
            "詳細": ("　" + e["detail"]) if e["event"] == "step" and e.get("detail") else (e.get("detail") or ""),
        }
        for e in shown
    ]

    def row_style(row: pd.Series) -> list[str]:
        e = shown[int(row.name)]  # type: ignore[arg-type]
        colors = ROW_STYLES.get(e["event"])
        if e["event"] == "phase_end" and (e.get("data") or {}).get("status") == "failed":
            colors = PHASE_END_FAIL
        if not colors:
            return [""] * len(row)
        return [f"background-color:{colors[0]};color:{colors[1]};font-weight:700"] * len(row)

    st.dataframe(pd.DataFrame(rows).style.apply(row_style, axis=1), hide_index=True, width="stretch", height=480)


def robot_map(s: dict) -> None:
    """位置は取らず、状態から推定して表示する（idle/scan=スタート、move=中間、done=区画）。"""
    zone = s["zone"] if s["zone"] in ("A", "B") else None
    if s["state"] == "move" and zone:
        sx, sy = POS["start"]; zx, zy = POS[zone]
        x, y = (sx + zx) / 2, (sy + zy) / 2
    elif s["state"] == "done" and zone:
        x, y = POS[zone]
    else:
        x, y = POS["start"]

    def zone_rect(name: str, rx: int, ry: int) -> str:
        hot = s["state"] == "done" and zone == name
        fill, stroke, tc = ("#d9f1e6", "#1f8a5b", "#17694a") if hot else ("#fff", "#9db8e3", "#0b2a66")
        return (f'<rect x="{rx}" y="{ry}" width="110" height="72" rx="12" fill="{fill}" stroke="{stroke}" '
                f'stroke-width="2"/><text x="{rx + 55}" y="{ry + 42}" text-anchor="middle" font-weight="700" '
                f'fill="{tc}">区画 {name}</text>')

    st.subheader("現在地（推定）")
    st.html(
        f"""
        <svg viewBox="0 0 420 270" style="width:100%;background:#fff;border:1px solid #d3e1f7;
             border-radius:12px;box-shadow:0 1px 3px rgba(11,77,181,.10);">
          <defs><pattern id="g" width="20" height="20" patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r="1" fill="#d3e1f7"/></pattern></defs>
          <rect width="420" height="270" rx="12" fill="url(#g)"/>
          <path d="M70 135 L330 62" stroke="#f5a623" stroke-width="2" stroke-dasharray="3 5" fill="none"/>
          <path d="M70 135 L330 208" stroke="#f5a623" stroke-width="2" stroke-dasharray="3 5" fill="none"/>
          {zone_rect('A', 290, 26)}
          {zone_rect('B', 290, 172)}
          <circle cx="70" cy="135" r="28" fill="#fff" stroke="#0b4db5" stroke-width="3"/>
          <text x="70" y="139" text-anchor="middle" font-size="11" font-weight="700" fill="#0b4db5">START</text>
          <circle cx="{x}" cy="{y}" r="20" fill="#ff6a0a" opacity=".22"/>
          <circle cx="{x}" cy="{y}" r="12" fill="#ff6a0a" stroke="#fff" stroke-width="3"/>
        </svg>
        """
    )


FRAME_STALE_SEC = 5  # この秒数更新が無ければ「映像なし」扱い


def camera_view() -> None:
    """scripts/live_frame.py が書き出す logs/latest.jpg を表示する。"""
    st.subheader("R1 カメラ映像")
    try:
        age = time.time() - FRAME_PATH.stat().st_mtime
        data = FRAME_PATH.read_bytes()
    except OSError:  # 無い／書き換え中
        st.info("映像がありません。認識スクリプトを起動すると表示されます。")
        return
    st.image(data, width="stretch")
    if age > FRAME_STALE_SEC:
        st.warning(f"映像が {age:.0f} 秒前から更新されていません")
    else:
        st.caption(f"更新: {age:.1f} 秒前")


def stats(events: list[dict]) -> None:
    st.subheader("集計")
    sm = summarize(events)
    c1, c2, c3 = st.columns(3)
    c1.metric("区画A 処理数", sm["zones"]["A"])
    c2.metric("区画B 処理数", sm["zones"]["B"])
    c3.metric("誤出荷検知", sm["unknown_qr"])


# ---------- 本体（1秒ごとに再描画） ----------
st.markdown('# ピック<span class="r1">R1</span>！ 作業ダッシュボード', unsafe_allow_html=True)


@st.fragment(run_every=1)
def live() -> None:
    events = read_events()
    state = derive_state(events)
    alert_banner(events)
    status_header(state)
    left, right = st.columns([1.25, 1])
    with left:
        timeline(events)
    with right:
        stats(events)
        camera_view()
        robot_map(state)


live()
