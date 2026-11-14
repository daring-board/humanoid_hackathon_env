#!/usr/bin/env python3
"""OpenCV (cv2.QRCodeEncoder) を使用してテスト用 QR コード画像を生成するスクリプト。

PC 単体での QR 読み取りテストやデモ用の画像ファイルを生成します。
"""
from pathlib import Path
import cv2
import numpy as np


def generate_qr(text: str, output_path: str | Path, scale: int = 12, margin: int = 4) -> Path:
    """QR コードを生成し、見やすいサイズに拡大して余白を付けて保存する。"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    encoder = cv2.QRCodeEncoder.create()
    qr_mat = encoder.encode(text)

    # 白黒画像を 0-255 のグレースケールに変換
    # QRCodeEncoder の出力: 0=黒(データ), 255=白(背景)
    qr_img = np.array(qr_mat, dtype=np.uint8)

    # 余白（クワイエットゾーン）を追加
    h, w = qr_img.shape
    padded = np.full((h + margin * 2, w + margin * 2), 255, dtype=np.uint8)
    padded[margin : margin + h, margin : margin + w] = qr_img

    # 拡大 (Nearest Neighbor でくっきり拡大)
    resized = cv2.resize(
        padded,
        (padded.shape[1] * scale, padded.shape[0] * scale),
        interpolation=cv2.INTER_NEAREST,
    )

    cv2.imwrite(str(output_path), resized)
    print(f"Generated QR: {text} -> {output_path} ({resized.shape[1]}x{resized.shape[0]} px)")
    return output_path


def main() -> None:
    out_dir = Path("data/test_qrs")
    out_dir.mkdir(parents=True, exist_ok=True)

    sample_payloads = [
        ("ZONE_A", out_dir / "qr_zone_a.png"),
        ("ZONE_B", out_dir / "qr_zone_b.png"),
        ('{"destination": "ZONE_C", "item_id": "ITEM-001"}', out_dir / "qr_zone_c_json.png"),
    ]

    for payload, path in sample_payloads:
        generate_qr(payload, path)

    print(f"All sample QR codes generated in {out_dir}/")


if __name__ == "__main__":
    main()
