#!/usr/bin/env python3
"""ロボット制御担当(担当A)へ渡す指示の形と POST。

  go_to  : 「この箱をこの区画へ運べ」(箱のQRが確定したとき1回)
  target : 「区画マーカーはいまこの位置に見えている/見えていない」(定期的に)
  sort   : 「QRを読んだ結果(箱と区画)」を分類動作の担当へ渡す(phase1 の --notify-url)
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from datetime import datetime

_WARN_INTERVAL = 5.0
_warn_lock = threading.Lock()
_last_warn: dict = {}


def _ts() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def make_go_to(box_id, zone, run_id) -> dict:
    return {"v": 1, "ts": _ts(), "cmd": "go_to", "box_id": box_id, "zone": zone, "run_id": run_id}


def make_sort(box_id, zone, run_id, verified=None, scanned_at=None) -> dict:
    """「この箱をこの区画へ分類せよ」(QRを読んだ結果を、分類動作の担当へ渡す)。"""
    return {"v": 1, "ts": _ts(), "cmd": "sort", "box_id": box_id, "zone": zone, "verified": verified,
            "scanned_at": scanned_at, "run_id": run_id, "source": "recognition"}


def make_target(zone, loc, run_id, visible=None) -> dict:
    """loc は qr_reader.locate の結果。None なら「見えていない」通知(位置系は None)。"""
    if visible is None:
        visible = loc is not None
    loc = loc or {}
    return {
        "v": 1, "ts": _ts(), "cmd": "target", "zone": zone, "visible": bool(visible),
        "offset": loc.get("offset"), "bearing": loc.get("bearing"),
        "size_ratio": loc.get("size_ratio"), "distance_m": loc.get("distance_m"),
        "run_id": run_id,
    }


def post_command(url, cmd, timeout: float = 0.5) -> bool:
    """JSON を POST する。失敗しても例外は出さず False。警告は同じ宛先につき5秒に1回まで。"""
    if not url:
        return False
    try:
        req = urllib.request.Request(
            url, data=json.dumps(cmd, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return 200 <= res.status < 300
    except Exception as e:
        now = time.monotonic()
        with _warn_lock:
            if now - _last_warn.get(url, -1e9) >= _WARN_INTERVAL:
                _last_warn[url] = now
                print(f"[警告] 指示の送信に失敗: {url} {e}", file=sys.stderr, flush=True)
        return False
