#!/usr/bin/env python3
import argparse
from pathlib import Path
from unitree_sdk2py.core.channel import ChannelFactoryInitialize


def main() -> None:
    # ネットワークインターフェース指定用の引数パーサーを設定
    parser = argparse.ArgumentParser(description="Unitree SDK2 program template")
    parser.add_argument("-i", "--interface", type=str, default=None, help="Network interface")
    args = parser.parse_args()

    # Windows 環境等での CycloneDDS ログ出力先ディレクトリを確保（権限エラー対策）
    try:
        Path("/tmp").mkdir(exist_ok=True)
    except OSError:
        pass

    # DDS チャンネル通信を初期化（ドメインID: 0，インターフェース指定）
    ChannelFactoryInitialize(0, args.interface)

    print("DDS initialized successfully.")
    # ここに任意のロボット制御コードやクライアント呼び出しを記述します


if __name__ == "__main__":
    main()
