"""カメラ映像のプレビュー表示(Webカメラ・実機どちらの取得元でも同じ)。

Recognizer が認識に使った最新フレームに、検出したQRの枠と読んだ文字を重ねて表示する。
 - 箱のQR: 緑の枠 + 読み取った文字 / 区画マーカー: 青の枠 + ZONE・左右・ずれ
 - 左上: 直近のイベント / 下: フェーズの状況
ウィンドウで q を押すとフェーズを中断する(PhaseError INTERRUPTED)。
cv2.imshow はメインスレッドで呼ぶこと(認識は別スレッドなのでメインは空いている)。
"""
from __future__ import annotations

import argparse
import sys
import time

from common import PhaseError


def add_show_args(parser) -> None:
    parser.add_argument("--show", action=argparse.BooleanOptionalAction, default=True,
                        help="カメラ映像と認識結果をウィンドウに表示する(q で中断)。--no-show で無効")


class Viewer:
    def __init__(self, title: str, enabled: bool = True):
        self.title = title
        self.enabled = enabled
        self._cv2 = None
        if enabled:
            import cv2
            from recognize import _draw  # 検出の枠と文字の描画は recognize.py と同じ
            self._cv2, self._draw = cv2, _draw

    def update(self, rec, status: str = "") -> None:
        """1回だけ描画する。q が押されていたら PhaseError を投げる。"""
        if not self.enabled:
            return
        cv2 = self._cv2
        try:
            view = rec.latest_view()
            if view is not None:
                frame = view[0].copy()
                evs = rec.events()
                label = f"{evs[-1]['event']}: {evs[-1]['box_id']} {evs[-1]['zone']}" if evs else ""
                self._draw(frame, view[1], label)
                if status:
                    cv2.putText(frame, status, (10, frame.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX,
                                0.7, (255, 255, 255), 3)
                    cv2.putText(frame, status, (10, frame.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX,
                                0.7, (0, 0, 0), 1)
                cv2.imshow(self.title, frame)
            key = cv2.waitKey(1) & 0xFF
        except cv2.error as e:  # GUI の無い OpenCV(headless)など
            print(f"[警告] 映像を表示できないため表示を止めます: {e}", file=sys.stderr, flush=True)
            self.enabled = False
            return
        if key == ord("q"):
            raise PhaseError("INTERRUPTED", "プレビューで q が押されて中断された")

    def pump(self, rec, status: str, seconds: float) -> None:
        """seconds 秒のあいだ、約30msごとに描画しながら待つ(time.sleep の代わり)。"""
        end = time.monotonic() + seconds
        while True:
            self.update(rec, status)
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.03, left))

    def close(self) -> None:
        if self.enabled and self._cv2 is not None:
            try:
                self._cv2.destroyWindow(self.title)
                self._cv2.waitKey(1)
            except self._cv2.error:
                pass
