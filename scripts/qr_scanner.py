#!/usr/bin/env python3
"""OpenCV (cv2.QRCodeDetector) を用いた QR コード読み取りモジュール。

- 単一画像からのデコード (scan_image)
- Web カメラからのリアルタイムデコード (scan_camera)
- QR ペイロードの構造化パース (parse_qr_payload)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
from typing import Callable

import cv2
import numpy as np


class QRScanner:
    """QR コードの検出とデコードを行うクラス。"""

    def __init__(self, cooldown_seconds: float = 2.0):
        """
        Args:
            cooldown_seconds: 同一 QR コードの重複読み取りを防ぐ待機時間（秒）
        """
        self.detector = cv2.QRCodeDetector()
        self.cooldown_seconds = cooldown_seconds
        self._last_detected_text: str | None = None
        self._last_detected_time: float = 0.0

    def detect_and_decode(self, frame: np.ndarray) -> tuple[str, np.ndarray | None]:
        """画像フレームから QR コードを検出し、テキストと頂点座標を返す。

        Returns:
            (decoded_text, points): 検出されなければ ("", None)
        """
        retval, decoded_info, points, _ = self.detector.detectAndDecodeMulti(frame)
        if retval and len(decoded_info) > 0:
            for text, pts in zip(decoded_info, points):
                if text:
                    return text.strip(), pts

        # 単一検出フォールバック
        text, pts, _ = self.detector.detectAndDecode(frame)
        return (text.strip() if text else "", pts)

    def is_new_detection(self, text: str) -> bool:
        """クールダウン判定を行い、新規またはクールダウン経過後の検出かを判定する。"""
        if not text:
            return False
        now = time.time()
        if text != self._last_detected_text or (now - self._last_detected_time) >= self.cooldown_seconds:
            self._last_detected_text = text
            self._last_detected_time = now
            return True
        return False

    def scan_image(self, image_path: str | Path) -> str:
        """静止画像ファイルを読み込んで QR コードのテキストを返す。見つからない場合は空文字。"""
        img_path = Path(image_path)
        if not img_path.exists():
            raise FileNotFoundError(f"Image file not found: {img_path}")

        frame = cv2.imread(str(img_path))
        if frame is None:
            raise ValueError(f"Failed to load image: {img_path}")

        text, _ = self.detect_and_decode(frame)
        return text

    def run_camera_loop(
        self,
        camera_id: int = 0,
        on_detected: Callable[[str, dict], None] | None = None,
        show_preview: bool = False,
        max_duration: float | None = None,
    ) -> None:
        """カメラからリアルタイムで QR コードをスキャンするループ。

        Args:
            camera_id: カメラデバイス番号 (デフォルト 0)
            on_detected: QR 検出時に呼ばれるコールバック関数 (raw_text, parsed_dict)
            show_preview: OpenCV の GUI ウィンドウを表示するかどうか
            max_duration: 最大実行時間（秒）。None の場合は停止されるまで継続
        """
        cap = cv2.VideoCapture(camera_id)
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open camera device {camera_id}")

        print(f"[QRScanner] Camera {camera_id} opened. Scanning started...")
        start_time = time.time()

        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    print("[QRScanner] Failed to read frame from camera.")
                    time.sleep(0.1)
                    continue

                text, pts = self.detect_and_decode(frame)

                if text and self.is_new_detection(text):
                    parsed = parse_qr_payload(text)
                    print(f"[QRScanner] Detected QR: '{text}' -> Parsed: {parsed}")
                    if on_detected:
                        on_detected(text, parsed)

                if show_preview:
                    # 検出枠の描画
                    if pts is not None and len(pts) > 0:
                        pts_int = pts.astype(int).reshape((-1, 1, 2))
                        cv2.polylines(frame, [pts_int], True, (0, 255, 0), 2)
                        if text:
                            cv2.putText(
                                frame,
                                text,
                                (pts_int[0][0][0], max(pts_int[0][0][1] - 10, 20)),
                                cv2.FONT_HERSHEY_SIMPLEX,
                                0.6,
                                (0, 255, 0),
                                2,
                            )
                    cv2.imshow("QR Scanner (Press 'q' to exit)", frame)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        print("[QRScanner] Exit requested by user.")
                        break

                if max_duration is not None and (time.time() - start_time) >= max_duration:
                    print(f"[QRScanner] Reached max duration ({max_duration}s). Stopping.")
                    break

                time.sleep(0.03)  # ~30 FPS
        finally:
            cap.release()
            if show_preview:
                cv2.destroyAllWindows()


def parse_qr_payload(raw: str) -> dict:
    """QR コードのテキスト内容を解析し、辞書型にして返す。

    対応形式:
      1. JSON 形式: `{"destination": "ZONE_A", "item_id": "123"}`
      2. Key-Value 形式: `DEST:ZONE_A` / `ZONE=A`
      3. 単一値: `ZONE_A` -> `{"destination": "ZONE_A"}`
    """
    raw_clean = raw.strip()

    # 1. JSON
    if raw_clean.startswith("{") and raw_clean.endswith("}"):
        try:
            data = json.loads(raw_clean)
            if isinstance(data, dict):
                data.setdefault("raw", raw_clean)
                return data
        except json.JSONDecodeError:
            pass

    # 2. Key-Value
    if ":" in raw_clean or "=" in raw_clean:
        delimiter = ":" if ":" in raw_clean else "="
        parts = [p.strip() for p in raw_clean.split(delimiter, 1)]
        key, val = parts[0].lower(), parts[1]
        key_map = {"dest": "destination", "zone": "destination", "target": "destination"}
        mapped_key = key_map.get(key, key)
        return {"destination": val, mapped_key: val, "raw": raw_clean}

    # 3. Simple Destination Name
    return {"destination": raw_clean, "raw": raw_clean}


def main() -> None:
    parser = argparse.ArgumentParser(description="QR Code Scanner CLI")
    parser.add_argument("--image", type=str, default=None, help="Scan single image file")
    parser.add_argument("--camera", type=int, default=None, help="Scan from camera index (e.g. 0)")
    parser.add_argument("--gui", action="store_true", help="Show OpenCV preview window (for camera mode)")
    parser.add_argument("--duration", type=float, default=None, help="Max duration in seconds for camera mode")
    args = parser.parse_args()

    scanner = QRScanner()

    if args.image:
        print(f"Scanning image: {args.image}")
        text = scanner.scan_image(args.image)
        if text:
            parsed = parse_qr_payload(text)
            print(f"SUCCESS: Found QR Code!\n  Raw:    {text}\n  Parsed: {parsed}")
        else:
            print("FAILURE: No QR Code detected in image.")
    elif args.camera is not None:
        scanner.run_camera_loop(
            camera_id=args.camera,
            show_preview=args.gui,
            max_duration=args.duration,
            on_detected=lambda text, parsed: print(f"-> Callback triggered: {parsed.get('destination')}"),
        )
    else:
        # デフォルトで test_qrs/ の画像をテスト
        test_img = Path("data/test_qrs/qr_zone_a.png")
        if test_img.exists():
            print(f"No arguments provided. Running test scan on {test_img}...")
            text = scanner.scan_image(test_img)
            parsed = parse_qr_payload(text)
            print(f"Result:\n  Raw:    {text}\n  Parsed: {parsed}")
        else:
            parser.print_help()


if __name__ == "__main__":
    main()
