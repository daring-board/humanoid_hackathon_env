#!/usr/bin/env python3
"""箱用のQR画像を作る。

例:
  python scripts/make_qr.py --box-id B001 --zone A              # 中身は短い形式 B001:A
  python scripts/make_qr.py --box-id B001 --zone A --json       # 中身は JSON 形式
  python scripts/make_qr.py --box-id B001 B002 --zone A B      # 組にして複数作成
  python scripts/make_qr.py --zone-marker A B                  # 区画マーカー(中身は ZONE:A)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np


# R1 頭部カメラ(1280px幅)で読むための、QRの見かけの幅(px)の目安。
# このPCでの実測(1280x720の実写、ぼかし+JPEG70): 見かけ幅80pxで読めたのは21マス・29マスとも概ね5回中4〜5回、
# 60px以下は不安定、100px以上なら数フレーム以内。マス数に比例しきらないので、下限90px+1マスあたり4.5pxで見積もる。
PX_PER_MODULE = 4.5
MIN_PX = 90


def _encoder(level: str = "M"):
    """誤り訂正レベル(L/M/Q/H)を指定したエンコーダ。指定できない版では既定のものを返す。"""
    try:
        params = cv2.QRCodeEncoder_Params()
        params.correction_level = {
            "L": cv2.QRCodeEncoder_CORRECT_LEVEL_L, "M": cv2.QRCodeEncoder_CORRECT_LEVEL_M,
            "Q": cv2.QRCodeEncoder_CORRECT_LEVEL_Q, "H": cv2.QRCodeEncoder_CORRECT_LEVEL_H,
        }[level.upper()]
        return cv2.QRCodeEncoder.create(params)
    except (AttributeError, cv2.error):
        return cv2.QRCodeEncoder.create()


def module_count(text: str, level: str = "M") -> int:
    """text をQRにしたときの一辺のマス数(静かな余白を除く)。"""
    qr = _encoder(level).encode(text)
    return int(qr.shape[0]) - 4  # エンコーダ出力は両側2マスの余白付き


def min_apparent_width_px(modules: int) -> int:
    """R1 のカメラ(1280px幅)で読むのに必要な、QRの見かけの幅(px)の目安。"""
    return max(MIN_PX, int(round(modules * PX_PER_MODULE / 5.0) * 5))


def encode_qr(text: str, size: int = 600, margin_ratio: float = 0.1, level: str = "M"):
    """text をQRにした BGR 画像(白余白付き、size x size)を返す。"""
    enc = _encoder(level)
    qr = enc.encode(text)
    if qr is None or qr.size == 0:
        raise ValueError(f"QRを生成できません: {text!r}")
    if qr.ndim == 2:
        qr = cv2.cvtColor(qr, cv2.COLOR_GRAY2BGR)
    inner = int(size * (1 - 2 * margin_ratio))
    qr = cv2.resize(qr, (inner, inner), interpolation=cv2.INTER_NEAREST)
    pad = (size - inner) // 2
    canvas = np.full((size, size, 3), 255, np.uint8)
    canvas[pad:pad + inner, pad:pad + inner] = qr
    return canvas


def _hint(text: str, level: str) -> str:
    n = module_count(text, level)
    return (f"\n    {n}x{n} マス。R1 のカメラ(1280px幅)で読むには見かけの幅 約{min_apparent_width_px(n)}px 以上が必要"
            "(画面上で幅が足りなければ、印刷を大きくするか近づける)")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="box_id と zone からQR画像を作る")
    parser.add_argument("--box-id", nargs="+", default=[], help="箱ID(複数可)")
    parser.add_argument("--zone", nargs="+", default=[],
                        help="分類先(box-id と同数、または1つだけ指定して全箱に適用)")
    parser.add_argument("--zone-marker", nargs="+", default=[],
                        help="区画マーカーを作る zone(複数可。ZONE_A.png のように出力、中身は ZONE:A)")
    parser.add_argument("--json", action="store_true",
                        help='箱QRの中身を JSON({"box_id":..,"zone":..})にする(既定は短い B001:A。マス目が粗く遠くから読める)')
    parser.add_argument("--level", choices=["L", "M", "Q", "H"], default="M",
                        help="誤り訂正レベル(既定 M。高いほど汚れに強いがマス目が増える)")
    parser.add_argument("--size", type=int, default=600, help="画像の一辺(px)")
    parser.add_argument("--out-dir", type=str, default="work/qr", help="出力先フォルダ")
    args = parser.parse_args()

    if not args.box_id and not args.zone_marker:
        parser.error("--box-id と --zone、または --zone-marker を指定してください")
    if args.box_id and len(args.zone) not in (1, len(args.box_id)):
        parser.error("--zone は1つか --box-id と同数で指定してください")
    zones = args.zone * len(args.box_id) if len(args.zone) == 1 else args.zone
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for zone in args.zone_marker:
        text = f"ZONE:{zone.upper()}"
        path = out_dir / f"ZONE_{zone.upper()}.png"
        cv2.imwrite(str(path), encode_qr(text, args.size, level=args.level))
        print(f"{path}  {text}{_hint(text, args.level)}")
    for box, zone in zip(args.box_id, zones):
        if args.json:
            text = json.dumps({"box_id": box, "zone": zone.upper()}, separators=(",", ":"))
        else:
            text = f"{box}:{zone.upper()}"
        path = out_dir / f"{box}_{zone.upper()}.png"
        cv2.imwrite(str(path), encode_qr(text, args.size, level=args.level))
        print(f"{path}  {text}{_hint(text, args.level)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
