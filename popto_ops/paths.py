"""仓库目录与数据目录。数据目录不进 git（见设计 01 §5）。"""
from __future__ import annotations

import os
from pathlib import Path


def ops_home() -> Path:
    """代码仓库根目录（rules.json、agents.yaml、skills/ 所在处）。"""
    env = os.environ.get("OPS_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def ops_data() -> Path:
    """数据目录：hub.db、ops.db、runs/、日志、登录态。"""
    return Path(os.environ.get("OPS_DATA", "~/popto-ops-data")).expanduser()


def ensure_data_dirs() -> Path:
    root = ops_data()
    for sub in ("runs", "media", "exports", "logs", "browser-profiles",
                "workbuddy-inbox", "workbuddy-outbox", "notify", "dashboard"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def expand(p: str | os.PathLike | None) -> Path | None:
    if p is None:
        return None
    return Path(os.path.expandvars(str(p))).expanduser()
