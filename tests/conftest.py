from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from popto_ops import timeutil  # noqa: E402
from popto_ops.hub.core import Hub  # noqa: E402
from popto_ops.notifier import FileNotifier  # noqa: E402

COPY = ["agents.yaml", "rules.json", "schedules.yaml", "decisions.md", "not-doing.md", "skills", "listening", "dashboards"]


@pytest.fixture(autouse=True)
def _no_keychain_in_redact(monkeypatch):
    """打码默认会读钥匙串里的真密钥（popto_ops.redact）；测试里一律不碰钥匙串。"""
    from popto_ops import redact

    monkeypatch.setattr(redact, "_known_secrets", lambda: ())


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "ops"
    h.mkdir()
    for name in COPY:
        src = REPO / name
        if src.is_dir():
            shutil.copytree(src, h / name)
        else:
            shutil.copy2(src, h / name)
    data = tmp_path / "data"
    data.mkdir()
    (data / "local.yaml").write_text(yaml.safe_dump({
        "external_skill_dirs": [str((REPO.parent / "skills").resolve())],
        "notifier": {"adapter": "file"},
        "proxy": None,
        "agent_browser": {"cli": ["true"], "profile": str(data / "browser-profiles" / "agent-browse")},  # 测试里不真开浏览器
    }, allow_unicode=True))
    monkeypatch.setenv("OPS_HOME", str(h))
    monkeypatch.setenv("OPS_DATA", str(data))
    yield h
    timeutil.set_fake_now(None)


@pytest.fixture()
def data_dir(home) -> Path:
    import os

    return Path(os.environ["OPS_DATA"])


@pytest.fixture()
def notify_dir(data_dir) -> Path:
    return data_dir / "notify" / "outbox"


@pytest.fixture()
def hub(home, notify_dir) -> Hub:
    return Hub(home=home, notifier=FileNotifier(notify_dir))


def at(s: str) -> datetime:
    """冻结到某个上海时间，例如 at('2026-11-10 10:00')。"""
    dt = datetime.fromisoformat(s).replace(tzinfo=timeutil.TZ)
    timeutil.set_fake_now(dt)
    return dt


def ok_output(summary="完成", **kw) -> dict:
    out = {"status": "done", "summary": summary, "needs_decision": False, "decision_card": None,
           "follow_up_events": [], "dogfood_friction": []}
    out.update(kw)
    return out
