#!/usr/bin/env python3
"""Step 4: Boxのリリース(目的位置で Box を解放・設置)。

ロボットに事前登録した腕動作を再生する。既定はカスタム動作 `release_box`(ティーチング機能で保存しておく)。
再生後に ReleaseArm(腕の制御を通常状態へ戻す)を送る(--keep-arm で送らない)。既定は dry-run。

例:
  python scripts/demo/phase4_release.py --force                    # dry-run(単体確認)
  python scripts/demo/phase4_release.py -i eth0 --execute          # 実機
"""
from __future__ import annotations

import argparse
import sys

from common import (PhaseError, add_arm_args, add_common_args, add_robot_args, make_arm_client, play_arm_action,
                    run_phase, setup_console)


def build_parser():
    parser = argparse.ArgumentParser(description="Step 4: 箱を離す(既定は dry-run)")
    add_common_args(parser)
    add_robot_args(parser)
    add_arm_args(parser, default_custom="release_box")
    parser.add_argument("--keep-arm", action="store_true", help="再生後に ReleaseArm を送らない")
    return parser


def body(ph):
    box = ph.box
    zone = (ph.state.get("destination") or {}).get("zone") or (box or {}).get("zone")
    ph.step(f"箱 {box['box_id']} を区画 {zone} で離す" if box else "箱情報なし(--force)で離す動作を行う")
    client = make_arm_client(ph.args)
    result = play_arm_action(ph, client, "リリース")
    if not ph.args.keep_arm:
        ph.step("腕の制御を通常状態へ戻す(ReleaseArm)")
        code = client.ReleaseArm()
        if code not in (0, None):
            raise PhaseError("ROBOT_FAULT", f"ReleaseArm がエラーコード {code} を返した")
    ph.step("リリース動作が完了")
    return result


def main(argv=None) -> int:
    setup_console()
    return run_phase("release", build_parser().parse_args(argv), body)


if __name__ == "__main__":
    sys.exit(main())
