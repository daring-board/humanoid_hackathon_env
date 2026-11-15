#!/usr/bin/env python3
"""Step 1: QRコードの読み取り。Nextアクション(区画A or B)を決めて状態JSONに書く。

箱のQR(`{"box_id":"B001","zone":"A"}` または `B001:A`)が確定するまで、timeout 秒カメラを読み続ける。
確定したら state["box"] = {box_id, zone, scanned_at, verified} を書いて完了。
--gemini を付けると、見た目の照合(Gemini)の結果も待って verified に入れる(照合の不一致では止めない)。

例:
  python scripts/demo/phase1_read_qr.py --source webcam
  python scripts/demo/phase1_read_qr.py --source file --path work/qr/B001_A.png --timeout 5
  python scripts/demo/phase1_read_qr.py --source videohub -i eth0 --gemini

分類動作(別担当)へ結果を渡す口:
  --notify-url URL   QR確定後(--gemini なら照合後)、カメラを閉じてから {"cmd":"sort",...} を POST する(失敗しても続行)
  --then "コマンド"   完了して状態JSONを保存した後にそのコマンドを起動し、終了コードをこのスクリプトの終了コードにする
                     {box_id} {zone} {run_id} {verified} {state} を置き換える。例:
                     --then "python scripts/sort_motion.py --zone {zone} --box {box_id}"
  結果だけ取り出したいときは scripts/demo/next_action.py(状態JSONを読むだけ)。
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time

from common import PhaseError, add_common_args, load_state, log_event, now_iso, run_phase, setup_console

import commands  # scripts/commands.py(common が scripts/ を sys.path に入れる)
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
    parser.add_argument("--notify-url", type=str, default=None,
                        help="QR確定後(--gemini なら照合後)、カメラを閉じてから分類動作へ sort 指示を POST する"
                             "送信先(例 http://localhost:8002/command)。失敗してもフェーズは失敗にしない")
    parser.add_argument("--then", type=str, default=None, metavar="COMMAND",
                        help="フェーズ完了・状態JSON保存後に起動するコマンド。{box_id} {zone} {run_id} {verified} "
                             "{state} を置き換える(シェルは使わない。先頭が python なら実行中の Python)。"
                             "終了コードはそのコマンドのものになる")
    return parser


def split_command(text: str) -> list[str]:
    """コマンド文字列を引数に分ける(シェルは使わない)。Windows ではバックスラッシュを保つため
    posix=False で分け、残る前後の引用符を外す。"""
    if os.name == "nt":
        return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t
                for t in shlex.split(text, posix=False)]
    return shlex.split(text)


def build_then_argv(text: str, values: dict) -> list[str]:
    """--then を引数リストにする。分けてから各引数の {名前} を置き換える(値に空白やバックスラッシュがあっても壊れない)。"""
    argv = split_command(text)
    if not argv:
        raise ValueError("--then が空です")
    out = []
    for tok in argv:
        for k, v in values.items():
            tok = tok.replace("{" + k + "}", str(v))
        out.append(tok)
    if os.path.basename(out[0]).lower() in ("python", "python3", "python.exe", "python3.exe")             and os.path.dirname(out[0]) == "":
        out[0] = sys.executable  # pixi 環境の Python を使う
    return out


def run_then(args) -> int:
    """状態JSON保存後に --then を起動する。開始と終了コードを events.jsonl に記録し、終了コードを返す。"""
    state = load_state(args.state) or {}
    box = state.get("box") or {}
    run_id = state.get("run_id", "")
    values = {"box_id": box.get("box_id", ""), "zone": box.get("zone", ""), "run_id": run_id,
              "verified": json_bool(box.get("verified")), "state": str(args.state)}

    def log(detail, **data):
        log_event("step", run_id=run_id, source="recognition", box_id=box.get("box_id"), zone=box.get("zone"),
                  detail=detail, data={"phase": "read_qr", **data}, path=args.log)

    try:
        argv = build_then_argv(args.then, values)
    except ValueError as e:
        print(f"[エラー] --then を解釈できません: {e}", file=sys.stderr, flush=True)
        log(f"--then を解釈できない: {e}", then=args.then, started=False)
        return 1
    print(f"[read_qr] --then を起動: {' '.join(argv)}", flush=True)
    log("--then を起動", then=args.then, argv=argv, started=True)
    try:
        code = subprocess.call(argv)
    except OSError as e:
        print(f"[エラー] --then のコマンドを起動できません: {argv[0]} ({e})", file=sys.stderr, flush=True)
        log(f"--then を起動できない: {e}", then=args.then, argv=argv, started=False)
        return 1
    print(f"[read_qr] --then 終了(終了コード {code})", flush=True)
    log(f"--then 終了(終了コード {code})", then=args.then, argv=argv, started=True, exit_code=code)
    return code


def json_bool(v) -> str:
    """verified を true / false / null の文字列にする(--then 用)。"""
    return "null" if v is None else ("true" if v else "false")


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
    scanned_at = now_iso()
    ph.state["box"] = {"box_id": box["box_id"], "zone": box["zone"], "scanned_at": scanned_at, "verified": verified}
    if args.notify_url:  # カメラを閉じた後。失敗してもフェーズは失敗にしない(警告のみ)
        cmd = commands.make_sort(box["box_id"], box["zone"], ph.run_id, verified=verified, scanned_at=scanned_at)
        ok = commands.post_command(args.notify_url, cmd, timeout=3.0)
        if ok:
            ph.step(f"分類動作へ sort 指示を送信: {args.notify_url}", notify_url=args.notify_url, ok=True)
        else:
            ph.step(f"[警告] 分類動作への送信に失敗(フェーズは続行): {args.notify_url}",
                    notify_url=args.notify_url, ok=False)
    return {"box_id": box["box_id"], "zone": box["zone"], "verified": verified}


def main(argv=None) -> int:
    setup_console()
    args = build_parser().parse_args(argv)
    code = run_phase("read_qr", args, body)
    if code == 0 and args.then:
        state = load_state(args.state) or {}
        if state.get("phases", {}).get("read_qr", {}).get("status") == "done":
            return run_then(args)
    return code


if __name__ == "__main__":
    sys.exit(main())
