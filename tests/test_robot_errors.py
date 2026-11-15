#!/usr/bin/env python3
"""実機の通し実行で出たエラー(DDS 未初期化・腕のエラーを握りつぶす・Move の 127・壊れた JPEG)の回帰テスト。

pytest 不要: `python tests/test_robot_errors.py`(pixi run test-robot-errors)。
実機・SDK・外部通信は使わず、偽モジュール(sys.modules)と一時ディレクトリだけで動く。
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
DEMO = SCRIPTS / "demo"
for _p in (str(SCRIPTS), str(DEMO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import arm_controller  # noqa: E402
import camera_source  # noqa: E402
import dds_util  # noqa: E402
import phase2_grasp  # noqa: E402
import phase3_move  # noqa: E402
import pick_move  # noqa: E402
from arm_controller import ArmController, ArmError  # noqa: E402

SOI = bytes([0xFF, 0xD8])
EOI = bytes([0xFF, 0xD9])


# ---------------------------------------------------------------- 共通の偽物

@contextlib.contextmanager
def quiet():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


def fake_channel_modules(init_calls: list, raises: Exception | None = None) -> dict:
    """unitree_sdk2py.core.channel の偽物(ChannelFactoryInitialize の呼び出しを記録する)。"""
    def init(*args):
        init_calls.append(args)
        if raises is not None:
            raise raises

    mods = {}
    for name in ("unitree_sdk2py", "unitree_sdk2py.core", "unitree_sdk2py.core.channel"):
        mods[name] = types.ModuleType(name)
    mods["unitree_sdk2py.core.channel"].ChannelFactoryInitialize = init
    return mods


@contextlib.contextmanager
def patched(modules: dict):
    """sys.modules を差し替え、DDS 初期化済みフラグと time.sleep(待ち時間)も整える。"""
    dds_util._initialized = False
    with mock.patch.dict(sys.modules, modules), mock.patch.object(time, "sleep", lambda s: None):
        try:
            yield
        finally:
            dds_util._initialized = False


ACTIONS = {"release_arm": 99, "hug": 19, "clamp": 17, "forward_push": 36}


class FakeArmClient:
    """ExecuteAction / ExecuteCustomAction の戻り値を台本どおりに返す腕クライアント。"""
    instances: list = []
    action_codes: list = []      # ExecuteAction の戻り値(尽きたら最後の値を繰り返す)
    custom_code = 0
    action_list = (0, [["hug"], ["wave_box", "pick_up"]])

    def __init__(self):
        self.log = []
        type(self).instances.append(self)

    def SetTimeout(self, t): self.log.append(("timeout", t))
    def Init(self): self.log.append("init")

    def ExecuteAction(self, a):
        self.log.append(("action", a))
        codes = type(self).action_codes
        return codes.pop(0) if len(codes) > 1 else (codes[0] if codes else 0)

    def ExecuteCustomAction(self, n): self.log.append(("custom", n)); return type(self).custom_code
    def ReleaseArm(self): self.log.append("release"); return 0
    def StopCustomAction(self): self.log.append("stop"); return 0

    def GetActionList(self):
        self.log.append("list")
        res = type(self).action_list
        if isinstance(res, Exception):
            raise res
        return res


def arm_modules(action_codes=(0,), custom_code=0, action_list=(0, [["hug"], ["wave_box", "pick_up"]])) -> dict:
    FakeArmClient.instances = []
    FakeArmClient.action_codes = list(action_codes)
    FakeArmClient.custom_code = custom_code
    FakeArmClient.action_list = action_list
    return {"r1_clients": types.SimpleNamespace(R1ArmActionClient=FakeArmClient, R1_ARM_ACTIONS=ACTIONS)}


# ---------------------------------------------------------------- A. DDS 初期化

def test_ensure_dds_without_sdk():
    with patched({"unitree_sdk2py": None, "unitree_sdk2py.core": None, "unitree_sdk2py.core.channel": None}):
        try:
            with quiet():
                dds_util.ensure_dds(0, "eth0")
        except dds_util.DdsError as e:
            assert "unitree_sdk2py" in str(e) and "pixi" in str(e), str(e)
        else:
            raise AssertionError("DdsError が出ていない")


def test_ensure_dds_once_and_error():
    calls = []
    with patched(fake_channel_modules(calls)):
        with quiet() as (out, _):
            dds_util.ensure_dds(0, "Ethenet")
            dds_util.ensure_dds(0, "Ethenet")
        assert len(calls) == 1 and calls[0] == (0, "Ethenet"), calls
        assert out.getvalue().count("DDS を初期化: domain=0 interface=Ethenet") == 1, out.getvalue()
    calls = []
    with patched(fake_channel_modules(calls, raises=Exception("channel factory init error."))):
        try:
            with quiet():
                dds_util.ensure_dds(0, "Typo")
        except dds_util.DdsError as e:
            assert "Typo" in str(e) and "Get-NetAdapter" in str(e) and "192.168.123.164" in str(e), str(e)
        else:
            raise AssertionError("DdsError が出ていない")
        assert not dds_util._initialized  # 失敗したら初期化済み扱いにしない


class FakeLoco:
    """R1LocoClient の偽物。Move の戻り値と GetFsmId の戻り値を差し替えられる。"""
    instances: list = []
    fsm = (0, 811)
    move_code = 0

    def __init__(self):
        self.moves = 0
        self.stops = 0
        type(self).instances.append(self)

    def SetTimeout(self, t): pass
    def Init(self): pass
    def GetFsmId(self): return type(self).fsm

    def Move(self, vx, vy, vyaw):
        self.moves += 1
        return type(self).move_code

    def StopMove(self): self.stops += 1; return 0


def loco_modules(fsm=(0, 811), move_code=0, calls=None) -> dict:
    FakeLoco.instances, FakeLoco.fsm, FakeLoco.move_code = [], fsm, move_code
    mods = fake_channel_modules(calls if calls is not None else [])
    mods["r1_clients"] = types.SimpleNamespace(R1LocoClient=FakeLoco, FSM_LOCKED_STAND=4, FSM_RUN=811)
    return mods


def test_phase3_make_loco_inits_dds_with_videohub():
    calls = []
    args = phase3_move.build_parser().parse_args(["--source", "videohub", "--execute", "-i", "Ethenet"])
    with patched(loco_modules(calls=calls)), quiet():
        loco = phase3_move.make_loco(args)
    assert isinstance(loco, FakeLoco) and calls == [(0, "Ethenet")], calls


# ---------------------------------------------------------------- B. 腕のエラー

def new_arm(**kw) -> ArmController:
    with quiet():
        return ArmController(mode=kw.pop("mode", "preset"), execute=kw.pop("execute", True), **kw)


def test_arm_hug_default_releases_first_then_retries_on_7401():
    with patched(arm_modules(action_codes=[7401, 0])):
        arm = new_arm()
        client = FakeArmClient.instances[0]
        client.log.clear()
        with quiet():
            arm.grab()
        assert client.log == ["release", ("action", 19), "release", ("action", 19)], client.log


def test_arm_hug_ok_sends_one_action():
    with patched(arm_modules(action_codes=[0])):
        arm = new_arm()
        client = FakeArmClient.instances[0]
        client.log.clear()
        with quiet():
            arm.grab()
        assert client.log == ["release", ("action", 19)], client.log


def test_arm_grab_preset_option_and_persistent_7401():
    with patched(arm_modules(action_codes=[7401])):
        arm = new_arm(grab_preset="forward_push")
        client = FakeArmClient.instances[0]
        client.log.clear()
        try:
            with quiet():
                arm.grab()
        except ArmError as e:
            assert e.code == 7401 and "7401" in str(e) and "姿勢保持" in str(e), str(e)
        else:
            raise AssertionError("ArmError が出ていない")
        assert client.log.count(("action", 36)) == 2, client.log  # 再試行は1回だけ


def test_arm_other_error_code_raises_without_retry():
    with patched(arm_modules(action_codes=[7404])):
        arm = new_arm()
        client = FakeArmClient.instances[0]
        client.log.clear()
        try:
            with quiet():
                arm.grab()
        except ArmError as e:
            assert e.code == 7404, str(e)
        else:
            raise AssertionError("ArmError が出ていない")
        assert client.log.count(("action", 19)) == 1, client.log


def test_arm_custom_7403_lists_saved_actions():
    with patched(arm_modules(custom_code=7403)):
        arm = new_arm(mode="custom", custom_grab="grasp_box")
        try:
            with quiet():
                arm.grab()
        except ArmError as e:
            msg = str(e)
            assert e.code == 7403 and "grasp_box" in msg and "保存されていません(7403)" in msg, msg
            assert "wave_box" in msg and "pick_up" in msg and "保存済み:" in msg, msg
        else:
            raise AssertionError("ArmError が出ていない")
    with patched(arm_modules(custom_code=7403, action_list=RuntimeError("timeout"))):
        arm = new_arm(mode="custom", custom_grab="grasp_box")
        try:
            with quiet():
                arm.grab()
        except ArmError as e:
            assert "grasp_box" in str(e) and "保存済み:" not in str(e), str(e)
        else:
            raise AssertionError("ArmError が出ていない")


def test_arm_release_error_raises():
    with patched(arm_modules()):
        arm = new_arm()
        with mock.patch.object(FakeArmClient, "ReleaseArm", return_value=7399):
            try:
                with quiet():
                    arm.release()
            except ArmError as e:
                assert e.code == 7399
            else:
                raise AssertionError("ArmError が出ていない")


def test_arm_dry_run_sends_nothing():
    with patched(arm_modules()):
        for mode in ("preset", "custom", "none"):
            arm = new_arm(mode=mode, execute=False)
            with quiet():
                arm.grab()
                arm.release()
                arm.emergency_release()
        assert FakeArmClient.instances == []


def run_phase_main(main, argv, modules):
    """フェーズの main を偽 SDK 付きで実行し (終了コード, 状態JSON, イベント一覧, stderr) を返す。"""
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        full = list(argv) + ["--state", str(d / "state.json"), "--log", str(d / "events.jsonl")]
        with patched(modules), quiet() as (_, err):
            rc = main(full)
        state = json.loads((d / "state.json").read_text(encoding="utf-8"))
        events = [json.loads(x) for x in (d / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    return rc, state, events, err.getvalue()


def test_phase2_fails_on_arm_error():
    mods = arm_modules(action_codes=[7401])
    mods.update(fake_channel_modules([]))
    rc, state, events, err = run_phase_main(phase2_grasp.main, ["--force", "--execute", "-i", "x"], mods)
    assert rc == 1, rc
    ph = state["phases"]["grasp"]
    assert ph["status"] == "failed" and "7401" in ph["message"], ph
    errs = [e for e in events if e["event"] == "error"]
    assert errs and errs[0]["error"]["code"] == "ROBOT_FAULT", events
    client = FakeArmClient.instances[0]
    assert "stop" in client.log and client.log[-1] == "release", client.log  # 失敗時は腕を安全解放する


def test_phase2_custom_7403_fails_with_list():
    mods = arm_modules(custom_code=7403)
    mods.update(fake_channel_modules([]))
    rc, state, _, _ = run_phase_main(
        phase2_grasp.main, ["--force", "--execute", "-i", "x", "--arm-mode", "custom"], mods)
    assert rc == 1 and state["phases"]["grasp"]["status"] == "failed"
    assert "grasp_box" in state["phases"]["grasp"]["message"] and "pick_up" in state["phases"]["grasp"]["message"]


def test_phase2_success_and_dds_error():
    mods = arm_modules(action_codes=[0])
    calls = []
    mods.update(fake_channel_modules(calls))
    rc, state, _, _ = run_phase_main(phase2_grasp.main, ["--force", "--execute", "-i", "x"], mods)
    assert rc == 0 and state["phases"]["grasp"]["status"] == "done" and len(calls) == 1
    mods = arm_modules()
    mods.update(fake_channel_modules([], raises=Exception("channel factory init error.")))
    rc, state, events, _ = run_phase_main(phase2_grasp.main, ["--force", "--execute", "-i", "Typo"], mods)
    assert rc == 1 and "Typo" in state["phases"]["grasp"]["message"]
    assert [e for e in events if e["event"] == "error"][0]["error"]["code"] == "ROBOT_FAULT"
    assert FakeArmClient.instances == []


# ---------------------------------------------------------------- C. 移動のエラー

def fake_time():
    t = [0.0]

    def sleep(s):
        t[0] += max(0.0, s)
    return (lambda: t[0]), sleep


def test_approach_fixed_aborts_after_3_consecutive_errors():
    loco = FakeLoco()
    FakeLoco.move_code = 127
    clock, sleep = fake_time()
    appr = pick_move.FixedApproach(1.0, 0.0, 0.0, 0.2, 0.3, 30.0)
    with quiet() as (_, err):
        state, reason = pick_move._approach_fixed(loco, appr, None, clock, sleep)
    assert state == "failed" and "127" in reason and "走行モード" in reason, (state, reason)
    assert loco.moves == 3 and loco.stops >= 1, (loco.moves, loco.stops)
    assert err.getvalue().count("[ヒント]") == 1, err.getvalue()  # 推定原因の表示は最初の1回だけ


def test_approach_fixed_single_error_continues():
    class Once(FakeLoco):
        def Move(self, vx, vy, vyaw):
            self.moves += 1
            return 127 if self.moves in (1, 4) else 0  # 単発のエラー(連続しない)

    loco = Once()
    clock, sleep = fake_time()
    appr = pick_move.FixedApproach(0.4, 0.0, 0.0, 0.2, 0.3, 30.0)
    with quiet() as (_, err):
        state, reason = pick_move._approach_fixed(loco, appr, None, clock, sleep)
    assert state == "arrived", (state, reason)
    assert err.getvalue().count("127") >= 2


def test_dryrun_loco_and_loco_without_getfsmid():
    assert pick_move.read_fsm_id(pick_move.DryRunLoco()) is None
    assert pick_move.check_run_mode(object()) is None
    clock, sleep = fake_time()
    appr = pick_move.FixedApproach(0.2, 0.0, 0.0, 0.2, 0.3, 30.0)
    with quiet():
        state, _ = pick_move._approach_fixed(pick_move.DryRunLoco(clock=clock), appr, None, clock, sleep)
    assert state == "arrived"


def test_check_run_mode_values():
    with quiet() as (out, err):
        FakeLoco.fsm = (0, 811)
        assert pick_move.check_run_mode(FakeLoco()) == 811
        FakeLoco.fsm = (3104, None)  # 取得失敗は警告だけで続行
        assert pick_move.check_run_mode(FakeLoco()) is None
        assert "3104" in err.getvalue()
        FakeLoco.fsm = (0, 4)
        try:
            pick_move.check_run_mode(FakeLoco())
        except pick_move.PickMoveError as e:
            assert "811" in str(e) and "現在 4" in str(e) and "R2 + A" in str(e), str(e)
        else:
            raise AssertionError("PickMoveError が出ていない")


def test_phase3_refuses_when_not_run_mode():
    calls = []
    # 走行モードへの自動切り替えは Locked Stand(4) からだけ(pick_move.ensure_running_mode)。
    # ダンピング(1)など他の状態では切り替えずに失敗する
    mods = loco_modules(fsm=(0, 1), calls=calls)
    rc, state, events, _ = run_phase_main(
        phase3_move.main, ["--force", "--execute", "--zone", "A", "--source", "videohub", "-i", "x"], mods)
    assert rc == 1, rc
    ph = state["phases"]["move"]
    assert ph["status"] == "failed" and "FSM ID 1" in ph["message"], ph
    assert [e for e in events if e["event"] == "error"][0]["error"]["code"] == "ROBOT_FAULT"
    assert FakeLoco.instances and all(l.moves == 0 for l in FakeLoco.instances)  # Move は1回も呼ばれない
    assert len(calls) == 1, calls  # --source videohub でも DDS はこのプロセスで初期化される


def test_phase3_fails_when_move_keeps_erroring():
    mods = loco_modules(fsm=(0, 811), move_code=127)
    rc, state, _, _ = run_phase_main(
        phase3_move.main, ["--force", "--execute", "--zone", "A", "--approach-x", "1.0", "--approach-y", "0",
                           "--approach-yaw", "0", "--source", "videohub", "-i", "x", "--max-seconds", "20"], mods)
    ph = state["phases"]["move"]
    assert rc == 1 and ph["status"] == "failed" and "127" in ph["message"], (rc, ph)


def test_phase3_arrives_in_run_mode():
    mods = loco_modules(fsm=(0, 811))
    rc, state, events, _ = run_phase_main(
        phase3_move.main, ["--force", "--execute", "--zone", "B", "--approach-x", "0", "--approach-y", "0",
                           "--approach-yaw", "0", "--source", "videohub", "-i", "x"], mods)
    assert rc == 0 and state["phases"]["move"]["status"] == "done", state["phases"]["move"]
    assert any("FSM ID = 811" in e.get("detail", "") for e in events)


# ---------------------------------------------------------------- D. videohub の JPEG

def make_jpeg() -> bytes:
    img = np.zeros((64, 96, 3), np.uint8)
    img[:, :, 0] = np.linspace(0, 255, 96, dtype=np.uint8)
    img[:, :, 1] = np.linspace(0, 255, 64, dtype=np.uint8)[:, None]
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return bytes(buf)


class FakeVideoClient:
    frames: list = []

    def SetTimeout(self, t): pass
    def Init(self): pass

    def GetImageSample(self):
        return type(self).frames.pop(0)


def video_modules() -> dict:
    names = ("unitree_sdk2py", "unitree_sdk2py.go2", "unitree_sdk2py.go2.video",
             "unitree_sdk2py.go2.video.video_client")
    mods = {n: types.ModuleType(n) for n in names}
    mods["unitree_sdk2py.go2.video.video_client"].VideoClient = FakeVideoClient
    return mods


def test_clean_jpeg_rules():
    good = make_jpeg()
    assert camera_source.clean_jpeg(good) == good
    assert camera_source.clean_jpeg(good + b"\x00" * 20) == good  # 余分な末尾は切り捨て
    assert camera_source.clean_jpeg(good + b"\x00" * 100) is None  # 末尾から 64 バイトを超えて EOI が前にある
    assert camera_source.clean_jpeg(good[: len(good) // 2]) is None  # 途中で切れた
    assert camera_source.clean_jpeg(b"junk" + good) is None  # SOI で始まらない
    assert camera_source.clean_jpeg(b"") is None and camera_source.clean_jpeg(SOI) is None


def test_videohub_source_decodes_and_drops():
    good = make_jpeg()
    FakeVideoClient.frames = [
        (0, good),
        (0, good + b"\x00" * 20),            # 余分な末尾付き -> decode できる
        (0, good[: len(good) // 2]),         # 途中で切れた -> 捨てる
        (0, b"junk" + good),                 # SOI で始まらない -> 捨てる
        (3104, None),                        # エラーコード -> None(捨てた枚数には数えない)
    ]
    with patched(video_modules()), quiet():
        src = camera_source.VideoHubSource()
        got = [src.read() for _ in range(5)]
    assert got[0] is not None and got[0].shape == (64, 96, 3)
    assert got[1] is not None and got[1].shape == (64, 96, 3)
    assert got[2] is None and got[3] is None and got[4] is None
    st = src.stats()
    assert st["decoded"] == 2 and st["dropped"] == 2, st


def test_videohub_drop_warning_is_rate_limited():
    good = make_jpeg()
    bad = good[: len(good) // 2]
    t = [100.0]
    FakeVideoClient.frames = [(0, bad)] * 30
    with patched(video_modules()), quiet() as (_, err):
        src = camera_source.VideoHubSource(clock=lambda: t[0])
        for _ in range(30):
            src.read()  # 時刻が進まない間は警告は最初の1回だけ
        assert err.getvalue().count("壊れていて捨てました") == 1, err.getvalue()
        t[0] += 3.5
        FakeVideoClient.frames = [(0, bad)] * 2
        src.read()
        assert err.getvalue().count("壊れていて捨てました") == 2
    # 割合が低ければ警告しない
    FakeVideoClient.frames = [(0, good)] * 40 + [(0, bad)] * 5
    with patched(video_modules()), quiet() as (_, err):
        src = camera_source.VideoHubSource(clock=lambda: t[0])
        for _ in range(45):
            src.read()
        assert "壊れていて捨てました" not in err.getvalue()


def test_source_from_args_videohub_inits_dds():
    calls = []
    mods = fake_channel_modules(calls)
    mods.update(video_modules())
    args = types.SimpleNamespace(source="videohub", domain=0, interface="Ethenet", index=0, path=None, port=5001)
    with patched(mods), mock.patch.object(camera_source, "check_network", lambda: True), quiet():
        src = camera_source.source_from_args(args)
        camera_source.source_from_args(args)
    assert isinstance(src, camera_source.VideoHubSource) and calls == [(0, "Ethenet")], calls


TESTS = [
    test_ensure_dds_without_sdk, test_ensure_dds_once_and_error, test_phase3_make_loco_inits_dds_with_videohub,
    test_arm_hug_default_releases_first_then_retries_on_7401, test_arm_hug_ok_sends_one_action,
    test_arm_grab_preset_option_and_persistent_7401, test_arm_other_error_code_raises_without_retry,
    test_arm_custom_7403_lists_saved_actions, test_arm_release_error_raises, test_arm_dry_run_sends_nothing,
    test_phase2_fails_on_arm_error, test_phase2_custom_7403_fails_with_list, test_phase2_success_and_dds_error,
    test_approach_fixed_aborts_after_3_consecutive_errors, test_approach_fixed_single_error_continues,
    test_dryrun_loco_and_loco_without_getfsmid, test_check_run_mode_values,
    test_phase3_refuses_when_not_run_mode, test_phase3_fails_when_move_keeps_erroring,
    test_phase3_arrives_in_run_mode,
    test_clean_jpeg_rules, test_videohub_source_decodes_and_drops, test_videohub_drop_warning_is_rate_limited,
    test_source_from_args_videohub_inits_dds,
]


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    ok = 0
    for fn in TESTS:
        try:
            fn()
        except Exception:
            import traceback
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
        else:
            ok += 1
            print(f"PASS {fn.__name__}")
    print(f"{ok}/{len(TESTS)} 件成功")
    return 0 if ok == len(TESTS) else 1


if __name__ == "__main__":
    sys.exit(main())
