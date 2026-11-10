import json

from popto_ops import db, ingest
from popto_ops.timeutil import iso
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


def test_codex_routing_timeout_is_network(home):
    from popto_ops import config
    cb = config.load_agents_config(home)["circuit_breaker"]
    text = '{"type":"error","message":"Reconnecting... 5/5 (workspace routing discovery timed out)"}'
    assert breaker.classify(text, timed_out=False, cb=cb) == "network"


def test_agy_readonly_settings_allow_reads_incl_symlink_targets(tmp_path):
    import json as _j
    from popto_ops.opsd.agents import AntigravityAdapter
    outside = tmp_path / "content"
    outside.mkdir()
    (outside / "v.mp4").write_bytes(b"x")
    run = tmp_path / "run"
    (run / "media").mkdir(parents=True)
    (run / "media" / "v.mp4").symlink_to(outside / "v.mp4")
    spec = _spec(run, {"type": "antigravity", "bin": "/opt/agy.sh", "strict_bin": "/opt/agy", "strict_tiers": ["readonly"]},
                 "readonly")
    cmd = AntigravityAdapter().build_cmd(spec)
    allow = _j.loads(cmd[cmd.index("--settings") + 1])["permissions"]["allow"]
    assert f"read_file({run.resolve()}/**)" in allow and f"read_file({outside.resolve()}/**)" in allow
    assert not any(a.startswith("write_file") for a in allow)


def test_codex_failure_classified_from_events_not_stderr_noise(tmp_path):
    from popto_ops.opsd.agents import CodexAdapter
    fake = tmp_path / "codex.sh"
    fake.write_text("#!/usr/bin/env python3\nimport sys, json\n"
                    "print(json.dumps({'type': 'turn.failed', 'error': {'message': 'model refused: bad request'}}))\n"
                    "sys.stderr.write('rmcp worker: error sending request for url (https://mcp.postman.com)\\n')\n")
    fake.chmod(0o755)
    r = CodexAdapter().run(_spec(tmp_path, {"type": "codex", "bin": str(fake)}, "readonly"), tmp_path / "a.log")
    assert not r.ok and "bad request" in r.error_text and "postman" not in r.error_text


def test_event_media_hardlinked_not_symlinked(home, tmp_path):
    from popto_ops.opsd.context import ContextBuilder
    src = tmp_path / "content" / "v.mp4"
    src.parent.mkdir()
    src.write_bytes(b"video")
    run = tmp_path / "run"
    run.mkdir()
    ctx = ContextBuilder(None, run)
    try:
        ctx.p_event_media({}, [{"data": {"media_path": str(src)}}])
    finally:
        ctx.close()
    dst = run / "media" / "v.mp4"
    assert dst.exists() and not dst.is_symlink() and dst.read_bytes() == b"video"


def test_platform_names_normalized(home):
    from popto_ops import platforms
    assert platforms.normalize("抖音") == "douyin" and platforms.normalize("B站") == "bilibili"
    assert platforms.normalize("douyin") == "douyin" and platforms.label("抖音") == "抖音"
    ops = db.open_ops()
    pid = metrics.register_post(ops, {"platform": "抖音", "platform_post_id": "123", "title": "t"})
    assert pid == "douyin_123"
    assert ops.execute("SELECT platform FROM posts WHERE id=?", (pid,)).fetchone()[0] == "douyin"


def test_manual_publish_message_has_copy_and_files(tmp_path):
    from popto_ops.executors import publish
    d = tmp_path / "pack"
    d.mkdir()
    (d / "video.mp4").write_bytes(b"v")
    (d / "cover.jpg").write_bytes(b"c")
    meta = {"is_test": True, "test_note": "联调", "media": {"video": "video.mp4", "cover": "cover.jpg"},
            "per_platform": {"抖音": {"title": "下班路过面包店", "body": "三款新品", "topics": ["#面包店"],
                                      "pinned_comment": "主页链接", "ai_label": True, "scheduled_at": "2026-11-10T19:00:00+08:00"}}}
    p = {"id": "pk_x_douyin", "platform": "douyin", "title": "下班路过面包店", "content_id": "x", "pack_dir": str(d)}
    text = publish._manual_instructions(p, meta)
    for want in ("测试内容，不要真的发布", "三款新品", "#面包店", "置顶评论", "AI 生成内容", "19:00", "「cover.jpg」"):
        assert want in text, want
    assert "pack" not in text.split("素材")[0] or str(d) not in text      # 不再给本机目录
    assert [f.name for f in publish._pack_files(p, meta)] == ["cover.jpg", "video.mp4"]


def test_render_card_local_links(tmp_path, monkeypatch):
    from popto_ops.notifier.render import render_card
    monkeypatch.setenv("OPS_DATA", str(tmp_path))
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "weekly.md").write_text("周报")
    card = {"title": "t", "proposal": "p", "options": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
            "links": ["runs/weekly.md", str(tmp_path / "runs"), "https://x.example/mr/1"]}
    body = render_card(card, 9, None)[1]
    assert "见下一条文件「weekly.md」" in body and "[详情](https://x.example/mr/1)" in body
    assert str(tmp_path / "runs") + "`" not in body and "详情：`" not in body


def test_publish_run_modes(home, monkeypatch, tmp_path):
    from popto_ops.executors import publish, publishers as pubs
    ops = db.open_ops()
    d = tmp_path / "pack"
    d.mkdir()
    (d / "video.mp4").write_bytes(b"v")
    (d / "meta.json").write_text(json.dumps({"media": {"video": "video.mp4"},
                                             "per_platform": {"抖音": {"title": "标题", "body": "正文"}}}, ensure_ascii=False))
    for pid in ("pk_a_douyin", "pk_b_douyin"):
        ops.execute("INSERT INTO publish_packs(id,content_id,platform,title,pack_dir,publish_mode,status) VALUES (?,?,?,?,?,?,?)",
                    (pid, pid[3:4], "douyin", "标题", str(d), "semi_auto", "carded"))
    ops.commit()
    sent, emitted = [], {}

    class Hub:
        def notify(self, kind, title, body, **k):
            sent.append((kind, title))
            return {"ok": True}
    monkeypatch.setattr(publish, "hub_client", lambda: Hub())
    monkeypatch.setattr(publish, "emit_result", lambda summary, **k: emitted.update(summary=summary, **k))

    def run_with(mode, result_or_exc, pack_id):
        sent.clear()
        monkeypatch.setattr(publish, "load_task", lambda: {"task": {"id": "t"}, "events": [
            {"data": {"pack_id": pack_id}, "received_at": iso()}]})
        monkeypatch.setattr("popto_ops.config.load_agents_config", lambda *a, **k: {"publishers": {"douyin": {"mode": mode}}})

        def fake_run(p, meta, *, dry_run, headless=True):
            assert dry_run == (mode == "dry_run")
            if isinstance(result_or_exc, Exception):
                raise result_or_exc
            return result_or_exc
        monkeypatch.setattr(pubs, "run", fake_run)
        publish.cmd_run()

    # 试运行：截图 + 仍推人工发布
    shot = tmp_path / "s.png"
    shot.write_bytes(b"p")
    run_with("dry_run", pubs.PublishResult(ok=True, dry_run=True, screenshots=[str(shot)], notes=["已填好"]), "pk_a_douyin")
    kinds = [k for k, _ in sent]
    assert kinds[:2] == ["message", "file"] and any("请手动发布" in t for _, t in sent)
    # 真发布成功：登记作品并发后续事件
    run_with("live", pubs.PublishResult(ok=True, dry_run=False, url="https://www.douyin.com/video/42",
                                        platform_post_id="42"), "pk_b_douyin")
    assert ops.execute("SELECT status FROM publish_packs WHERE id='pk_b_douyin'").fetchone()[0] == "published"
    assert emitted["follow_up_events"][0]["type"] == "social.post.published"
    assert not any("请手动发布" in t for _, t in sent)


def test_publish_live_failure_falls_back_to_manual(home, monkeypatch, tmp_path):
    from popto_ops.executors import publish, publishers as pubs
    ops = db.open_ops()
    ops.execute("INSERT INTO publish_packs(id,content_id,platform,title,pack_dir,publish_mode,status) VALUES "
                "('pk_c_douyin','c','douyin','t',?, 'semi_auto','carded')", (str(tmp_path),))
    ops.commit()
    sent = []
    monkeypatch.setattr(publish, "hub_client", lambda: type("H", (), {"notify": lambda self, k, t, b, **kw: sent.append((k, t))})())
    monkeypatch.setattr(publish, "emit_result", lambda *a, **k: None)
    monkeypatch.setattr(publish, "load_task", lambda: {"task": {"id": "t"}, "events": [
        {"data": {"pack_id": "pk_c_douyin"}, "received_at": iso()}]})
    monkeypatch.setattr("popto_ops.config.load_agents_config", lambda *a, **k: {"publishers": {"douyin": {"mode": "live"}}})

    def boom(*a, **k):
        raise pubs.NotLoggedIn("抖音登录已失效")
    monkeypatch.setattr(pubs, "run", boom)
    publish.cmd_run()
    assert sent[0][0] == "alert" and "登录失效" in sent[0][1] and any("请手动发布" in t for _, t in sent)


def test_test_content_never_live():
    from popto_ops.executors.publish import _publisher_mode
    cfg = {"publishers": {"douyin": {"mode": "live"}}}
    assert _publisher_mode(cfg, "douyin", {"is_test": True}) == "dry_run"
    assert _publisher_mode(cfg, "douyin", {}) == "live"
    assert _publisher_mode({}, "douyin", {}) == "off"


def test_friendly_publish_errors():
    from popto_ops.executors.publishers import PublishError, friendly_error
    raw = Exception('Locator.click: Timeout 30000ms exceeded.\nCall log:\n  - waiting for get_by_role("button", name="发表视频")')
    assert friendly_error(raw) == "等「发表视频」超时（多半是网络慢或页面改版）"
    assert friendly_error(PublishError("视频上传失败")) == "视频上传失败"


def test_late_registration_skips_past_checkpoints(home):
    ops = db.open_ops()
    at("2026-11-10 10:00")
    pid = metrics.register_post(ops, {"platform": "douyin", "platform_post_id": "77", "title": "老作品",
                                      "published_at": "2026-08-31T23:49:00+08:00"})
    jobs = {r[0]: (r[1] or "") for r in ops.execute("SELECT checkpoint, done_at FROM metric_jobs WHERE post_id=?", (pid,))}
    assert set(jobs) == {"1h", "24h", "72h", "7d", "late"}
    assert all(jobs[c].startswith("skipped:") for c in ("1h", "24h", "72h", "7d")) and jobs["late"] == ""
    # 刚发的作品照常排 4 个节点，没有 late
    pid2 = metrics.register_post(ops, {"platform": "douyin", "platform_post_id": "78", "published_at": "2026-11-10T09:30:00+08:00"})
    cps = [r[0] for r in ops.execute("SELECT checkpoint FROM metric_jobs WHERE post_id=? AND done_at IS NULL", (pid2,))]
    assert sorted(cps) == ["1h", "24h", "72h", "7d"]


def test_comment_batch_payload_uses_collected_at(home):
    from popto_ops.collectors.comments import save_comments
    from popto_ops.opsd.context import resolve_payload_ref
    ops = db.open_ops()
    at("2026-11-10 10:00")
    n = save_comments(ops, "douyin_1", [{"id": "c1", "text": "三天前的老评论", "author": "某人", "created_at": 1762500000}],
                      collected_at="2026-11-10T10:00:00+08:00")
    ops.commit()
    rows, untrusted = resolve_payload_ref("ops.db://comments?post_id=douyin_1&since=2026-11-10T10:00:00+08:00", ops)
    assert n == 1 and untrusted and [r["text"] for r in rows] == ["三天前的老评论"]
    assert rows[0]["author_hash"] != "某人"


def test_late_snapshots_never_flag_anomaly(home):
    ops = db.open_ops()
    at("2026-11-10 10:00")
    for i in range(6):
        pid = metrics.register_post(ops, {"platform": "douyin", "platform_post_id": f"l{i}", "published_at": "2026-08-01T10:00:00+08:00"})
        metrics.save_snapshot(ops, {"id": pid, "platform": "douyin", "title": "x"}, "late", {"views": 10}, "creator")
    pid = metrics.register_post(ops, {"platform": "douyin", "platform_post_id": "big", "published_at": "2026-08-01T10:00:00+08:00"})
    evs = metrics.save_snapshot(ops, {"id": pid, "platform": "douyin", "title": "x"}, "late", {"views": 5000}, "creator")
    assert not any(e["type"] == "social.metrics.anomaly" for e in evs)
