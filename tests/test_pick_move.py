#!/usr/bin/env python3
"""担当A (ロボット制御): 単体テストおよび構文検証テスト

実行:
    pixi run python tests/test_pick_move.py
"""
import sys
from pathlib import Path
import unittest

# scripts/ ディレクトリを import 対象に追加
sys.path.append(str(Path(__file__).parent.parent / "scripts"))

from pick_move import PickRobotController, EventReporter


class TestPickMove(unittest.TestCase):
    """PickRobotController のドライラン・モジュールテスト。"""

    def setUp(self):
        # DDS 通信を行わないドライランモードでインスタンス化
        self.controller = PickRobotController(dry_run=True, event_url="http://127.0.0.1:9999/events")

    def test_event_reporter_payload(self):
        """イベント JSON のフォーマット検証 (docs/log_schema.md 準拠)。"""
        reporter = EventReporter(enabled=False, run_id="20261115-test")
        payload = reporter.send(
            "moving",
            box_id="BOX-100",
            zone="A",
            detail="区画Aへ移動",
            data={"from": "start", "to": "A"},
        )

        self.assertEqual(payload["v"], 1)
        self.assertEqual(payload["robot"], "R1")
        self.assertEqual(payload["run_id"], "20261115-test")
        self.assertEqual(payload["source"], "control")
        self.assertEqual(payload["event"], "moving")
        self.assertEqual(payload["box_id"], "BOX-100")
        self.assertEqual(payload["zone"], "A")
        self.assertEqual(payload["data"]["to"], "A")
        self.assertIn("+", payload["ts"])  # タイムゾーン検証

    def test_go_to_zone_a(self):
        """Zone A へのドライラン移動シーケンス検証。"""
        # 例外なく正常終了することを確認
        try:
            self.controller.go_to("A", box_id="B001")
        except Exception as e:
            self.fail(f"go_to('A') raised exception: {e}")

    def test_go_to_zone_b(self):
        """Zone B へのドライラン移動シーケンス検証。"""
        try:
            self.controller.go_to("ZONE_B", box_id="B002")
        except Exception as e:
            self.fail(f"go_to('ZONE_B') raised exception: {e}")

    def test_arm_actions(self):
        """腕動作メソッドの呼び出し検証。"""
        try:
            self.controller.point_zone()
            self.controller.release_arm()
            self.controller.wave_hand()
            self.controller.release_arm()
        except Exception as e:
            self.fail(f"Arm actions raised exception: {e}")

    def test_unknown_zone_handling(self):
        """未知のゾーンが渡された際のエラーハンドリング検証。"""
        # 未知のゾーンでも例外で落ちず、安全に終了すること
        try:
            self.controller.go_to("UNKNOWN_ZONE", box_id="B999")
        except Exception as e:
            self.fail(f"Unknown zone raised exception: {e}")


if __name__ == "__main__":
    unittest.main()
