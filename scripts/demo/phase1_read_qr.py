#!/usr/bin/env python3
"""Step 1: QRコードの読み取り。Nextアクション(区画A or B)を決めて状態JSONに書く。

箱のQR(`{"box_id":"B001","zone":"A"}` または `B001:A`)が確定するまで、timeout 秒カメラを読み続ける。
確定したら state["box"] = {box_id, zone, scanned_at, verified} を書いて完了。
--gemini を付けると、見た目の照合(Gemini)の結果も待って verified に入れる(照合の不一致では止めない)。

例:
  python scripts/demo/phase1_read_qr.py --source webcam
  python scripts/demo/phase1_read_qr.py --source file --path work/qr/B001_A.png --timeout 5
  python scripts/demo/phase1_read_qr.py --source videohub -i eth0 --gemini
"""
from __future__ import annotations

import argparse
import sys
import time

from common import PhaseError, add_common_args, now_iso, run_phase, setup_console

from camera_source import add_source_args, source_from_args  # common が scripts/ を sys.path に入れる
from recognition_link import add_recognizer_args, recognizer_from_args
from viewer import Viewer, add_show_args


def build_parser():
    parser = argparse.ArgumentParser(description="Step 1: 箱のQRを読んで区画(A/B)を決める")
    add_common_args(parser)
    add_source_args(parser)
    add_recognizer_args(parser)
    add_show_args(parser)
    parser.add_argument("--timeout", type=float, default=60.0, help="QRが確定するまで待つ秒数")
    return parser


def body(ph):
    args = ph.args
    ph.step(f"カメラ取得元を開く: {args.source}", source=args.source)
    source = source_from_args(args)
    rec = recognizer_from_args(args, source, run_id=ph.run_id)
    viewer = Viewer("Step1 read QR (q: quit)", args.show)
    logged, unknown_seen, box, verified = 0, False, None, None

    def flush():
        """認識側が作ったイベント(scanned / error / verified)をログへ写す。"""
        nonlocal logged, unknown_seen, verified
        evs = rec.events()
        for ev in evs[logged:]:
            ph.copy_event(ev)
            code = (ev.get("error") or {}).get("code")
            unknown_seen |= code == "UNKNOWN_QR"
            if ev["event"] == "verified":
                verified = True
            elif code == "CONTENT_MISMATCH":
                verified = False
        logged = len(evs)

    try:
        ph.step(f"QRを探す(最大 {args.timeout:g} 秒)")
        rec.start()
        deadline = time.monotonic() + args.timeout
        while box is None:
            box = rec.pop_box()
            viewer.pump(rec, f"Step1 waiting for QR: {max(0, deadline - time.monotonic()):.0f}s left", 0.1)
            flush()
            if box is None and time.monotonic() >= deadline:
                if unknown_seen:
                    raise PhaseError("UNKNOWN_QR", "QRは読めたが、想定外の形式または区画だった")
                raise PhaseError("QR_READ_FAILED", f"{args.timeout:g} 秒以内にQRを読めなかった")
        ph.step(f"QR確定: 箱 {box['box_id']} -> 区画 {box['zone']}", box_id=box["box_id"], zone=box["zone"])
        if args.gemini:
            ph.step("見た目の照合(Gemini)の結果を待つ")
            end = time.monotonic() + args.gemini_timeout + 2.0
            while verified is None and time.monotonic() < end:
                viewer.pump(rec, "Step1 waiting for Gemini check", 0.2)
                flush()
            ph.step({True: "見た目の照合: 一致", False: "見た目の照合: 不一致(処理は続行)",
                     None: "見た目の照合: 結果なし(スキップ・失敗・時間切れ)"}[verified], verified=verified)
        viewer.pump(rec, f"OK: box {box['box_id']} -> zone {box['zone']}", 1.0)  # 結果を一瞬見せる
    finally:
        viewer.close()
        rec.stop(close_source=True)
        flush()
    ph.state["box"] = {"box_id": box["box_id"], "zone": box["zone"], "scanned_at": now_iso(), "verified": verified}
    return {"box_id": box["box_id"], "zone": box["zone"], "verified": verified}


def main(argv=None) -> int:
    setup_console()
    return run_phase("read_qr", build_parser().parse_args(argv), body)


if __name__ == "__main__":
    sys.exit(main())
