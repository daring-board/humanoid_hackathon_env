#!/usr/bin/env python3
"""pick_move.run() と arm_controller.ArmController の単体テスト（実機・カメラ不要）。

実行:
    pixi run test-pick-move
"""
import contextlib
import io
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import arm_controller  # noqa: E402
import pick_move  # noqa: E402
from arm_controller import ArmController  # noqa: E402


class FakeRecognizer:
    run_id = "test-run"
    zones = {"A", "B"}

    def __init__(self, target, boxes=()):
        self.target = target
        self.boxes = list(boxes)

    def start(self): pass
    def stop(self, close_source=False): pass

    def pop_box(self, timeout=None):
        return self.boxes.pop(0) if self.boxes else None

    def get_target(self, zone):
        return self.target


class FakeLoco:
    def __init__(self):
        self.calls = []

    def Move(self, vx, vy, vyaw): self.calls.append("Move"); return 0
    def StopMove(self): self.calls.append("StopMove"); return 0
    def WaveHand(self): self.calls.append("WaveHand"); return 0


class FakeArm:
    mode = "preset"

    def __init__(self):
        self.calls = []

    def grab(self): self.calls.append("grab")
    def release(self): self.calls.append("release")
    def emergency_release(self): self.calls.append("emergency_release")


def parse(*argv):
    return pick_move.build_parser().parse_args(["--source", "camera", *argv]) \
        if False else pick_move.build_parser().parse_args(list(argv))


class TestPickMoveRun(unittest.TestCase):
    def _run(self, target, *argv, boxes=()):
        args = parse("--once", "--zone", "A", *argv)
        loco, arm = FakeLoco(), FakeArm()
        rec = FakeRecognizer(target, boxes)
        t = [0.0]

        def clock():
            t[0] += 0.05
            return t[0]

        with mock.patch.object(pick_move, "recognizer_from_args", return_value=rec), \
                mock.patch.object(pick_move, "post_event"), \
                contextlib.redirect_stdout(io.StringIO()):
            events = pick_move.run(args, source=mock.Mock(), loco=loco, arm=arm,
                                   clock=clock, sleep=lambda s: None)
        return events, loco, arm

    def test_arrived_grabs_then_releases(self):
        events, loco, arm = self._run({"offset": 0.0, "size_ratio": 0.5})
        self.assertEqual(arm.calls, ["grab", "release", "emergency_release"])
        self.assertEqual([e["event"] for e in events], ["moving", "arrived"])
        self.assertEqual(events[-1]["zone"], "A")
        self.assertEqual(events[0]["data"]["arm_mode"], "preset")
        self.assertEqual(loco.calls[-1], "StopMove")

    def test_marker_lost_reports_error_and_releases(self):
        events, _, arm = self._run(None, "--lost-timeout", "0.1")
        self.assertEqual([e["event"] for e in events], ["moving", "error"])
        self.assertEqual(events[-1]["error"]["code"], "MOVE_FAILED")
        self.assertEqual(arm.calls, ["grab", "release", "emergency_release"])

    def test_wave_gesture(self):
        _, loco, _ = self._run({"offset": 0.0, "size_ratio": 0.5}, "--gesture", "wave")
        self.assertIn("WaveHand", loco.calls)

    def test_unknown_zone_rejected(self):
        args = parse("--once", "--zone", "Z")
        with mock.patch.object(pick_move, "recognizer_from_args", return_value=FakeRecognizer(None)), \
                self.assertRaises(pick_move.PickMoveError):
            pick_move.run(args, source=mock.Mock(), loco=FakeLoco(), arm=FakeArm(),
                          clock=lambda: 0.0, sleep=lambda s: None)

    def test_arm_args_have_defaults(self):
        args = parse()
        self.assertEqual(args.arm_mode, "preset")
        self.assertEqual(args.custom_grab, "grab_box")
        self.assertFalse(args.execute)


class FakeArmClient:
    log = []

    def SetTimeout(self, t): self.log.append(("timeout", t))
    def Init(self): self.log.append("init")
    def ExecuteAction(self, i): self.log.append(("action", i)); return 0
    def ExecuteCustomAction(self, n): self.log.append(("custom", n)); return 0
    def ReleaseArm(self): self.log.append("release"); return 0
    def StopCustomAction(self): self.log.append("stop"); return 0


class TestArmController(unittest.TestCase):
    def setUp(self):
        FakeArmClient.log = []
        fake = types.SimpleNamespace(R1ArmActionClient=FakeArmClient)
        patches = [mock.patch.dict(sys.modules, {"r1_clients": fake}),
                   mock.patch.object(arm_controller.time, "sleep", lambda s: None),
                   mock.patch("sys.stdout", io.StringIO()),
                   mock.patch("sys.stderr", io.StringIO())]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_dry_run_creates_no_client(self):
        for mode in ("preset", "custom", "sdk", "none"):
            arm = ArmController(mode=mode, execute=False)
            arm.grab(); arm.release(); arm.emergency_release()
        self.assertEqual(FakeArmClient.log, [])

    def test_preset_sequence(self):
        arm = ArmController(mode="preset", execute=True)
        arm.grab(); arm.release()
        self.assertEqual(FakeArmClient.log,
                         [("timeout", 30.0), "init", ("action", 15), ("action", 17), "release"])

    def test_custom_default_release(self):
        arm = ArmController(mode="custom", execute=True)
        arm.grab(); arm.release()
        self.assertEqual(FakeArmClient.log[2:], [("custom", "grab_box"), "release"])

    def test_custom_named_release(self):
        arm = ArmController(mode="custom", execute=True, custom_release="open")
        FakeArmClient.log.clear()
        arm.release()
        self.assertEqual(FakeArmClient.log, [("custom", "open"), "release"])

    def test_emergency_release(self):
        arm = ArmController(mode="preset", execute=True)
        FakeArmClient.log.clear()
        arm.emergency_release()
        self.assertEqual(FakeArmClient.log, ["stop", "release"])

    def test_none_mode_does_nothing(self):
        arm = ArmController(mode="none", execute=True)
        arm.grab(); arm.release(); arm.emergency_release()
        self.assertEqual(FakeArmClient.log, [])

    def test_error_code_warns(self):
        arm = ArmController(mode="preset", execute=True)
        with mock.patch.object(FakeArmClient, "ExecuteAction", return_value=7):
            arm.grab()
        self.assertIn("エラーコード 7", sys.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
