#!/usr/bin/env python3
"""Unitree R1 腕制御モジュール（preset / custom / sdk / none 切替対応）。

- preset : R1ArmActionClient.ExecuteAction (both_hands_up -> clamp -> release_arm)
- custom : R1ArmActionClient.ExecuteCustomAction (ティーチング済み動作の再生)
- sdk    : rt/arm_sdk による関節角度直接制御（R1用インデックス 15-19, 22-26 / mode_pr 重み）
           ※Windows環境では周期が約55Hzになるため、必ず吊った状態で検証すること
- none   : 腕を動かさない（歩行単体テスト用）
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Optional

JOINTS = [15, 16, 17, 18, 19, 22, 23, 24, 25, 26, 13, 29, 30]
KP = [50, 50, 40, 40, 30, 50, 50, 40, 40, 30, 50, 15, 15]
KD = [2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 1, 1]
DT = 0.01

ACTION_BOTH_HANDS_UP = 15
ACTION_CLAMP = 17
ACTION_RELEASE_ARM = 99


class _R1ArmSdkDriver:
    """rt/arm_sdk をバックグラウンドスレッドで維持しながら制御するドライバ（実機未検証・吊り下げテスト必須）。"""

    def __init__(self):
        from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber
        from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
        from unitree_sdk2py.utils.crc import CRC

        self.state = None
        self.cmd = unitree_hg_msg_dds__LowCmd_()
        self.crc = CRC()
        self.pub = ChannelPublisher("rt/arm_sdk", LowCmd_)
        self.pub.Init()
        self.sub = ChannelSubscriber("rt/lowstate", LowState_)
        self.sub.Init(lambda m: setattr(self, "state", m), 10)

        self._lock = threading.Lock()
        self._weight = 0.0
        self._active = False
        self._thread: Optional[threading.Thread] = None

    def _publish_once(self):
        with self._lock:
            self.cmd.mode_pr = int(round(max(0.0, min(1.0, self._weight)) * 100))
            self.cmd.crc = self.crc.Crc(self.cmd)
            self.pub.Write(self.cmd)

    def _loop(self):
        while self._active:
            if self.state is not None:
                for j in JOINTS:
                    if self.state.motor_state[j].motorstate != 0:
                        print(f"[緊急警告] 関節 {j} で motorstate 異常を検知！arm_sdk を解放します。", file=sys.stderr, flush=True)
                        with self._lock:
                            self._weight = 0.0
                        self._publish_once()
                        self._active = False
                        return
            self._publish_once()
            time.sleep(DT)

    def enable(self, timeout: float = 5.0):
        t0 = time.time()
        while self.state is None:
            if time.time() - t0 > timeout:
                raise RuntimeError("rt/lowstate を受信できません。ロボットとの接続を確認してください。")
            time.sleep(0.05)

        with self._lock:
            for j, kp, kd in zip(JOINTS, KP, KD):
                m = self.cmd.motor_cmd[j]
                m.q = self.state.motor_state[j].q
                m.dq = 0.0
                m.tau = 0.0
                m.kp = float(kp)
                m.kd = float(kd)
            self._weight = 1.0

        if not self._active:
            self._active = True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def move_joints(self, target_map: dict[int, float], duration: float = 1.5):
        with self._lock:
            q0 = {j: self.cmd.motor_cmd[j].q for j in JOINTS}
            q1 = {j: target_map.get(j, q0[j]) for j in JOINTS}

        t0 = time.time()
        while True:
            r = min(1.0, (time.time() - t0) / max(duration, 0.05))
            with self._lock:
                for j in JOINTS:
                    self.cmd.motor_cmd[j].q = q0[j] + (q1[j] - q0[j]) * r
            if r >= 1.0:
                break
            time.sleep(DT)

    def release(self, duration: float = 1.0):
        if not self._active:
            return
        t0 = time.time()
        while True:
            r = min(1.0, (time.time() - t0) / max(duration, 0.05))
            with self._lock:
                self._weight = 1.0 - r
            if r >= 1.0:
                break
            time.sleep(DT)
        self._active = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)


class ArmController:
    """把持（grab）と解放（release）を提供する統合腕コントローラー。"""

    def __init__(
        self,
        mode: str = "preset",
        execute: bool = False,
        custom_grab: str = "grab_box",
        custom_release: str = "release_arm",
        custom_wait: float = 3.0,
        clamp_roll: float = 0.15,
        open_roll: float = 0.45,
        shoulder_pitch: float = -0.35,
        elbow_angle: float = 0.50,
    ):
        self.mode = mode
        self.execute = execute
        self.custom_grab = custom_grab
        self.custom_release = custom_release
        self.custom_wait = custom_wait
        self.clamp_roll = clamp_roll
        self.open_roll = open_roll
        self.shoulder_pitch = shoulder_pitch
        self.elbow_angle = elbow_angle

        self._arm_client = None
        self._sdk_driver: Optional[_R1ArmSdkDriver] = None

        if not self.execute or self.mode == "none":
            return

        if self.mode in ("preset", "custom"):
            from r1_clients import R1ArmActionClient
            self._arm_client = R1ArmActionClient()
            self._arm_client.SetTimeout(30.0)
            self._arm_client.Init()
            time.sleep(1.0)
        elif self.mode == "sdk":
            self._sdk_driver = _R1ArmSdkDriver()

    def _check(self, label: str, code):
        if code not in (0, None):
            print(f"[警告] {label} がエラーコード {code} を返しました。", file=sys.stderr, flush=True)

    def grab(self):
        if self.mode == "none":
            return

        if not self.execute:
            print(f"[dry-run] Arm grab (mode={self.mode})", flush=True)
            return

        if self.mode == "preset":
            print("[腕制御] プリセット: 構え(both_hands_up=15) -> 把持(clamp=17)", flush=True)
            self._check("both_hands_up", self._arm_client.ExecuteAction(ACTION_BOTH_HANDS_UP))
            time.sleep(0.5)
            self._check("clamp", self._arm_client.ExecuteAction(ACTION_CLAMP))

        elif self.mode == "custom":
            print(f"[腕制御] カスタム動作再生: {self.custom_grab} ({self.custom_wait}s待機)", flush=True)
            self._check(f"Custom({self.custom_grab})", self._arm_client.ExecuteCustomAction(self.custom_grab))
            time.sleep(self.custom_wait)

        elif self.mode == "sdk":
            print("[腕制御] arm_sdk: 開く -> 挟む（バックグラウンド保持開始）", flush=True)
            self._sdk_driver.enable()
            open_pose = {
                15: self.shoulder_pitch, 16: self.open_roll, 18: self.elbow_angle,
                22: self.shoulder_pitch, 23: -self.open_roll, 25: self.elbow_angle,
            }
            self._sdk_driver.move_joints(open_pose, duration=1.5)
            grab_pose = {
                15: self.shoulder_pitch, 16: self.clamp_roll, 18: self.elbow_angle,
                22: self.shoulder_pitch, 23: -self.clamp_roll, 25: self.elbow_angle,
            }
            self._sdk_driver.move_joints(grab_pose, duration=1.5)

    def release(self):
        if self.mode == "none":
            return

        if not self.execute:
            print(f"[dry-run] Arm release (mode={self.mode})", flush=True)
            return

        if self.mode == "preset":
            print("[腕制御] プリセット: 解放(release_arm=99)", flush=True)
            self._check("release_arm", self._arm_client.ReleaseArm())

        elif self.mode == "custom":
            if self.custom_release == "release_arm":
                self._check("release_arm", self._arm_client.ReleaseArm())
            else:
                print(f"[腕制御] カスタム動作再生: {self.custom_release}", flush=True)
                self._check(f"Custom({self.custom_release})", self._arm_client.ExecuteCustomAction(self.custom_release))
                time.sleep(self.custom_wait)
                self._check("release_arm", self._arm_client.ReleaseArm())

        elif self.mode == "sdk":
            print("[腕制御] arm_sdk: 開いて解放（1秒かけて重み解除）", flush=True)
            open_pose = {
                15: self.shoulder_pitch, 16: self.open_roll, 18: self.elbow_angle,
                22: self.shoulder_pitch, 23: -self.open_roll, 25: self.elbow_angle,
            }
            self._sdk_driver.move_joints(open_pose, duration=1.0)
            self._sdk_driver.release(duration=1.0)

    def emergency_release(self):
        if not self.execute or self.mode == "none":
            return
        try:
            if self.mode in ("preset", "custom") and self._arm_client is not None:
                self._arm_client.StopCustomAction()
                self._arm_client.ReleaseArm()
            elif self.mode == "sdk" and self._sdk_driver is not None:
                self._sdk_driver.release(duration=1.0)
        except Exception as e:
            print(f"[警告] 腕の安全解放に失敗: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
