#!/usr/bin/env python3
"""Step 0: Boxに近づく(手が届く範囲で、QR読み取りができる位置まで)。

シナリオ上「実装が間に合えばやる。無理なら1から開始」なので、現状は未実装のスタブ。
実行すると状態を skipped にして終了する(後続フェーズはそのまま続けられる)。
実装するときは body() の中に書く。ロボットを動かすなら add_robot_args の --execute で dry-run を切り替える。

例:
  python scripts/demo/phase0_approach.py
"""
from __future__ import annotations

import argparse
import sys

from common import PhaseSkipped, add_common_args, add_robot_args, run_phase, setup_console


def build_parser():
    parser = argparse.ArgumentParser(description="Step 0: Boxに近づく(未実装・スキップ扱い)")
    add_common_args(parser)
    add_robot_args(parser)
    return parser


def body(ph):
    ph.step("未実装のためスキップ。ロボットを手動で箱の前(QRが見える位置)に置いてください")
    raise PhaseSkipped("未実装(手動で位置合わせ)")


def main(argv=None) -> int:
    setup_console()
    return run_phase("approach", build_parser().parse_args(argv), body)


if __name__ == "__main__":
    sys.exit(main())
