#!/usr/bin/env python3
"""QR 読取成功率の計測。距離・角度・照明を変えて何度か回し、CSV に溜めて条件を比べる。

例:
  python scripts/qr_survey.py --source webcam --seconds 10 --label "1.0m 正面 蛍光灯"
  python scripts/qr_survey.py --source file --path work/qr/B001_A.png --seconds 2
  python scripts/qr_survey.py --source webcam --seconds 10 --thorough   # 従来の徹底版 decode_qr と比べる
  python scripts/qr_survey.py --source videohub -i eth0 --label "0.5m 右30度" --save
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))

from camera_source import CameraError, add_source_args, source_from_args  # noqa: E402
from qr_reader import QRScanner, decode_qr, locate, parse_payload, parse_zone_marker  # noqa: E402

CSV_COLUMNS = ["ts", "label", "source", "width", "height", "frames", "fps", "text", "kind",
               "hits", "rate", "size_mean", "size_min", "size_max", "offset_mean",
               "decode_ms_mean", "decode_ms_max", "read_ms_mean", "stage"]


def classify(text) -> str:
    """QRテキストの種類。区画マーカー=zone、箱=box、どちらでもなければ unknown。"""
    if parse_zone_marker(text) is not None:
        return "zone"
    if parse_payload(text) is not None:
        return "box"
    return "unknown"


def summarize(records) -> dict:
    """フレームごとの記録を集計する(純粋関数)。
    records: [{"ms": decode所要ms, "dets": [{"text", "size", "offset"}, ...]}, ...]
    戻り値: {"frames", "hit_frames", "hit_rate", "decode_ms_mean", "decode_ms_max",
             "texts": [{"text","kind","hits","rate","size_mean","size_min","size_max","offset_mean"}]}
    同じフレーム内で同じテキストが複数あれば、そのフレームでは最初の1件だけ数える。"""
    n = len(records)
    per = {}
    hit_frames = 0
    for rec in records:
        seen = set()
        for d in rec.get("dets", []):
            if d["text"] in seen:
                continue
            seen.add(d["text"])
            per.setdefault(d["text"], []).append(d)
        if seen:
            hit_frames += 1
    texts = []
    for text, ds in per.items():
        sizes = [d["size"] for d in ds]
        offs = [d["offset"] for d in ds]
        texts.append({
            "text": text, "kind": classify(text), "hits": len(ds),
            "rate": len(ds) / n if n else 0.0,
            "size_mean": sum(sizes) / len(sizes), "size_min": min(sizes), "size_max": max(sizes),
            "offset_mean": sum(offs) / len(offs),
        })
    texts.sort(key=lambda t: (-t["hits"], t["text"]))
    ms = [r["ms"] for r in records]
    stages = {}
    for rec in records:
        if rec.get("dets") and rec.get("stage"):
            stages[rec["stage"]] = stages.get(rec["stage"], 0) + 1
    reads = [r["read_ms"] for r in records if "read_ms" in r]
    return {
        "stage": max(stages, key=stages.get) if stages else "",
        "read_ms_mean": sum(reads) / len(reads) if reads else 0.0,
        "frames": n, "hit_frames": hit_frames,
        "hit_rate": hit_frames / n if n else 0.0,
        "decode_ms_mean": sum(ms) / len(ms) if ms else 0.0,
        "decode_ms_max": max(ms) if ms else 0.0,
        "texts": texts,
    }


def survey(source, seconds, thorough=False, clock=time.monotonic):
    """seconds 秒フレームを読んで QRScanner(thorough なら徹底版 decode_qr)を回す。
    (records, info, last_hit_frame) を返す。info に last_frame / stats も入る。"""
    records, last_hit = [], None
    shape, last_frame = None, None
    scanner = QRScanner()
    start = clock()
    while clock() - start < seconds:
        r0 = time.perf_counter()
        frame = source.read()
        read_ms = (time.perf_counter() - r0) * 1000.0
        if frame is None:
            time.sleep(0.01)
            continue
        shape, last_frame = frame.shape, frame
        t0 = time.perf_counter()
        if thorough:
            found = decode_qr(frame)
            stage = "thorough" if found else None
        else:
            found = scanner.scan(frame)
            stage = scanner.last_stage
        ms = (time.perf_counter() - t0) * 1000.0
        dets = []
        for text, pts in found:
            loc = locate(pts, frame.shape)
            dets.append({"text": text, "size": loc["size_ratio"], "offset": loc["offset"]})
        records.append({"ms": ms, "read_ms": read_ms, "stage": stage, "dets": dets})
        if dets:
            last_hit = frame
    elapsed = max(clock() - start, 1e-9)
    info = {
        "fps": len(records) / elapsed,
        "width": int(shape[1]) if shape else 0,
        "height": int(shape[0]) if shape else 0,
        "last_frame": last_frame,
        "stats": {"thorough": _thorough_stats(records)} if thorough else scanner.stats(),
    }
    return records, info, last_hit


def _thorough_stats(records):
    ms = [r["ms"] for r in records]
    return {"tries": len(ms), "hits": sum(1 for r in records if r["dets"]),
            "ms_mean": sum(ms) / len(ms) if ms else 0.0}


def csv_rows(summary, info, label, source_name, ts):
    """CSV の行(1計測×1テキスト)。1件も読めなければ text 空で1行。"""
    base = {
        "ts": ts, "label": label, "source": source_name, "width": info["width"],
        "height": info["height"], "frames": summary["frames"], "fps": round(info["fps"], 2),
        "decode_ms_mean": round(summary["decode_ms_mean"], 1),
        "decode_ms_max": round(summary["decode_ms_max"], 1),
        "read_ms_mean": round(summary.get("read_ms_mean", 0.0), 1),
        "stage": summary.get("stage", ""),
    }
    if not summary["texts"]:
        return [{**base, "text": "", "kind": "", "hits": 0, "rate": 0.0, "size_mean": "",
                 "size_min": "", "size_max": "", "offset_mean": ""}]
    return [{**base, "text": t["text"], "kind": t["kind"], "hits": t["hits"],
             "rate": round(t["rate"], 4), "size_mean": round(t["size_mean"], 4),
             "size_min": round(t["size_min"], 4), "size_max": round(t["size_max"], 4),
             "offset_mean": round(t["offset_mean"], 4)} for t in summary["texts"]]


def append_csv(path, rows) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    if not new:
        with open(path, newline="", encoding="utf-8-sig") as f:
            header = next(csv.reader(f), [])
        if header != CSV_COLUMNS:  # 古い形式(列が違う)は別名に退避して新しく作る
            backup = path.with_name(f"{path.stem}.old-{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
            path.rename(backup)
            print(f"列が古い形式のため退避しました: {backup}")
            new = True
    with open(path, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if new:
            w.writeheader()
        w.writerows(rows)


def print_summary(summary, info, label) -> None:
    print(f"--- QR 読取計測{f' [{label}]' if label else ''} ---")
    print(f"解像度: {info['width']}x{info['height']}  総フレーム数: {summary['frames']}  "
          f"実測fps: {info['fps']:.1f}")
    print(f"1フレームの内訳: 取得(source.read) 平均 {summary['read_ms_mean']:.1f} ms"
          f" + 読取 平均 {summary['decode_ms_mean']:.1f} ms")
    print(f"1件以上読めたフレーム: {summary['hit_frames']}/{summary['frames']} "
          f"({summary['hit_rate'] * 100:.1f}%)")
    print(f"読取の処理時間: 平均 {summary['decode_ms_mean']:.1f} ms / 最大 {summary['decode_ms_max']:.1f} ms")
    for name, st in info.get("stats", {}).items():
        print(f"  段 {name:<9} 試行 {st['tries']:>4}  ヒット {st['hits']:>4}  平均 {st['ms_mean']:.1f} ms")
    if summary.get("stage"):
        print(f"最も多く当たった段: {summary['stage']}")
    if not summary["texts"]:
        print("QRは1件も読めませんでした。")
    for t in summary["texts"]:
        print(f"  [{t['kind']}] {t['text']!r}: {t['hits']}フレーム ({t['rate'] * 100:.1f}%) "
              f"size_ratio 平均{t['size_mean']:.3f} 最小{t['size_min']:.3f} 最大{t['size_max']:.3f} "
              f"offset平均 {t['offset_mean']:+.3f}")


def run(args, source=None) -> dict:
    own = source is None
    if own:
        source = source_from_args(args)
    try:
        records, info, last_hit = survey(source, args.seconds, thorough=args.thorough)
    finally:
        if own:
            source.close()
    summary = summarize(records)
    print_summary(summary, info, args.label)
    now = datetime.now()
    append_csv(args.csv, csv_rows(summary, info, args.label, args.source,
                                  now.astimezone().isoformat(timespec="seconds")))
    print(f"CSV に追記しました: {args.csv}")
    if args.save:
        out_dir = Path("work")
        out_dir.mkdir(parents=True, exist_ok=True)
        for tag, img in (("last", info.get("last_frame")), ("hit", last_hit)):
            if img is None:
                continue
            ok, buf = cv2.imencode(".jpg", img)
            if ok:
                out = out_dir / f"qr_survey_{now:%Y%m%d_%H%M%S}_{tag}.jpg"
                buf.tofile(str(out))
                print(f"{'最後のフレーム' if tag == 'last' else '読めたフレーム'}を保存しました: {out}")
        if info.get("last_frame") is None:
            print("フレームを1枚も取得できなかったため画像は保存しません。")
    return summary


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="QR 読取成功率の計測(条件ごとに CSV へ追記)")
    add_source_args(parser)
    parser.add_argument("--seconds", type=float, default=10.0, help="計測する秒数")
    parser.add_argument("--label", type=str, default="", help='計測条件のメモ(例 "1.0m 正面 蛍光灯")')
    parser.add_argument("--csv", type=str, default="work/qr_survey.csv", help="結果の追記先CSV")
    parser.add_argument("--save", action="store_true",
                        help="最後のフレーム(読めなくても)と、読めたフレームがあればそれも work/ に保存")
    parser.add_argument("--thorough", action="store_true",
                        help="従来の徹底版 decode_qr で読む(遅い。QRScanner との比較用)")
    args = parser.parse_args()
    try:
        run(args)
    except CameraError as e:
        print(f"[エラー] {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("中断しました。", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
