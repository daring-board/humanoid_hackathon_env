"""ダッシュボード（Streamlit）。logs/events.jsonl を1秒ごとに読んで再描画する。

起動: pixi run dashboard   （receiver と同時）
仕様: docs/dashboard_spec.md ／ モック: docs/dashboard_mockup.html
"""
from __future__ import annotations

import json
import time
import urllib.request

import pandas as pd
import streamlit as st

import send_dummy
from common import FRAME_PATH, LOG_PATH, RECEIVER_URL, derive_state, read_events, reset_log, summarize

st.set_page_config(page_title="ピックR1！ ダッシュボード", layout="wide")

STATE_COLORS = {  # (文字色, 背景色)  モックアップのライトテーマと同じ
    "idle": ("#6b778a", "#e7ebf1"),
    "scan": ("#2f6fe0", "#dfe9fc"),
    "move": ("#b7791f", "#faecd0"),
    "done": ("#1f8a5b", "#d9f1e6"),
    "err": ("#c63a3a", "#f9dede"),
}
EVENT_LABELS = {"scanned": "読取", "moving": "移動", "arrived": "到着", "error": "エラー",
                "demo_start": "デモ開始", "demo_end": "デモ終了",
                "phase_start": "▶ フェーズ開始", "phase_end": "■ フェーズ終了", "step": "手順"}
# タイムラインで目立たせる行（背景色, 文字色）。フェーズの開始・終了とデモの開始・終了
ROW_STYLES = {
    "demo_start": ("#1a2230", "#ffffff"), "demo_end": ("#1a2230", "#ffffff"),
    "phase_start": ("#dfe9fc", "#1d4fa8"), "phase_end": ("#d9f1e6", "#17694a"),
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
    st.html(
        f"""
        <div style="display:flex;align-items:center;gap:32px;flex-wrap:wrap;
                    padding:16px 24px;border:1px solid #d8dee7;border-radius:8px;background:#fff;">
          <div style="background:{bg};color:{fg};font-size:28px;font-weight:700;
                      padding:10px 22px;border-radius:999px;">● {s['label']}</div>
          <div><div style="font-size:12px;color:#5d6b7e;">箱ID</div>
               <div style="font-size:26px;font-weight:600;font-family:monospace;">{s['box_id'] or '—'}</div></div>
          <div><div style="font-size:12px;color:#5d6b7e;">分類先</div>
               <div style="font-size:26px;font-weight:600;">{zone}</div></div>
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
        fill, stroke = ("#d9f1e6", "#1f8a5b") if hot else ("#fff", "#d8dee7")
        return (f'<rect x="{rx}" y="{ry}" width="110" height="72" rx="6" fill="{fill}" stroke="{stroke}" '
                f'stroke-width="2"/><text x="{rx + 55}" y="{ry + 40}" text-anchor="middle" font-weight="700" '
                f'fill="#1a2230">区画 {name}</text>')

    st.subheader("現在地（推定）")
    st.html(
        f"""
        <svg viewBox="0 0 420 270" style="width:100%;background:#f5f7fa;border-radius:6px;">
          <path d="M70 135 L330 62" stroke="#d8dee7" stroke-width="2" stroke-dasharray="3 5" fill="none"/>
          <path d="M70 135 L330 208" stroke="#d8dee7" stroke-width="2" stroke-dasharray="3 5" fill="none"/>
          {zone_rect('A', 290, 26)}
          {zone_rect('B', 290, 172)}
          <circle cx="70" cy="135" r="26" fill="#fff" stroke="#5d6b7e" stroke-width="2"/>
          <text x="70" y="139" text-anchor="middle" font-size="11" fill="#5d6b7e">START</text>
          <circle cx="{x}" cy="{y}" r="13" fill="#2f5fd0"/>
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
st.title("ピックR1！ 作業ダッシュボード")


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
        camera_view()
        robot_map(state)
        stats(events)


live()
