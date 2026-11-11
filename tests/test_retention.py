"""打码（Cookie / token / 手机号不进日志和输出）+ 数据保留期清理（08 §6 / §7）。"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from datetime import timedelta
from pathlib import Path

import pytest

from popto_ops import agent_browser as ab
from popto_ops import config as cfgmod
from popto_ops import db, redact, retention
from popto_ops.opsd.agents import RunResult
from popto_ops.opsd.worker import Worker
from popto_ops.timeutil import iso, now
from popto_ops.validate import build_schema

LEAKS = [
    ('{"HOST":"channels.weixin.qq.com","cookie":"sessionid=abc123def456; wxuin=1"}', "abc123def456"),
    ("Cookie: sid_tt=abcdef1234567890; uid=1", "abcdef1234567890"),
    ('"set-cookie": ["web_session=0400697f4a2b77; path=/"]', "0400697f4a2b77"),
    ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz"),
    ("curl -H 'x-api-key: k_live_123456789'", "k_live_123456789"),
    ('[{"name": "a1", "value": "18f1c2d3e4f5a6b7", "domain": ".xiaohongshu.com"}]', "18f1c2d3e4f5a6b7"),
    ("https://x.com/cb?access_token=abcdefgh12345678&q=1", "abcdefgh12345678"),
    ("http://user:pa55word@proxy.example:3128", "pa55word"),
    ("token glpat-abcdefghijklmnopqrstuv", "glpat-abcdefghijklmnopqrstuv"),
    ("key sk-ant-abcdefghijklmnopqrstuvwxyz", "sk-ant-abcdefghijklmnopqrstuvwxyz"),
    ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop", "eyJhbGciOiJIUzI1NiJ9"),
    ("想要资料加我 13812345678", "13812345678"),
    ("电话：+86 13912345678。", "13912345678"),
]


@pytest.mark.parametrize("text,secret", LEAKS)
def test_redact_masks_cookies_tokens_and_phones(text, secret):
    out, n = redact.redact(text, secrets=())
    assert secret not in out and n >= 1
    assert redact.redact(out, secrets=()) == (out, 0)  # 打过码的再扫一遍不变


@pytest.mark.parametrize("text", [
    "Cookie 已过期，请重新登录",
    "第一步：打开抖音创作者中心",
    "作品 7680223353274362362 播放 13，时间戳 1731312345678",
    "https://www.xiaohongshu.com/user/profile/668b9b13812345678f035a7f",
    "page_token=abcdefgh12345678",
    "https://weixin.sogou.com/link?url=abc&token=0123456789abcdef",  # 搜索结果链接里的防爬参数，打了码就打不开
    'www_authenticate_header: "Bearer resource_metadata=\\"https://mcp.postman.com/x\\""',
    '{"session_id": "0b3c9f1e-2c4d-4e5f-8a9b-0c1d2e3f4a5b", "input_tokens": 12345678901}',
])
def test_redact_leaves_normal_text_alone(text):
    assert redact.redact(text, secrets=()) == (text, 0)


def test_known_secret_values_masked_anywhere():
    out, n = redact.redact("TikHub key=XyZ-secret-value-123 已配置", secrets=("XyZ-secret-value-123",))
    assert "XyZ-secret-value-123" not in out and n == 1


def test_redact_obj_walks_nested_output():
    out, n = redact.redact_obj({"summary": "Cookie: sessionid=abcdef123456", "items": [{"note": "电话 13812345678"}, 3],
                                "ok": True})
    assert n == 2 and out["ok"] is True and out["items"][1] == 3
    assert "abcdef123456" not in json.dumps(out) and "13812345678" not in json.dumps(out)


def test_scrub_file_keeps_mode_and_mtime_and_skips_binary(tmp_path):
    p = tmp_path / "console.log"
    p.write_text('[LOG] {"cookie":"sessionid=abcdef123456"}', encoding="utf-8")
    os.chmod(p, 0o600)
    os.utime(p, (1_700_000_000, 1_700_000_000))
    assert redact.scrub_file(p, secrets=(), dry_run=True) == 1 and "abcdef123456" in p.read_text()
    assert redact.scrub_file(p, secrets=()) == 1
    assert "abcdef123456" not in p.read_text() and "<已打码>" in p.read_text()
    assert p.stat().st_mtime == 1_700_000_000 and stat.S_IMODE(p.stat().st_mode) == 0o600
    binary = tmp_path / "dump.log"
    binary.write_bytes(b"\x00\x01cookie: sessionid=abcdef123456")
    png = tmp_path / "shot.png"
    png.write_bytes(b"cookie: sessionid=abcdef123456")
    assert redact.scrub_file(binary, secrets=()) == 0 and redact.scrub_file(png, secrets=()) == 0


def test_scrub_tree_marks_and_notices_later_files(tmp_path):
    d = tmp_path / "run"
    (d / ".playwright-cli").mkdir(parents=True)
    (d / ".playwright-cli" / "page.yml").write_text("- /url: https://x/?sessionid=abcdef123456")
    (d / "notes.md").write_text("没有敏感信息")
    assert redact.needs_scrub(d)
    assert redact.scrub_tree(d, secrets=()) == {"files": 1, "hits": 1}
    assert not redact.needs_scrub(d)
    later = d / "attempt-2.log"
    later.write_text("重试")
    os.utime(later, (time.time() + 5, time.time() + 5))
    assert redact.needs_scrub(d)


# ---------------------------------------------------------------- opsd
class _Client:
    base = "http://hub"

    def __init__(self):
        self.bodies = []

    def renew(self, tid, worker):
        return True

    def result(self, tid, body):
        self.bodies.append(body)
        return {"state": "done"}


def _lease(tid="t_leak"):
    return {"task": {"id": tid, "attempts": 1}, "rule": {"name": "社媒舆情监听"}, "agent": "claude", "events": []}


def test_worker_scrubs_run_dir_and_redacts_result_before_posting(home, data_dir, monkeypatch):
    c = _Client()
    w = Worker(cfg=cfgmod.load_agents_config(home), client=c, home=home)

    def fake_run(lease, agent, acfg, run_dir, alive):
        (run_dir / ".playwright-cli").mkdir()
        (run_dir / ".playwright-cli" / "console-1.log").write_text('{"header":{"cookie":"sessionid=abcdef123456"}}')
        return RunResult(ok=True, output={"status": "done", "summary": "请求头 Cookie: sessionid=abcdef123456",
                                          "leads": [{"why": "留了电话 13812345678"}]}), None

    monkeypatch.setattr(w, "_run_agent", fake_run)
    w.execute(_lease())
    posted = json.dumps(c.bodies[0], ensure_ascii=False)
    assert "abcdef123456" not in posted and "13812345678" not in posted and "<已打码>" in posted
    run_dir = data_dir / "runs" / "t_leak"
    assert "abcdef123456" not in (run_dir / ".playwright-cli" / "console-1.log").read_text()
    assert (run_dir / redact.MARKER).exists()

    def fake_fail(lease, agent, acfg, run_dir, alive):
        return RunResult(ok=False, error_text="401 Authorization: Bearer abcdefghijklmnopqrstu"), None

    monkeypatch.setattr(w, "_run_agent", fake_fail)
    w.execute(_lease("t_fail"))
    assert "abcdefghijklmnopqrstu" not in c.bodies[1]["error_detail"]


def test_finalize_redacts_before_output_json_and_ingest(home, data_dir):
    w = Worker(cfg=cfgmod.load_agents_config(home), client=_Client(), home=home)
    run_dir = data_dir / "runs" / "t_fin"
    run_dir.mkdir(parents=True)
    res, path = w._finalize(RunResult(ok=True, output={"status": "done", "summary": "token sk-ant-abcdefghijklmnopqrstuvwxyz",
                                                        "needs_decision": False}), build_schema(None), run_dir)
    assert res.ok and "sk-ant-" not in res.output["summary"] and "sk-ant-" not in path.read_text()


# ---------------------------------------------------------------- 专用浏览器
def test_ops_browser_redacts_output_and_snapshots(home, data_dir, monkeypatch, capsys):
    run_dir = data_dir / "runs" / "t_b"
    run_dir.mkdir(parents=True)
    monkeypatch.setenv("OPS_RUN_DIR", str(run_dir))

    def fake_run(cmd, env=None, cwd=None, check=False, capture_output=False):
        d = Path(cwd) / ".playwright-cli"
        d.mkdir(exist_ok=True)
        (d / "console-1.log").write_text('[LOG] {"header":{"cookie":"sessionid=abcdef123456"}}')
        out = b"- [Console](.playwright-cli/console-1.log)\ncookie: sessionid=abcdef123456\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr=b"")

    monkeypatch.setattr(ab.subprocess, "run", fake_run)
    assert ab.main(["console"]) == 0
    out = capsys.readouterr().out
    assert "abcdef123456" not in out and "<已打码>" in out and "console-1.log" in out
    assert "abcdef123456" not in (run_dir / ".playwright-cli" / "console-1.log").read_text()


@pytest.mark.parametrize("expr", ["document.cookie", "JSON.stringify(localStorage)", "window.sessionStorage.length",
                                  "indexedDB.databases()"])
def test_ops_browser_eval_cannot_read_login_state(home, expr, capsys):
    assert ab.main(["eval", expr]) == 2
    assert "Cookie" in capsys.readouterr().err


def test_ops_browser_eval_innertext_still_allowed():
    assert ab.blocked_reason("eval", ["document.body.innerText.slice(0, 4000)"]) is None


# ---------------------------------------------------------------- 保留期清理
def _seed_runs(data_dir: Path) -> dict:
    hub, ops = db.open_hub(), db.open_ops()
    old = data_dir / "runs" / "t_20250101_000000_aaaaaa"
    (old / "scripts" / "c1").mkdir(parents=True)
    (old / "scripts" / "c1" / "douyin.md").write_text("脚本正文")
    (old / "pack" / "c1").mkdir(parents=True)
    (old / "pack" / "c1" / "meta.json").write_text("{}")
    (old / "input.md").write_text("评论原文：生日快乐")
    busy = data_dir / "runs" / "t_20250101_000001_bbbbbb"
    busy.mkdir()
    new = data_dir / "runs" / f"t_{now().strftime('%Y%m%d_%H%M%S')}_cccccc"
    new.mkdir()
    for tid, state in ((old.name, "done"), (busy.name, "waiting_decision"), (new.name, "done")):
        hub.execute("INSERT INTO tasks(id, rule, state, created_at, updated_at) VALUES (?,?,?,?,?)",
                    (tid, "r", state, iso(), iso()))
    ops.execute("INSERT INTO contents(id, status, script_path) VALUES ('c1', 'scripted', ?)",
                (json.dumps([f"runs/{old.name}/scripts/c1/douyin.md"]),))
    for pid, platform in (("pk1", "douyin"), ("pk2", "xiaohongshu")):
        ops.execute("INSERT INTO publish_packs(id, content_id, platform, pack_dir, status) VALUES (?, 'c1', ?, ?, 'published')",
                    (pid, platform, str(old / "pack" / "c1")))
    return {"old": old, "busy": busy, "new": new, "hub": hub, "ops": ops}


def test_retention_archives_referenced_files_then_deletes_old_runs(home, data_dir):
    s = _seed_runs(data_dir)
    old, ops = s["old"], s["ops"]
    dry = retention.run(dry_run=True, cfg={})
    assert dry["summary"].startswith("（试运行") and old.exists() and dry["details"]["runs"]["deleted"] == 1
    assert json.loads(ops.execute("SELECT script_path FROM contents").fetchone()[0])[0].startswith("runs/")

    r = retention.run(cfg={})
    assert not old.exists() and s["busy"].exists() and s["new"].exists()
    arch = data_dir / "content" / "_archived_runs" / old.name
    assert (arch / "scripts" / "c1" / "douyin.md").read_text() == "脚本正文"
    assert (arch / "pack" / "c1" / "meta.json").exists() and not (arch / "input.md").exists()
    assert json.loads(ops.execute("SELECT script_path FROM contents").fetchone()[0]) == \
        [f"content/_archived_runs/{old.name}/scripts/c1/douyin.md"]
    assert {r_[0] for r_ in ops.execute("SELECT pack_dir FROM publish_packs")} == {str(arch / "pack" / "c1")}
    assert "删除 90 天前的运行目录 1 个" in r["summary"] and "2 份文件" in r["summary"] and "1 个任务还没结束" in r["summary"]


def test_retention_blanks_old_text_but_keeps_metadata(home, data_dir):
    hub, ops = db.open_hub(), db.open_ops()
    old, fresh = iso(now() - timedelta(days=200)), iso()
    hub.execute("INSERT INTO tasks(id, rule, state, created_at, updated_at, summary, output) VALUES"
                " ('t_old', '评论分拣', 'done', ?, ?, '「生日快乐」', '{}'), ('t_new', '评论分拣', 'done', ?, ?, '新的', '{}')",
                (old, old, fresh, fresh))
    hub.execute("INSERT INTO events(id, source, type, summary, data, received_at) VALUES"
                " ('e_old', 'douyin', 'social.comment.batch', '新增 1 条评论', '{}', ?)", (old,))
    hub.execute("INSERT INTO outbox(kind, title, body, created_at, status) VALUES ('message', '日报', '评论：生日快乐', ?, 'sent'),"
                " ('message', '待发', '还没发', ?, 'pending')", (old, old))
    hub.execute("INSERT INTO cards(id, seq, kind, title, payload, created_at, status, decided_by, choice) VALUES"
                " ('cd1', 1, 'comment_reply', '回复评论', '{}', ?, 'decided', '邬勇', 'approve')", (old,))
    ops.execute("INSERT INTO signals(id, source_type, quote, kind, created_at) VALUES ('s1', 'comment', '原话', 'ask', ?)", (old,))
    ops.execute("INSERT INTO comments(id, post_id, text, purge_after) VALUES ('cm1', 'p', '原文', ?)",
                (iso(now() - timedelta(hours=1)),))
    r = retention.run(cfg={})
    assert tuple(hub.execute("SELECT summary, output, state, rule FROM tasks WHERE id='t_old'").fetchone()) == \
        (None, None, "done", "评论分拣")
    assert hub.execute("SELECT summary FROM tasks WHERE id='t_new'").fetchone()[0] == "新的"
    assert tuple(hub.execute("SELECT summary, data, type FROM events").fetchone()) == (None, None, "social.comment.batch")
    assert [tuple(x) for x in hub.execute("SELECT body, status FROM outbox ORDER BY id")] == [(None, "sent"), ("还没发", "pending")]
    assert tuple(hub.execute("SELECT title, payload, decided_by, choice FROM cards").fetchone()) == (None, None, "邬勇", "approve")
    assert tuple(ops.execute("SELECT quote, kind FROM signals").fetchone()) == (None, "ask")
    assert ops.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 0
    assert "hub.db 清空 170 天前的正文" in r["summary"] and "过期评论 1 条" in r["summary"]


def test_retention_rescrubs_unmarked_runs_but_not_running_ones(home, data_dir):
    hub = db.open_hub()
    done_dir = data_dir / "runs" / "t_20261111_000000_dddddd"
    running = data_dir / "runs" / "t_20261111_000001_eeeeee"
    for d, state in ((done_dir, "done"), (running, "leased")):
        d.mkdir(parents=True)
        (d / "attempt-1-codex.stderr").write_text("Authorization: Bearer abcdefghijklmnopqrstu")
        hub.execute("INSERT INTO tasks(id, rule, state, created_at, updated_at) VALUES (?, 'r', ?, ?, ?)",
                    (d.name, state, iso(), iso()))
    r = retention.run(cfg={})
    assert "abcdefghijklmnopqrstu" not in (done_dir / "attempt-1-codex.stderr").read_text()
    assert (done_dir / redact.MARKER).exists()
    assert "abcdefghijklmnopqrstu" in (running / "attempt-1-codex.stderr").read_text()  # Agent 还在写，不碰
    assert "补打码：1 个文件 1 处" in r["summary"]


def test_rotate_logs_copies_truncates_and_scrubs_old_copy(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    big = logs / "dingtalk-events.err.log"
    big.write_text("x" * 2048 + "\ncookie: sessionid=abcdef123456\n")
    (logs / "dingtalk-events.err.log.1").write_text("上一份")
    small = logs / "hub.err.log"
    small.write_text("小")
    assert retention.rotate_logs([logs], 1024, 2, dry_run=False) == ["logs/dingtalk-events.err.log"]
    assert big.stat().st_size == 0 and small.read_text() == "小"
    first = (logs / "dingtalk-events.err.log.1").read_text()
    assert first.startswith("x") and "abcdef123456" not in first
    assert (logs / "dingtalk-events.err.log.2").read_text() == "上一份"


def test_retention_days_capped_so_comment_copies_go_within_180_days():
    s = retention.settings({"retention": {"runs_days": 365, "text_days": 400, "log_max_mb": "x"}})
    assert s["runs_days"] == 170 and s["text_days"] == 170 and s["log_max_mb"] == 20
    assert retention.settings({}) == {"enabled": True, "runs_days": 90, "text_days": 170, "log_max_mb": 20.0, "log_keep": 2}


def test_retention_rule_and_schedule_registered(home):
    rules = json.loads((home / "rules.json").read_text(encoding="utf-8"))
    rule = next(r for r in rules if r.get("name") == "数据保留清理")
    assert rule["when"]["types"] == ["cron.data_retention"] and rule["command"][-2:] == ["popto_ops.retention", "run"]
    assert "cron.data_retention" in (home / "schedules.yaml").read_text(encoding="utf-8")
