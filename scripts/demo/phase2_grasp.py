#!/usr/bin/env python3
"""Step 2: 掴み動作の実行(ロボットアームによる Box 把持)。

scripts/pick_move.py と同じ ArmController で把持する(--arm-mode preset: 99で保持解除→--grab-preset(既定 hug=19) / custom: 登録済み動作)。
把持した腕は、Step 3(移動)・Step 4(リリース)まで保持したままにする(正常終了時は解放しない)。
どの箱か(box_id / zone)は Step 1 が書いた状態JSONから読む。既定は dry-run、実機へ送るのは --execute のときだけ。

例:
  python scripts/demo/phase2_grasp.py --force                      # dry-run(単体確認)
  python scripts/demo/phase2_grasp.py -i eth0 --execute            # 実機(プリセット)
  python scripts/demo/phase2_grasp.py -i eth0 --execute --arm-mode custom --custom-grab grasp_box
"""
from __future__ import annotations

import argparse
import sys

from common import (PhaseError, add_arm_args, add_common_args, add_robot_args, confirm_execute, make_arm,
                    run_phase, setup_console)
from arm_controller import ArmError  # common が scripts/ を sys.path に入れる


def build_parser():
    parser = argparse.ArgumentParser(description="Step 2: 腕で箱を掴む(既定は dry-run)")
    add_common_args(parser)
    add_robot_args(parser)
    add_arm_args(parser, default_custom="grasp_box")
    return parser


def body(ph):
    box = ph.box
    zone = (box or {}).get("zone")
    ph.step(f"箱 {box['box_id']}(区画 {zone} 行き)を掴む" if box else "箱情報なし(--force)で掴み動作を行う",
            arm_mode=ph.args.arm_mode)
    arm = make_arm(ph.args)
    confirm_execute(ph.args, "腕")
    try:
        arm.grab()
    except ArmError as e:
        arm.emergency_release()
        raise PhaseError("ROBOT_FAULT", f"掴み動作に失敗: {e}") from e
    except BaseException:
        arm.emergency_release()
        raise
    ph.step("掴み動作が完了(腕は保持したまま)")
    return {"arm_mode": ph.args.arm_mode, "box_id": (box or {}).get("box_id"), "zone": zone,
            "dry_run": ph.dry_run}


def main(argv=None) -> int:
    setup_console()
    return run_phase("grasp", build_parser().parse_args(argv), body)


if __name__ == "__main__":
    sys.exit(main())
