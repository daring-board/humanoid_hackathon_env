#!/usr/bin/env python3
"""ルートスクリプト: デモシナリオの各フェーズ(phase0〜phase4)を順に別プロセスで呼び出す。

フェーズ間の受け渡しは状態JSON(logs/state.json)、記録はイベントログ(logs/events.jsonl)。
いずれかのフェーズが失敗(終了コード≠0)したらそこで止める。既定は dry-run(ロボットに送らない)。
ここで解釈しない引数は、各フェーズが受け付けるものだけを渡す(例: --source / -i / --execute / --zone)。

例:
  python scripts/demo/run_demo.py --source webcam                       # 全フェーズを dry-run
  python scripts/demo/run_demo.py --source videohub -i eth0 --execute   # 実機で通し
  python scripts/demo/run_demo.py --from grasp --to release --execute -i eth0   # 途中から(状態JSONを引き継ぐ)
  python scripts/demo/run_demo.py --skip approach --source file --path work/qr/B001_A.png
"""
from __future__ import annotations

import argparse
import importlib
import re
import subprocess
import sys
import time
from pathlib import Path

from common import (DEMO_DIR, LOG_PATH, PHASE_INFO, PHASES, STATE_PATH, load_state, log_event, new_state,
                    phase_end_detail, reset_state, save_state, setup_console)

_PHASE_MODULES = {p: PHASE_INFO[p]["script"][:-3] for p in PHASES}


def filter_args(parser, tokens):
    """tokens のうち、parser が知っているオプション(と、その値)だけを返す。"""
    chunks, cur = [], None
    for t in tokens:
        if t.startswith("-") and not re.match(r"-\d", t):
            cur = [t]
            chunks.append(cur)
        elif cur is not None:
            cur.append(t)
    out = []
    for c in chunks:
        if c[0].split("=", 1)[0] in parser._option_string_actions:
            out += c
    return out


def build_parser():
    p = argparse.ArgumentParser(
        description="デモの全フェーズを順に実行するルートスクリプト(既定は dry-run)",
        epilog="上記以外の引数は各フェーズへ渡す。各フェーズの引数は `python scripts/demo/phaseN_xxx.py --help`。")
    p.add_argument("--from", dest="start", choices=PHASES, default=PHASES[0], help="このフェーズから実行する")
    p.add_argument("--to", dest="stop", choices=PHASES, default=PHASES[-1], help="このフェーズまで実行する")
    p.add_argument("--skip", action="append", choices=PHASES, default=[], metavar="PHASE",
                   help=f"飛ばすフェーズ(複数指定可)。候補: {', '.join(PHASES)}")
    p.add_argument("--state", type=str, default=str(STATE_PATH), help="状態JSONのパス")
    p.add_argument("--log", type=str, default=str(LOG_PATH), help="イベントログ(JSONL)のパス")
    return p


def main(argv=None) -> int:
    setup_console()
    args, extra = build_parser().parse_known_args(argv)
    names = PHASES[PHASES.index(args.start): PHASES.index(args.stop) + 1]
    if not names:
        print("[エラー] --from が --to より後ろです。", file=sys.stderr)
        return 2
    state_path, log_path = Path(args.state), Path(args.log)

    # 先頭フェーズから始めるときは新しい通し実行。途中からは状態JSONを引き継ぐ
    if args.start == PHASES[0]:
        state = reset_state(state_path)
    else:
        state = load_state(state_path)
        if state is None:
            state = new_state()
            save_state(state, state_path)
    run_id = state["run_id"]
    execute = "--execute" in extra
    print(f"##### デモ開始 run_id={run_id} フェーズ={', '.join(names)} "
          f"{'[実機へ送信]' if execute else '[dry-run]'} #####", flush=True)
    log_event("demo_start", run_id=run_id, source="control",
              detail=f"▶▶▶▶▶ デモ開始: {' → '.join(PHASE_INFO[n]['label'] for n in names)}",
              data={"phases": list(names), "skip": args.skip, "execute": execute}, path=log_path)

    sys.path.insert(0, str(DEMO_DIR))
    failed = None
    for name in names:
        if name in args.skip:
            state = load_state(state_path) or state
            state["phases"][name].update(status="skipped", message="--skip で指定")
            save_state(state, state_path)
            log_event("phase_end", run_id=run_id, source=PHASE_INFO[name]["source"],
                      detail=phase_end_detail(name, "skipped", "--skip で指定"),
                      data={"phase": name, "step": PHASE_INFO[name]["step"], "label": PHASE_INFO[name]["label"],
                            "status": "skipped"}, path=log_path)
            print(f"--- {name}: スキップ(--skip)", flush=True)
            continue
        parser = importlib.import_module(_PHASE_MODULES[name]).build_parser()
        cmd = [sys.executable, str(DEMO_DIR / PHASE_INFO[name]["script"]),
               *filter_args(parser, extra), "--state", str(state_path), "--log", str(log_path)]
        t0 = time.monotonic()
        code = subprocess.call(cmd)
        if code != 0:
            failed = (name, code)
            break
        print(f"--- {name}: 終了 ({time.monotonic() - t0:.1f}秒)", flush=True)

    state = load_state(state_path) or state
    ok = failed is None
    log_event("demo_end", run_id=run_id, source="control", box_id=(state.get("box") or {}).get("box_id"),
              zone=(state.get("box") or {}).get("zone"),
              detail="■■■■■ デモ完了 ✅" if ok else f"■■■■■ デモ中断 ❌: {failed[0]} が失敗(終了コード {failed[1]})",
              error=None if ok else {"code": "PHASE_FAILED", "message": f"{failed[0]} が失敗"},
              data={"ok": ok, "failed_phase": failed[0] if failed else None}, path=log_path)
    print(f"##### デモ{'完了' if ok else '中断: ' + failed[0] + ' が失敗'} "
          f"(状態: {state_path}, ログ: {log_path}) #####", flush=True)
    return 0 if ok else (failed[1] or 1)


if __name__ == "__main__":
    sys.exit(main())
