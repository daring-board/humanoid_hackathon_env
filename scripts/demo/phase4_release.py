#!/usr/bin/env python3
"""Step 4: Boxのリリース(目的位置で Box を解放・設置)。

scripts/pick_move.py と同じ ArmController で解放する(preset: ReleaseArm / custom: 登録済み動作→ReleaseArm)。
どの区画かは Step 3 が書いた状態JSONの destination(なければ box.zone)から読む。既定は dry-run。

例:
  python scripts/demo/phase4_release.py --force                    # dry-run(単体確認)
  python scripts/demo/phase4_release.py -i eth0 --execute          # 実機
"""
from __future__ import annotations

import argparse
import sys

from common import (PhaseError, add_arm_args, add_common_args, add_robot_args, confirm_execute, make_arm,
                    run_phase, setup_console)
from arm_controller import ArmError  # common が scripts/ を sys.path に入れる


def build_parser():
    parser = argparse.ArgumentParser(description="Step 4: 箱を離す(既定は dry-run)")
    add_common_args(parser)
    add_robot_args(parser)
    add_arm_args(parser, default_custom="release_box")
    parser.add_argument("--gesture", choices=("none", "wave"), default="none",
                        help="解放後の動作(wave=手を振る。実機の LocoClient が必要)")
    return parser


def _wave(ph):
    if not ph.args.execute:
        print("[dry-run] WaveHand()", flush=True)
        return
    from pick_move import PickMoveError, make_real_loco
    try:
        code = make_real_loco(ph.args.domain, ph.args.interface).WaveHand()
    except PickMoveError as e:
        raise PhaseError("ROBOT_FAULT", str(e)) from e
    if code not in (0, None):
        ph.step(f"[警告] WaveHand がエラーコード {code} を返した")


def body(ph):
    box = ph.box
    zone = (ph.state.get("destination") or {}).get("zone") or (box or {}).get("zone")
    ph.step(f"箱 {box['box_id']} を区画 {zone} で離す" if box else "箱情報なし(--force)で離す動作を行う",
            arm_mode=ph.args.arm_mode)
    arm = make_arm(ph.args)  # execute 時はここで DDS を初期化する(ensure_dds)
    confirm_execute(ph.args, "腕")
    try:
        arm.release()
    except ArmError as e:
        arm.emergency_release()
        raise PhaseError("ROBOT_FAULT", f"リリース動作に失敗: {e}") from e
    except BaseException:
        arm.emergency_release()
        raise
    if ph.args.gesture == "wave":
        _wave(ph)
    ph.step("リリース動作が完了")
    return {"arm_mode": ph.args.arm_mode, "zone": zone, "gesture": ph.args.gesture, "dry_run": ph.dry_run}


def main(argv=None) -> int:
    setup_console()
    return run_phase("release", build_parser().parse_args(argv), body)


if __name__ == "__main__":
    sys.exit(main())
