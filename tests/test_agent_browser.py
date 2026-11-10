"""Agent 专用浏览器：ops-browser 包装层 + opsd 接入。"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from popto_ops import agent_browser as ab
from popto_ops import config as cfgmod
from popto_ops.opsd.agents import RunResult
from popto_ops.opsd.worker import Worker

S = {"cli": ["playwright-cli"], "session": "ops-agent", "profile": Path("/data/agent-browse"), "headed": False, "enabled": True}


def test_open_pins_chrome_and_profile_whatever_agent_passes():
    cmd = ab.build_argv(["open", "https://www.doubao.com/", "--profile", "/tmp/other", "--browser=firefox", "--headed",
                         "-s=mine"], S)
    assert cmd == ["playwright-cli", "-s=ops-agent", "open", "https://www.doubao.com/", "--browser", "chrome",
                   "--profile", "/data/agent-browse"]
    assert ab.build_argv(["open"], dict(S, headed=True))[-1] == "--headed"


def test_preopened_browser_open_becomes_goto_and_close_is_noop():
    """opsd 已经开好浏览器（Codex 沙箱里重开不了）：Agent 的 open 当跳转，close 不真关。"""
    assert ab.build_argv(["open", "https://www.zhihu.com/", "--headed"], S, preopened=True)[1:] == ["-s=ops-agent", "goto", "https://www.zhihu.com/"]
    assert ab.build_argv(["open"], S, preopened=True)[2:] == ["tab-list"]
    assert ab.build_argv(["close"], S, preopened=True)[2:] == ["tab-list"]
    assert ab.build_argv(["goto", "https://a.cn"], S, preopened=True)[2:] == ["goto", "https://a.cn"]
    with pytest.raises(ValueError):
        ab.build_argv(["cookie-list"], S, preopened=True)


def test_other_commands_only_get_the_fixed_session():
    assert ab.build_argv(["click", "e12"], S) == ["playwright-cli", "-s=ops-agent", "click", "e12"]
    assert ab.build_argv(["-s=x", "screenshot", "--filename=geo/a.png"], S)[1:] == ["-s=ops-agent", "screenshot", "--filename=geo/a.png"]


@pytest.mark.parametrize("command", ["cookie-list", "state-save", "localstorage-get", "request-headers", "requests", "upload",
                                     "run-code", "delete-data", "kill-all", "route", "install-browser", "attach"])
def test_login_state_and_side_effect_commands_blocked(command):
    with pytest.raises(ValueError):
        ab.build_argv([command, "x"], S)


def test_child_env_drops_agent_proxy():
    env = ab.child_env({"HTTPS_PROXY": "http://p:1", "http_proxy": "http://p:1", "PATH": "/bin"})
    assert env == {"PATH": "/bin"}


def test_settings_defaults_and_login_mark(home, data_dir):
    s = ab.settings(cfgmod.load_agents_config(home))
    assert s["profile"] == data_dir / "browser-profiles" / "agent-browse" and s["cli"] == ["true"]
    (s["profile"] / "Default").mkdir(parents=True)  # Chrome 自己建的目录不算登录过
    assert not ab.is_initialized(s)
    assert "还没有人登录过" in ab.agent_note(cfgmod.load_agents_config(home))
    (s["profile"] / ab.LOGIN_MARK).write_text("{}")
    assert ab.is_initialized(s)


def test_main_refuses_blocked_command_without_running_anything(home, capsys):
    assert ab.main(["cookie-list"]) == 2
    assert "不提供" in capsys.readouterr().err


def _lease(tools: str) -> dict:
    return {"task": {"id": "t1", "attempts": 1}, "rule": {"name": "GEO 检查", "tools": tools, "skill": "ops-geo-check"},
            "agent": "antigravity", "events": []}


def test_browser_tasks_run_one_at_a_time_and_close_browser(home, data_dir, monkeypatch):
    w = Worker(cfg=cfgmod.load_agents_config(home), client=object(), home=home)
    acfg = w.cfg["agents"]["antigravity"]
    running, peak, closed = [], [], []
    gate = threading.Event()

    def impl(lease, agent, acfg_, run_dir, alive):
        running.append(1)
        peak.append(len(running))
        gate.wait(2)
        running.pop()
        return RunResult(ok=True, output={}), None

    monkeypatch.setattr(w, "_run_agent_impl", impl)
    monkeypatch.setattr(ab, "close", lambda cfg, env=None, timeout=30: closed.append(env["OPS_RUN_DIR"]))
    monkeypatch.setattr(ab, "open_session", lambda cfg, env=None, timeout=90: None)
    ts = [threading.Thread(target=w._run_agent, args=(_lease("browser"), "antigravity", acfg, data_dir, None)) for _ in range(2)]
    for t in ts:
        t.start()
    gate.set()
    for t in ts:
        t.join(5)
    assert peak == [1, 1] and closed == [str(data_dir)] * 2
    # 非浏览器档位不加锁、不关浏览器
    w._run_agent(_lease("readonly"), "antigravity", acfg, data_dir, None)
    assert len(closed) == 2


def test_browser_note_only_for_browser_tier(home):
    w = Worker(cfg=cfgmod.load_agents_config(home), client=object(), home=home)
    a = w.cfg["agents"]
    assert w._uses_browser({"tools": "browser"}, a["antigravity"]) and w._uses_browser({"tools": "browser"}, a["codex"])
    assert not w._uses_browser({"tools": "readonly"}, a["antigravity"])
    assert not w._uses_browser({"tools": "browser"}, a["workbuddy"])
    w.cfg["agent_browser"]["enabled"] = False
    assert not w._uses_browser({"tools": "browser"}, a["antigravity"])


def test_browser_open_failure_fails_task_without_running_agent(home, data_dir, monkeypatch):
    w = Worker(cfg=cfgmod.load_agents_config(home), client=object(), home=home)
    ran, closed = [], []
    monkeypatch.setattr(w, "_run_agent_impl", lambda *a, **k: ran.append(1))
    monkeypatch.setattr(ab, "open_session", lambda cfg, env=None, timeout=90: "Chrome 没装")
    monkeypatch.setattr(ab, "close", lambda cfg, env=None, timeout=30: closed.append(1))
    res, path = w._run_agent(_lease("browser"), "antigravity", w.cfg["agents"]["antigravity"], data_dir, None)
    assert not ran and closed == [1] and path is None
    assert res.output["status"] == "failed" and "Chrome 没装" in res.output["summary"]


def test_codex_browser_tier_gets_socket_access_only_there(tmp_path):
    from popto_ops.opsd.agents import CodexAdapter, RunSpec

    def cmd(tools):
        spec = RunSpec(task_id="t", run_dir=tmp_path, cwd=tmp_path, prompt="", schema={}, schema_path=tmp_path / "s.json",
                       tools=tools, effort=None, model=None, timeout=60, agent_cfg={"bin": "codex"}, env={}, alive=None)
        return CodexAdapter().build_cmd(spec, tmp_path / "last.txt")

    assert "sandbox_workspace_write.network_access=true" in cmd("browser")
    assert "sandbox_workspace_write.network_access=true" not in cmd("workspace")
    assert "read-only" in cmd("readonly")


def test_geo_schema_separates_not_collected_from_not_mentioned(home):
    from popto_ops.validate import build_schema, validate

    schema = build_schema(home / "skills" / "ops-geo-check")
    base = {"status": "done", "summary": "采集 1/2，提到 0", "needs_decision": False, "coverage": {"planned": 2, "collected": 1}}
    ok = {"assistant": "Kimi", "question": "q", "collected": True, "mentioned": False}
    blocked = {"assistant": "豆包", "question": "q", "collected": False, "mentioned": None, "blocked_reason": "未登录"}
    assert not validate({**base, "results": [ok, blocked]}, schema)
    assert validate({**base, "results": [dict(blocked, mentioned=False)]}, schema)  # 没采到不能记成“没提到”
    assert validate({**base, "results": [{"assistant": "Kimi", "question": "q", "mentioned": False}]}, schema)
