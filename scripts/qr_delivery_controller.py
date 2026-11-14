#!/usr/bin/env python3
"""QR コード認識と Unitree R1/G1 搬送動作を連携させる統合コントローラー。

- PC 単体ドライラン（--dry-run）
- モック環境（--domain 1）
- 実機接続（--domain 0 -i Ethernet）
のすべてに対応します。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

# プロジェクト内の scripts ディレクトリを path に追加
sys.path.append(str(Path(__file__).parent))

from qr_scanner import QRScanner, parse_qr_payload


class RobotBridge:
    """ロボット実機 / モック / ドライランを抽象化するブリッジクラス。"""

    def __init__(self, interface: str | None, domain: int, dry_run: bool = False):
        self.interface = interface
        self.domain = domain
        self.dry_run = dry_run

        self.loco = None
        self.audio = None

        if not self.dry_run:
            self._init_sdk()

    def _init_sdk(self) -> None:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
        from r1_clients import R1LocoClient

        try:
            Path("/tmp").mkdir(exist_ok=True)
        except OSError:
            pass
        print(f"[Bridge] Initializing ChannelFactory (domain={self.domain}, iface={self.interface})...")
        ChannelFactoryInitialize(self.domain, self.interface)

        print("[Bridge] Connecting to Loco & Audio services...")
        self.loco = R1LocoClient()
        self.loco.SetTimeout(5.0)
        self.loco.Init()

        self.audio = AudioClient()
        self.audio.SetTimeout(5.0)
        self.audio.Init()

        time.sleep(1.0)  # DDS ディスカバリ待機
        print("[Bridge] DDS services initialized.")

    def speak(self, text: str) -> None:
        """英語音声 (speaker_id=1) で発話する。"""
        print(f"[TTS] >> \"{text}\"")
        if self.dry_run or self.audio is None:
            return
        try:
            # speaker_id=1 (英語)
            self.audio.TtsMaker(text, 1)
        except Exception as e:
            print(f"[TTS Error] {e}")

    def move_velocity(self, vx: float, vy: float, vyaw: float) -> None:
        """速度指令を送信する。"""
        if self.dry_run or self.loco is None:
            return
        self.loco.Move(vx, vy, vyaw)

    def stop(self) -> None:
        """移動を停止する。"""
        print("[Motion] Stopping robot.")
        if self.dry_run or self.loco is None:
            return
        self.loco.StopMove()

    def wave_hand(self) -> None:
        """手振りジェスチャーを実行する。"""
        print("[Motion] Waving hand.")
        if self.dry_run or self.loco is None:
            return
        self.loco.WaveHand(False)

    def walk_duration(self, vx: float, vy: float, vyaw: float, duration: float) -> None:
        """指定された秒数間、安全に速度コマンドを周期的(0.2s)に送信して移動させる。"""
        print(f"[Motion] Walk vx={vx:.2f}, vy={vy:.2f}, vyaw={vyaw:.2f} for {duration:.1f}s")
        if self.dry_run:
            time.sleep(duration)
            return

        t_end = time.time() + duration
        while time.time() < t_end:
            self.move_velocity(vx, vy, vyaw)
            time.sleep(0.2)
        self.stop()


class DeliveryController:
    """QR 情報を元に配送動作シーケンスを管理・実行するコントローラー。"""

    def __init__(self, bridge: RobotBridge):
        self.bridge = bridge

    def execute_route(self, destination: str) -> None:
        """目的地に応じた搬送ルートを実行する。"""
        dest_upper = destination.upper()
        print(f"\n==========================================")
        print(f"[*] Starting delivery mission for: {dest_upper}")
        print(f"==========================================")

        try:
            # 1. 開始アナウンス
            self.bridge.speak(f"Target {dest_upper} confirmed. Starting mission.")
            time.sleep(1.0)

            # 2. 目的地別のルート移動
            if "ZONE_A" in dest_upper or "A" == dest_upper:
                # ルートA: 直進 0.3 m/s で 3 秒
                print("[Mission] Executing Route A: Straight forward")
                self.bridge.walk_duration(vx=0.3, vy=0.0, vyaw=0.0, duration=3.0)

            elif "ZONE_B" in dest_upper or "B" == dest_upper:
                # ルートB: 直進 2 秒 → 右旋回 2 秒
                print("[Mission] Executing Route B: Forward then Turn Right")
                self.bridge.walk_duration(vx=0.3, vy=0.0, vyaw=0.0, duration=2.0)
                self.bridge.walk_duration(vx=0.0, vy=0.0, vyaw=-0.5, duration=2.0)

            elif "ZONE_C" in dest_upper or "C" == dest_upper:
                # ルートC: 直進 2 秒 → 左旋回 2 秒
                print("[Mission] Executing Route C: Forward then Turn Left")
                self.bridge.walk_duration(vx=0.3, vy=0.0, vyaw=0.0, duration=2.0)
                self.bridge.walk_duration(vx=0.0, vy=0.0, vyaw=0.5, duration=2.0)

            else:
                # 未知のゾーン: 短い直進のみ
                print(f"[Mission] Unknown zone: {dest_upper}. Executing standard advance.")
                self.bridge.walk_duration(vx=0.2, vy=0.0, vyaw=0.0, duration=2.0)

            # 3. 到着アナウンスと完了ジェスチャー
            print("[Mission] Arrived at destination.")
            self.bridge.speak(f"Arrived at {dest_upper}. Please receive the package.")
            time.sleep(1.0)
            self.bridge.wave_hand()
            time.sleep(2.0)

        finally:
            self.bridge.stop()
            print("[*] Mission completed safely.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="QR Delivery Controller for Unitree R1/G1")
    # 接続設定
    parser.add_argument("-i", "--interface", default=None, help="NIC name (e.g. Ethernet for real robot, default: auto)")
    parser.add_argument("-d", "--domain", type=int, default=1, help="DDS domain (0: real robot, 1: mock)")
    parser.add_argument("--dry-run", action="store_true", help="Run without DDS / robot connection (offline test)")

    # 入力ソース
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--image", type=str, help="Scan single QR image file")
    group.add_argument("--camera", type=int, help="Scan QR from camera index (e.g. 0)")
    group.add_argument("--qr-text", type=str, help="Directly provide QR text without scanning")

    parser.add_argument("--gui", action="store_true", help="Show camera preview window (camera mode only)")
    parser.add_argument("--duration", type=float, default=30.0, help="Camera scanning duration limit (sec)")

    args = parser.parse_args()

    # ロボットブリッジ初期化
    bridge = RobotBridge(interface=args.interface, domain=args.domain, dry_run=args.dry_run)
    controller = DeliveryController(bridge)

    scanner = QRScanner(cooldown_seconds=5.0)

    # 1. テキスト直接入力
    if args.qr_text:
        parsed = parse_qr_payload(args.qr_text)
        dest = parsed.get("destination", "UNKNOWN")
        controller.execute_route(dest)

    # 2. 画像ファイルからスキャン
    elif args.image:
        print(f"Scanning image: {args.image}")
        text = scanner.scan_image(args.image)
        if not text:
            print(f"Error: No QR code found in {args.image}")
            sys.exit(1)
        parsed = parse_qr_payload(text)
        dest = parsed.get("destination", "UNKNOWN")
        controller.execute_route(dest)

    # 3. カメラからリアルタイムスキャン
    elif args.camera is not None:
        def on_qr(raw_text: str, parsed: dict):
            dest = parsed.get("destination", "UNKNOWN")
            controller.execute_route(dest)

        scanner.run_camera_loop(
            camera_id=args.camera,
            on_detected=on_qr,
            show_preview=args.gui,
            max_duration=args.duration,
        )

    # 4. 引数なしの場合はサンプル画像でドライランデモ
    else:
        sample_img = Path("data/test_qrs/qr_zone_a.png")
        if sample_img.exists():
            print("No source specified. Running demo with sample image and dry-run...")
            # dry-run を強制して安全にテスト
            if not args.dry_run:
                bridge.dry_run = True
            text = scanner.scan_image(sample_img)
            parsed = parse_qr_payload(text)
            dest = parsed.get("destination", "ZONE_A")
            controller.execute_route(dest)
        else:
            parser.print_help()


if __name__ == "__main__":
    main()
