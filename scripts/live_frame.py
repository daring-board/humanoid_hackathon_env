"""ダッシュボード表示用に、最新のカメラ映像を logs/latest.jpg へ書き出す。

認識スレッド(recognition_link)から publish() が呼ばれる。ダッシュボード(Streamlit)が
このファイルを定期的に読んで表示する。

- 書き出しは間引く(既定 0.5 秒に1回)。認識ループを遅くしないため。
- 一時ファイルへ書いてから os.replace で差し替える(読み手に壊れた画像を見せない)。
- 出力先は環境変数 LIVE_FRAME で変更できる。LIVE_FRAME=off で無効化。
- 失敗しても例外は投げない(映像表示のために認識を止めない)。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
_env = os.environ.get("LIVE_FRAME", "")
ENABLED = _env.lower() != "off"
FRAME_PATH = Path(_env) if _env and ENABLED else ROOT / "logs" / "latest.jpg"

_MAX_WIDTH = 640
_JPEG_QUALITY = 70
_last = 0.0


def _draw(img, detections) -> None:
    for d in detections or []:
        pts = d.get("points")
        if pts is None or len(pts) < 3:
            continue
        color = (0, 160, 255) if d.get("kind") == "zone" else (0, 200, 0)
        poly = [[int(round(x)), int(round(y))] for x, y in pts]
        cv2.polylines(img, [np.array(poly, dtype="int32")], True, color, 3)
        label = str(d.get("text", ""))
        if label:
            cv2.putText(img, label, (poly[0][0], max(15, poly[0][1] - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)


def publish(frame, detections=None, min_interval: float = 0.5) -> bool:
    """frame(BGR)に検出枠を重ねて保存する。書き出したら True。"""
    global _last
    if not ENABLED or frame is None:
        return False
    now = time.monotonic()
    if now - _last < min_interval:
        return False
    _last = now
    try:
        img = frame.copy()
        h, w = img.shape[:2]
        _draw(img, detections)
        if w > _MAX_WIDTH:
            img = cv2.resize(img, (_MAX_WIDTH, max(1, int(h * _MAX_WIDTH / w))), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY])
        if not ok:
            return False
        FRAME_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = FRAME_PATH.with_name(FRAME_PATH.name + ".tmp")
        tmp.write_bytes(buf.tobytes())
        os.replace(tmp, FRAME_PATH)  # Windows で読み手が開いていると失敗する → 次回に持ち越し
        return True
    except Exception:
        return False
