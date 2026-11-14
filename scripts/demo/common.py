"""デモ各フェーズ共通の部品: 状態JSON・イベントログ(JSONL)・フェーズ実行枠・ロボット操作の補助。

- 状態JSON : logs/state.json(--state / 環境変数 DEMO_STATE で変更可)。フェーズ間の受け渡しはこれだけで行う
- イベントログ: logs/events.jsonl(--log / 環境変数 EVENTS_LOG で変更可)。ダッシュボードと同じファイル・同じ形式
詳細は docs/demo_scripts.md、ログの形式は docs/log_schema.md。
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = DEMO_DIR.parent
ROOT = SCRIPTS_DIR.parent
for _p in (str(SCRIPTS_DIR),):  # 既存モジュール(camera_source / recognition_link など)を import できるように
    if _p not in sys.path:
        sys.path.insert(0, _p)

LOG_PATH = Path(os.environ.get("EVENTS_LOG", ROOT / "logs" / "events.jsonl"))
STATE_PATH = Path(os.environ.get("DEMO_STATE", ROOT / "logs" / "state.json"))

# シナリオ(スプレッドシート「デモ動作シナリオ」)の Step 0〜4 に1対1で対応する
PHASES = ("approach", "read_qr", "grasp", "move", "release")
PHASE_INFO = {
    "approach": {"step": 0, "label": "Boxに近づく", "source": "control", "script": "phase0_approach.py"},
    "read_qr": {"step": 1, "label": "QRコードの読み取り", "source": "recognition", "script": "phase1_read_qr.py"},
    "grasp": {"step": 2, "label": "掴み動作の実行", "source": "control", "script": "phase2_grasp.py"},
    "move": {"step": 3, "label": "目的位置への移動(移動先の決定)", "source": "control", "script": "phase3_move.py"},
    "release": {"step": 4, "label": "Boxのリリース", "source": "control", "script": "phase4_release.py"},
}
# このフェーズを実行するために done(または skipped)になっているべきフェーズ。--force で無視できる
REQUIRES = {"approach": (), "read_qr": (), "grasp": ("read_qr",), "move": ("grasp",), "release": ("move",)}
STATUSES = ("pending", "running", "done", "skipped", "failed")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


# ---------------------------------------------------------------- 状態JSON

def new_state() -> dict:
    return {
        "v": 1,
        "run_id": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "current_phase": None,
        "box": None,  # read_qr が {"box_id","zone","scanned_at","verified"} を書く
        "destination": None,  # move が {"zone","decided_at","from"} を書く(移動先の区画)
        "phases": {p: {"status": "pending", "started_at": None, "finished_at": None,
                       "message": None, "result": None} for p in PHASES},
    }


def load_state(path: Path = STATE_PATH) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise RuntimeError(f"状態ファイル {path} を読めません({e})。壊れているなら削除してください。") from e


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    """一時ファイルに書いてから置き換える(読む側が途中の内容を見ないように)。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_iso()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def reset_state(path: Path = STATE_PATH) -> dict:
    """新しい通し実行(新しい run_id)の状態を作って保存する。"""
    state = new_state()
    save_state(state, path)
    return state


# ---------------------------------------------------------------- イベントログ

def log_event(event: str, *, run_id: str, source: str, box_id=None, zone=None, detail: str = "",
              error: dict | None = None, data: dict | None = None, path: Path = LOG_PATH) -> dict:
    """docs/log_schema.md の形式で1行追記する(receiver を通さず直接書くので id / received_at も付ける)。"""
    ts = now_iso()
    ev = {"v": 1, "id": uuid.uuid4().hex, "ts": ts, "received_at": ts, "robot": "R1", "run_id": run_id,
          "box_id": box_id, "event": event, "zone": zone, "source": source, "detail": detail}
    if error is not None:
        ev["error"] = error
    if data is not None:
        ev["data"] = data
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:  # 1行を1回の write で追記(複数プロセスから追記しても行が混ざりにくい)
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


# ---------------------------------------------------------------- フェーズ実行枠

class PhaseError(Exception):
    """フェーズの失敗。code は log_schema.md の error.code。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class PhaseSkipped(Exception):
    """フェーズを意図して飛ばす(状態は skipped になり、後続は続行できる)。"""


def add_common_args(parser) -> None:
    parser.add_argument("--state", type=str, default=str(STATE_PATH), help="状態JSONのパス")
    parser.add_argument("--log", type=str, default=str(LOG_PATH), help="イベントログ(JSONL)のパス")
    parser.add_argument("--force", action="store_true", help="前フェーズが完了していなくても実行する(単体確認用)")


def add_robot_args(parser) -> None:
    """ロボットを動かすフェーズ共通の引数。"""
    parser.add_argument("--execute", action="store_true",
                        help="実機へコマンドを送る(付けなければ dry-run: 送るはずの内容を表示するだけ)")
    parser.add_argument("-i", "--interface", type=str, default=None, help="ネットワークインターフェース(例 eth0, en7)")
    parser.add_argument("-d", "--domain", type=int, default=0, help="DDS ドメインID")


class Phase:
    """1フェーズぶんの状態更新とログ出力を引き受ける。"""

    def __init__(self, name: str, args):
        self.name, self.args = name, args
        self.info = PHASE_INFO[name]
        self.state_path = Path(args.state)
        self.log_path = Path(args.log)
        self.state: dict = {}
        self.t0 = time.monotonic()

    # --- 参照用
    @property
    def run_id(self) -> str:
        return self.state["run_id"]

    @property
    def box(self) -> dict | None:
        return self.state.get("box")

    @property
    def dry_run(self) -> bool:
        return not getattr(self.args, "execute", False)

    # --- ログ
    def log(self, event, *, detail="", error=None, data=None, box_id=None, zone=None) -> dict:
        box = self.state.get("box") or {}
        data = {"phase": self.name, **(data or {})}
        return log_event(event, run_id=self.run_id, source=self.info["source"],
                         box_id=box_id if box_id is not None else box.get("box_id"),
                         zone=zone if zone is not None else box.get("zone"),
                         detail=detail, error=error, data=data, path=self.log_path)

    def step(self, message: str, **data) -> None:
        """フェーズ内の1ステップ。画面に出し、`step` イベントとして記録する。"""
        print(f"[{self.name}] {message}", flush=True)
        self.log("step", detail=message, data=data or None)

    def copy_event(self, ev: dict) -> None:
        """認識側(Recognizer)が作ったイベントを、フェーズ名を付けてそのまま記録する。"""
        data = {"phase": self.name, **(ev.get("data") or {})}
        log_event(ev["event"], run_id=self.run_id, source=ev.get("source", self.info["source"]),
                  box_id=ev.get("box_id"), zone=ev.get("zone"), detail=ev.get("detail", ""),
                  error=ev.get("error"), data=data, path=self.log_path)

    # --- 状態
    def _set(self, status, message=None, result=None) -> None:
        ph = self.state["phases"][self.name]
        ph["status"] = status
        if message is not None:
            ph["message"] = message
        if result is not None:
            ph["result"] = result
        if status != "running":
            ph["finished_at"] = now_iso()
        save_state(self.state, self.state_path)

    def begin(self) -> None:
        state = load_state(self.state_path)
        if state is None:
            state = new_state()
        self.state = state
        if not getattr(self.args, "force", False):
            for req in REQUIRES[self.name]:
                st = state["phases"][req]["status"]
                if st not in ("done", "skipped"):
                    raise PhaseError(
                        "PREREQUISITE",
                        f"前のフェーズ {req}(Step {PHASE_INFO[req]['step']})が完了していません(状態: {st})。"
                        "先にそれを実行するか、単体確認なら --force を付けてください。")
        # このフェーズ以降は実行し直すので pending に戻す
        for later in PHASES[PHASES.index(self.name):]:
            state["phases"][later] = {"status": "pending", "started_at": None, "finished_at": None,
                                      "message": None, "result": None}
        state["phases"][self.name].update(status="running", started_at=now_iso())
        state["current_phase"] = self.name
        save_state(state, self.state_path)
        mode = "dry-run" if "execute" in vars(self.args) and self.dry_run else "実行"
        print(f"=== Step {self.info['step']}: {self.info['label']} ({self.name}, run_id={self.run_id}, {mode}) ===",
              flush=True)
        self.log("phase_start", detail=f"Step {self.info['step']} 開始: {self.info['label']}",
                 data={"step": self.info["step"], "dry_run": self.dry_run if "execute" in vars(self.args) else None})

    def end(self, status: str, message: str, result: dict | None = None, error: dict | None = None) -> None:
        elapsed = int((time.monotonic() - self.t0) * 1000)
        self._set(status, message, result)
        self.log("phase_end", detail=f"Step {self.info['step']} {status}: {message}", error=error,
                 data={"status": status, "duration_ms": elapsed, **({"result": result} if result else {})})
        print(f"=== {self.name}: {status} - {message} ===", flush=True)


def run_phase(name: str, args, body) -> int:
    """body(phase) を実行し、状態とログを更新して終了コードを返す(0=完了/スキップ, 1=失敗, 130=中断)。
    body は結果の辞書(状態JSONの result に入る)を返す。"""
    ph = Phase(name, args)
    try:
        ph.begin()
        result = body(ph)
        ph.end("done", "完了", result)
        return 0
    except PhaseSkipped as e:
        ph.end("skipped", str(e) or "スキップ")
        return 0
    except PhaseError as e:
        _fail(ph, e.code, e.message)
        return 1
    except KeyboardInterrupt:
        _fail(ph, "INTERRUPTED", "Ctrl+C で中断された")
        return 130
    except Exception as e:  # 想定外の例外も必ず状態とログに残す
        _fail(ph, "UNEXPECTED", f"{type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1


def _fail(ph: Phase, code: str, message: str) -> None:
    print(f"[エラー] {code}: {message}", file=sys.stderr, flush=True)
    if not ph.state:  # begin() が状態を読む前に失敗した
        ph.state = load_state(ph.state_path) or new_state()
    err = {"code": code, "message": message}
    ph.log("error", detail=message, error=err)
    ph.end("failed", message, error=err)


def setup_console() -> None:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")


# ---------------------------------------------------------------- 腕の操作(掴む・離す)
# scripts/arm_controller.py の ArmController(pick_move.py と同じ)を使う。
# フェーズは別プロセスなので、プロセスをまたいで保持できない sdk モードは使えない(preset / custom / none のみ)。

ARM_MODES = ("preset", "custom", "none")


def add_arm_args(parser, default_custom: str) -> None:
    """腕制御の引数。default_custom は custom モードで再生する動作名の既定(掴む/離すのどちらか)。"""
    parser.add_argument("--arm-mode", choices=ARM_MODES, default="preset",
                        help="腕制御: preset(構え15→挟む17 / 解放99), custom(ティーチング済み動作), none(腕を動かさない)")
    parser.add_argument("--custom-grab", type=str, default="grasp_box",
                        help="custom モードの把持動作名(ロボット側に事前登録が必要)")
    parser.add_argument("--custom-release", type=str, default="release_box",
                        help="custom モードの解放動作名(再生後に ReleaseArm も送る。release_arm なら ReleaseArm のみ)")
    parser.add_argument("--custom-wait", type=float, default=3.0,
                        help="custom モードで動作の完了とみなすまで待つ秒数")


def make_arm(args):
    """ArmController を作る(--execute のときだけ DDS を初期化して実機クライアントを開く)。"""
    if args.execute and args.arm_mode != "none":
        try:
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        except ImportError as e:
            raise PhaseError("ROBOT_FAULT", f"unitree_sdk2py を import できません({e})。"
                                            "pixi 環境(pixi run python scripts/demo/...)で実行してください。") from e
        Path("/tmp").mkdir(exist_ok=True)  # CycloneDDS のログ出力先
        ChannelFactoryInitialize(args.domain, args.interface)
    elif not args.execute:
        print("[dry-run] ロボットにはコマンドを送りません(実機へ送るには --execute)。", flush=True)
    try:
        from arm_controller import ArmController
        return ArmController(mode=args.arm_mode, execute=args.execute, custom_grab=args.custom_grab,
                             custom_release=args.custom_release, custom_wait=args.custom_wait)
    except ImportError as e:
        raise PhaseError("ROBOT_FAULT", f"腕制御を初期化できません({e})") from e


def confirm_execute(args, what: str) -> None:
    """--execute のときだけ、安全の注意を出して3秒待つ。"""
    if args.execute:
        print(f"実機の{what}を動かします。周囲を空け、停止担当(コントローラー)を置いてください。"
              "3秒後に開始(やめるなら Ctrl+C)。", flush=True)
        time.sleep(3.0)
