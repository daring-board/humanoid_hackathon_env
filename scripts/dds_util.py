"""DDS(ChannelFactory)の初期化を1か所にまとめる。

どのスクリプト・フェーズからでも ensure_dds() を呼べばよい。プロセス内で何回呼んでも安全
(このモジュールのフラグ + SDK 側の ChannelFactory.Init が初期化済みなら何もしない性質)。
"""
from __future__ import annotations

from pathlib import Path

_initialized = False  # このプロセスで初期化に成功したか(テストでは False に戻してよい)


class DdsError(RuntimeError):
    """DDS を初期化できない。日本語の原因と対処を添える。"""


def ensure_dds(domain=0, interface=None) -> None:
    """ChannelFactoryInitialize を(まだなら)呼ぶ。失敗したら DdsError。"""
    global _initialized
    if _initialized:
        return
    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
    except ImportError as e:
        raise DdsError(f"unitree_sdk2py を import できません({e})。"
                       "pixi 環境(pixi run python scripts/...)で実行してください。") from e
    Path("/tmp").mkdir(exist_ok=True)  # CycloneDDS のログ出力先
    print(f"DDS を初期化: domain={domain} interface={interface}", flush=True)
    try:
        ChannelFactoryInitialize(domain, interface)
    except Exception as e:
        raise DdsError(
            f"DDS を初期化できません({type(e).__name__}: {e})。-i に渡したインターフェース名 {interface!r} を"
            "確認してください(Windows は PowerShell の Get-NetAdapter の Name 列にある、"
            "192.168.123.164 を設定したアダプター名)。") from e
    _initialized = True
