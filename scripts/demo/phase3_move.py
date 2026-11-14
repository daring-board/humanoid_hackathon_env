#!/usr/bin/env python3
"""Step 3: 目的位置への移動。移動先は固定の場所(区画A or B)なので、ここでは「どこへ移動するか」を決めて
状態JSON(state["destination"])に記録するだけ。カメラ・区画マーカーの確認、ロボットへの移動コマンドは行わない。

区画は Step 1 が state["box"]["zone"] に書いたもの。--zone で上書きできる(単体確認用)。
後続(Step 4 など)や、移動を実装するスクリプトは state["destination"] を読む。

例:
  python scripts/demo/phase3_move.py                # state の箱の区画へ
  python scripts/demo/phase3_move.py --zone B --force
"""
from __future__ import annotations

import argparse
import sys

from common import PhaseError, add_common_args, now_iso, run_phase, setup_console

VALID_ZONES = ("A", "B")


def build_parser():
    parser = argparse.ArgumentParser(description="Step 3: 移動先(区画A/B)を決めて状態JSONに記録する")
    add_common_args(parser)
    parser.add_argument("--zone", type=str, default=None, help="移動先の区画。省略時は状態JSONの箱の区画")
    return parser


def body(ph):
    zone = (ph.args.zone or (ph.box or {}).get("zone") or "").strip().upper()
    origin = "--zone" if ph.args.zone else "box.zone"
    if not zone:
        raise PhaseError("PREREQUISITE", "移動先の区画が不明です(Step 1 が未実行か、--zone を指定してください)")
    if zone not in VALID_ZONES:
        raise PhaseError("UNKNOWN_QR", f"区画 {zone!r} は想定外です(候補: {', '.join(VALID_ZONES)})")
    ph.state["destination"] = {"zone": zone, "decided_at": now_iso(), "from": origin}
    ph.step(f"移動先を区画 {zone} に決定(state の destination に記録)", zone=zone, origin=origin)
    return {"zone": zone}


def main(argv=None) -> int:
    setup_console()
    return run_phase("move", build_parser().parse_args(argv), body)


if __name__ == "__main__":
    sys.exit(main())
