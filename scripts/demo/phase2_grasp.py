#!/usr/bin/env python3
"""Step 2: 掴み動作の実行(ロボットアームによる Box 把持)。

ロボットに事前登録した腕動作を再生する。既定はカスタム動作 `grasp_box`(ティーチング機能で保存しておく)。
プリセットで代用するなら --preset(例: hug)。既定は dry-run、実機へ送るのは --execute のときだけ。

例:
  python scripts/demo/phase2_grasp.py --force                      # dry-run(単体確認)
  python scripts/demo/phase2_grasp.py -i eth0 --execute            # 実機
  python scripts/demo/phase2_grasp.py -i eth0 --execute --preset hug
"""
from __future__ import annotations

import argparse
import sys

from common import (add_arm_args, add_common_args, add_robot_args, make_arm_client, play_arm_action,
                    run_phase, setup_console)


def build_parser():
    parser = argparse.ArgumentParser(description="Step 2: 腕で箱を掴む(既定は dry-run)")
    add_common_args(parser)
    add_robot_args(parser)
    add_arm_args(parser, default_custom="grasp_box")
    return parser


def body(ph):
    box = ph.box
    ph.step(f"箱 {box['box_id']} を掴む" if box else "箱情報なし(--force)で掴み動作を行う")
    client = make_arm_client(ph.args)
    result = play_arm_action(ph, client, "掴み")
    ph.step("掴み動作が完了")
    return result


def main(argv=None) -> int:
    setup_console()
    return run_phase("grasp", build_parser().parse_args(argv), body)


if __name__ == "__main__":
    sys.exit(main())
