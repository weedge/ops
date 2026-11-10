import json

from popto_ops import db, ingest
from popto_ops.ci import gate
from popto_ops.collectors import metrics
from popto_ops.notifier.listener import handle_event, parse_command
from popto_ops.notifier.render import render_card
from popto_ops.opsd import breaker

from conftest import at


def test_parse_commands():
    assert parse_command("C12 1")["data"] == {"card": "C12", "choice": "1", "reason": None}
    assert parse_command("批 c7 reject 这周没人手")["data"]["reason"] == "这周没人手"
    assert parse_command("@兔管家 C3 approve_all")["data"]["choice"] == "approve_all"
    assert parse_command("记下来：视频号先不做自动发布")["type"] == "decision.note"
    assert parse_command("需求: 批量口播要支持横屏")["type"] == "demand.manual"
    assert parse_command("已发 pk_c1_douyin https://v.douyin.com/abc/")["data"]["pack_id"] == "pk_c1_douyin"
    assert parse_command("暂停 douyin")["type"] == "_pause"
    assert parse_command("今天天气不错") is None


class FakeHub:
    def __init__(self):
        self.events, self.posts = [], []

    def emit(self, e):
        self.events.append(e)
        return {"ok": True}

    def post(self, path, body=None, retries=0):
        self.posts.append((path, body))
        return {"ok": True}

    def notify(self, *a, **k):
        return {"ok": True}


def test_listener_only_accepts_approvers():
    raw = {"event_type": "user_im_message_receive_group", "event_id": "e1",
           "data": '{"senderStaffId": "u1", "text": {"content": "C5 1"}}'}
    h = FakeHub()
    assert handle_event(raw, h, approvers=["someone_else"]) is None and not h.events
    handle_event(raw, h, approvers=["u1"])
    assert h.events[0]["type"] == "decision.made" and h.events[0]["data"]["by"] == "u1"
    assert h.events[0]["dedup_key"] == "dingtalk:e1"


def test_listener_card_callback():
    raw = {"event_type": "user_card_action_triggered", "payload": {"body": {
        "operatorUserId": "u1", "actionData": {"context": {"card_id": "card_20261110_0003", "choice": "approve"}}}}}
    h = FakeHub()
    handle_event(raw, h, approvers=["u1"])
    assert h.events[0]["data"]["card"] == "card_20261110_0003" and h.events[0]["data"]["choice"] == "approve"


def test_gate_checks():
    changes = [
        {"new_path": "db/migrations/0012.sql", "diff": "+ALTER TABLE users DROP COLUMN phone;\n"},
        {"new_path": "src/billing/credits.go", "diff": "+x := 1\n"},
        {"new_path": "src/config.ts", "diff": "+const key = 'sk-abcdefghijklmnopqrstuvwxyz123456'\n"},
    ]
    g = gate.check(changes)
    assert any("迁移" in x for x in g.needs_human) and any("计费" in x for x in g.needs_human)
    assert any("API Key" in x for x in g.blockers) and not g.auto_merge_ok
    assert gate.check([{"new_path": "src/a.ts", "diff": "+const a = 1\n"}]).auto_merge_ok
    big = [{"new_path": "src/a.ts", "diff": "+x\n" * 801}]
    assert not gate.check(big).auto_merge_ok


def test_breaker_classify():
    cb = {"quota_patterns": ["usage limit"], "auth_patterns": ["401"], "network_patterns": ["econnrefused"]}
    assert breaker.classify("You've hit your usage limit", timed_out=False, cb=cb) == "quota"
    assert breaker.classify("HTTP 401", timed_out=False, cb=cb) == "auth"
    assert breaker.classify("connect ECONNREFUSED", timed_out=False, cb=cb) == "network"
    assert breaker.classify("", timed_out=True, cb=cb) == "timeout"
    at("2026-11-10 10:00")
    assert breaker.parse_retry_at("try again in 2 hours").startswith("2026-11-10T12:00")


def test_render_card():
    title, body = render_card({"kind": "publish_batch", "title": "今晚 6 条", "proposal": "全部发布",
                               "options": [{"key": "a", "label": "全部发布"}, {"key": "b", "label": "全部不发", "require_reason": True}],
                               "on_expire": "b"}, 17, "2026-11-10T22:00:00+08:00")
    assert "C17" in title and "【待发布】" in title and "11-10 22:00 前未处理则「全部不发」" in body


def test_metrics_plan_and_anomaly(home):
    ops = db.open_ops()
    at("2026-11-10 10:00")
    for i in range(6):
        pid = metrics.register_post(ops, {"platform": "douyin", "platform_post_id": str(i), "title": f"t{i}",
                                          "published_at": "2026-11-08T10:00:00+08:00"})
        metrics.save_snapshot(ops, {"id": pid, "platform": "douyin", "title": f"t{i}"}, "24h", {"views": 1000}, "test")
    pid = metrics.register_post(ops, {"platform": "douyin", "platform_post_id": "hot", "title": "爆了"})
    evs = metrics.save_snapshot(ops, {"id": pid, "platform": "douyin", "title": "爆了"}, "24h", {"views": 5000}, "test")
    assert any(e["type"] == "social.metrics.anomaly" and "高于" in e["summary"] for e in evs)
    jobs = ops.execute("SELECT checkpoint, due_at FROM metric_jobs WHERE post_id=? ORDER BY due_at", (pid,)).fetchall()
    assert [j[0] for j in jobs] == ["1h", "24h", "72h", "7d"]


def test_ingest_comment_and_demand(home):
    ops = db.open_ops()
    ops.execute("INSERT INTO comments(id,post_id,text) VALUES ('douyin_1:9','douyin_1','能不能横屏')")
    lease = {"task": {"id": "t1"}}
    ingest.apply("ops-comment-triage", {"comments": [{"id": "9", "category": "demand", "sentiment": "neu", "reply_draft": "在做了"}],
                                        "signals": [{"quote": "能不能横屏", "kind": "功能需求", "module": "批量口播", "intensity": 2}]}, lease)
    row = ops.execute("SELECT category, reply_status FROM comments").fetchone()
    assert tuple(row) == ("demand", "drafted")
    ingest.apply("ops-demand-mining", {"assignments": [{"quote": "能不能横屏", "kind": "功能需求", "module": "批量口播",
                                                        "intensity": 2, "new_demand": {"title": "批量口播支持横屏", "size": "S"}}]}, lease)
    d = ops.execute("SELECT title, signal_count FROM demands").fetchone()
    assert d[0] == "批量口播支持横屏" and d[1] >= 1


def test_sync_terms(home):
    ops = db.open_ops()
    assert ingest.sync_terms(ops) > 5
    assert ingest.sync_terms(ops) == 0


def _spec(tmp_path, agent_cfg, tools):
    from popto_ops.opsd.agents import RunSpec
    return RunSpec(task_id="t", run_dir=tmp_path, cwd=tmp_path, prompt="p", schema={}, schema_path=tmp_path / "s.json",
                   tools=tools, effort=None, model=None, timeout=60, agent_cfg=agent_cfg,
                   env={"HTTPS_PROXY": "http://global:1"})


def test_wrapper_and_strict_tiers(tmp_path):
    from popto_ops.opsd.agents import AntigravityAdapter, ClaudeAdapter, wrapper_proxy
    sh = tmp_path / "agy.sh"
    sh.write_text('#!/usr/bin/env bash\nPROXY="http://10.0.0.9:3218"\nexport HTTPS_PROXY="$PROXY"\n'
                  'exec agy --dangerously-skip-permissions "$@"\n')
    assert wrapper_proxy(str(sh)) == "http://10.0.0.9:3218"
    agy = {"type": "antigravity", "bin": str(sh), "strict_bin": "/opt/agy", "strict_tiers": ["readonly"]}
    a = AntigravityAdapter()
    ro = _spec(tmp_path, agy, "readonly")
    cmd = a.build_cmd(ro)
    assert cmd[0] == "/opt/agy" and "--sandbox" in cmd and "--dangerously-skip-permissions" not in cmd
    assert a.resolve_env(ro)["HTTPS_PROXY"] == "http://10.0.0.9:3218"
    ws = _spec(tmp_path, agy, "workspace")
    cmd = a.build_cmd(ws)
    assert cmd[0] == str(sh) and "--dangerously-skip-permissions" not in cmd  # 包装脚本自己带
    assert a.resolve_env(ws)["HTTPS_PROXY"] == "http://global:1"
    cl = {"type": "claude", "bin": "~/claude.sh", "strict_bin": "/opt/claude", "strict_tiers": []}
    cmd = ClaudeAdapter().build_cmd(_spec(tmp_path, cl, "workspace"))
    assert cmd[0].endswith("claude.sh") and "Bash(security:*)" in cmd[cmd.index("--disallowedTools") + 1]


def test_strict_tiers_validated(home):
    import yaml
    from popto_ops import config
    p = home / "agents.yaml"
    cfg = yaml.safe_load(p.read_text())
    cfg["agents"]["claude"]["strict_tiers"] = ["root"]
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True))
    assert any("strict_tiers" in e for e in config.check_config(home).errors)


def test_codex_reconnect_notices_not_fatal(tmp_path):
    import json as _j
    from popto_ops.opsd.agents import CodexAdapter
    out = {"status": "done", "summary": "ok", "needs_decision": False}
    fake = tmp_path / "codex.sh"
    def run(events, rc=0):
        fake.write_text(fake.read_text().split("for e in")[0] + f"for e in {events!r}: print(json.dumps(e))\nsys.exit({rc})\n")
        fake.chmod(0o755)
        return CodexAdapter().run(_spec(tmp_path, {"type": "codex", "bin": str(fake)}, "readonly"), tmp_path / "a.log")
    fake.write_text("#!/usr/bin/env python3\nimport sys, json\na = sys.argv\n"
                    f"open(a[a.index('-o') + 1], 'w').write({_j.dumps(out)!r})\n")
    r = run([{"type": "thread.started", "thread_id": "th1"}, {"type": "error", "message": "Reconnecting... 4/5"},
             {"type": "turn.completed", "usage": {}}])
    assert r.ok and r.output == out and r.session_id == "th1"
    r = run([{"type": "error", "message": "You've hit your usage limit"}])
    assert not r.ok and "usage limit" in r.error_text
    r = run([{"type": "turn.failed", "error": {"message": "boom"}}, {"type": "turn.completed"}])
    assert not r.ok


def test_dingtalk_message_id_and_failure(monkeypatch):
    import subprocess as sp
    from popto_ops.notifier import DingTalkNotifier
    n = DingTalkNotifier({"chat_id": "cid1"})
    out = {"stdout": '{"success": true, "result": {"data": {"openMsgId": "msgX"}}}'}
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 0, out["stdout"], ""))
    assert n.send("notice", "t", "b") == {"ok": True, "message_id": "msgX"}
    out["stdout"] = '{"success": false, "errorMsg": "no permission"}'
    assert n.send("notice", "t", "b")["ok"] is False


def test_listener_flatten_group_message():
    # dws event consume --flatten 的真实字段形状（ID 已替换）
    raw = {"type": "user_im_message_receive_group", "event_id": "ev1", "message_id": "msg1",
           "conversation_id": "cid1", "sender": "审批人", "sender_open_dingtalk_id": "ODT_APPROVER",
           "content": "C5 2 先不做", "create_time": "2026-11-10 11:39:30"}
    h = FakeHub()
    handle_event(raw, h, approvers=["ODT_APPROVER"])
    assert h.events[0]["data"] == {"card": "C5", "choice": "2", "reason": "先不做", "by": "ODT_APPROVER"}
    # 昵称冒充审批人 ID 不算数
    fake = dict(raw, event_id="ev2", sender="ODT_APPROVER", sender_open_dingtalk_id="ODT_OTHER")
    h2 = FakeHub()
    assert handle_event(fake, h2, approvers=["ODT_APPROVER"]) is None and not h2.events


def test_gitlab_project_normalize():
    from popto_ops.ci.gitlab import GitLab
    want = "front%2Fbiz-platform%2Fai-growth-middle-platfrom%2Fpopto"
    for p in ("/front/biz-platform/ai-growth-middle-platfrom/popto", "front/biz-platform/ai-growth-middle-platfrom/popto/",
              "https://gitlab.pri.ibanyu.com/front/biz-platform/ai-growth-middle-platfrom/popto.git"):
        assert GitLab.pid(p) == want
    assert GitLab.pid(123) == "123"


def test_gitlab_add_labels_compatible_with_11x():
    from popto_ops.ci.gitlab import GitLab
    gl = GitLab("https://gl.example", token="t")
    calls = []

    def fake(method, path, body=None):
        calls.append((method, path, body))
        return {"labels": ["frontend", "review:round-1"]} if method == "GET" else {}
    gl._req = fake
    gl.add_labels("front/popto", 7, ["review:round-2", "review:passed", "frontend"])
    put = calls[-1]
    assert put[0] == "PUT" and "add_labels" not in put[2]
    assert put[2]["labels"] == "frontend,review:round-1,review:round-2,review:passed"


def test_dingtalk_retries_transient_dns_errors(monkeypatch):
    import subprocess as sp
    import time as _t
    from popto_ops.notifier import DingTalkNotifier
    calls = []
    outs = [(1, "", "dial tcp: lookup mcp-gw.dingtalk.com: i/o timeout"), (0, '{"success": true}', "")]

    def fake(*a, **k):
        calls.append(1)
        rc, out, err = outs[len(calls) - 1]
        return sp.CompletedProcess(a, rc, out, err)
    monkeypatch.setattr(sp, "run", fake)
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    assert DingTalkNotifier({"chat_id": "c"}).send("card", "t", "b")["ok"] and len(calls) == 2
    calls.clear()
    outs[:] = [(1, "", "permission denied")]
    assert not DingTalkNotifier({"chat_id": "c"}).send("card", "t", "b")["ok"] and len(calls) == 1  # 非瞬时错误不重试


def test_listener_watch_stderr_states(tmp_path, monkeypatch):
    import io
    monkeypatch.setenv("OPS_DATA", str(tmp_path))  # 不要写到真实的 ~/popto-ops-data
    from popto_ops.notifier import listener as L
    class P:
        stderr = io.StringIO(
            "[event] ready event_key=x bus_pid=4242 subscribe_id=s\n"
            '[event] transport {"observed":true,"state":"degraded","reconnect_count":6}\n')
    st = {"connected": True, "changed": 0}
    L._watch_stderr(P(), st, "test")
    assert st["bus_pid"] == 4242 and st["connected"] is False
    P.stderr = io.StringIO('[event] transport {"observed":true,"state":"connected","reconnect_count":0}\n')
    L._watch_stderr(P(), st, "test")
    assert st["connected"] is True


def test_demand_lifecycle_prd_mr_merged(home):
    ops = db.open_ops()
    ops.execute("INSERT INTO demands(id,title,status) VALUES ('dm_1','改 favicon','open')")
    ops.commit()
    lease = {"task": {"id": "t1"}, "events": [{"subject": "demand:dm_1", "data": {"demand_id": "dm_1"}}]}
    url = "https://gl/front/x/popto/merge_requests/12"
    assert ingest.apply("ops-prd-writing", {"size": "S", "mr_url": url}, lease)
    assert tuple(ops.execute("SELECT status, mr_links FROM demands").fetchone()) == ("in_dev", url)
    assert ingest.demand_merged("front/x/popto", 12) == ["dm_1"]
    assert ops.execute("SELECT status FROM demands").fetchone()[0] == "done"
    assert ingest.demand_merged("front/x/popto", 12) == []              # 幂等


def test_listener_poll_fallback_dedups_by_message_id(tmp_path, monkeypatch):
    import json as _j
    import subprocess as sp
    import threading
    from popto_ops.notifier import listener as L
    monkeypatch.setenv("OPS_DATA", str(tmp_path))
    msgs = {"result": {"messages": [
        {"messageId": "msgA", "createTime": "2026-11-10 13:57:57", "senderId": "ODT_A", "text": "**【需求】卡片** 回复「C2 1」"},
        {"messageId": "msgB", "createTime": "2026-11-10 13:59:00", "senderId": "ODT_A", "text": "C2 1"},
        {"messageId": "msgC", "createTime": "2026-11-10 13:59:30", "senderId": "ODT_X", "text": "C2 3 乱批"}]}}
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 0, _j.dumps(msgs), ""))
    h = FakeHub()
    stop = threading.Event()
    t = threading.Thread(target=L.poll_group, args=("cid", h, ["ODT_A"], False, stop), kwargs={"interval": 0.05})
    t.start()
    import time as _t
    _t.sleep(0.3)
    stop.set()
    t.join(2)
    assert [e["dedup_key"] for e in h.events] == ["dingtalk:msg:msgB"]       # 只有审批人的批红，且多轮轮询只处理一次
    assert h.events[0]["data"]["card"] == "C2"
    # 事件流里同一条消息：去重键相同
    raw = {"type": "user_im_message_receive_group", "event_id": "e9", "message_id": "msgB",
           "sender_open_dingtalk_id": "ODT_A", "content": "C2 1"}
    h2 = FakeHub()
    handle_event(raw, h2, approvers=["ODT_A"])
    assert h2.events[0]["dedup_key"] == "dingtalk:msg:msgB"


def test_demand_initial_score(home):
    ops = db.open_ops()
    lease = {"task": {"id": "t1"}}
    ingest.apply("ops-demand-mining", {"assignments": [
        {"quote": "保险客户要批量出片", "kind": "功能需求", "module": "批量口播", "intensity": 3, "persona": "金融保险从业者",
         "new_demand": {"title": "批量口播支持保险模板", "size": "M", "value": "付费"}},
        {"quote": "也想要", "kind": "功能需求", "module": "批量口播", "intensity": 2, "persona": "普通创作者",
         "demand_id": "dm_" + ingest._sid("批量口播支持保险模板")}]}, lease)
    row = ops.execute("SELECT score, value_factor, persona_dist, signal_count FROM demands").fetchone()
    # (3×1.5 + 2×1.0) × 1.5 ÷ 2 = 4.88
    assert row[0] == 4.88 and row[1] == 1.5 and row[3] == 2
    assert json.loads(row[2]) == {"金融保险从业者": 1, "普通创作者": 1}


def test_readonly_output_instruction_does_not_ask_to_write_files(tmp_path):
    from popto_ops.opsd.context import build_input_md

    class Ctx:
        files, untrusted = [], []
    lease = {"task": {"id": "t1"}, "rule": {"name": "MR 审查", "tools": "readonly", "capability": "review"}, "events": []}
    md = build_input_md(lease, skill_name="ops-mr-review", skill_text="x", schema={}, ctx=Ctx(), agent="codex",
                        structured=False, run_dir=tmp_path)
    assert "不要写任何文件" in md and "output.json" not in md.split("## Skill")[0]
    lease["rule"]["tools"] = "workspace"
    md = build_input_md(lease, skill_name="s", skill_text="x", schema={}, ctx=Ctx(), agent="codex",
                        structured=False, run_dir=tmp_path)
    assert "output.json" in md
