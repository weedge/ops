import json

from conftest import at, ok_output


def comment_evt(subject="post:dy_1", key="k1", source="douyin"):
    return {"source": source, "type": "social.comment.batch", "subject": subject, "summary": "新增 3 条评论", "dedup_key": key}


def run(hub, task, agent=None, **out):
    t = task["task"]
    return hub.submit_result(t["id"], {"run_id": f"r_{t['id']}_{t['attempts']}", "agent": agent or task["agent"],
                                       "status": "ok", "output": ok_output(**out)})


def fail(hub, task, kind="error", detail="boom"):
    t = task["task"]
    return hub.submit_result(t["id"], {"run_id": f"r_{t['id']}_{t['attempts']}", "agent": task["agent"], "status": "error",
                                       "error_kind": kind, "error_detail": detail})


def test_dedup_and_unmatched(hub):
    at("2026-11-10 10:00")
    r1 = hub.ingest({"source": "x", "type": "unknown.thing", "dedup_key": "same"})
    r2 = hub.ingest({"source": "x", "type": "unknown.thing", "dedup_key": "same"})
    assert r1["routed"] == "unmatched" and r2["duplicate"] and r2["id"] == r1["id"]
    assert hub.q1("SELECT seen_count FROM events WHERE id=?", (r1["id"],))["seen_count"] == 2


def test_record_only_rule(hub):
    r = hub.ingest({"source": "popto", "type": "popto.feedback.new", "summary": "用户反馈"})
    assert r["routed"] == "none" and not hub.q("SELECT * FROM tasks")


def test_debounce_group_serial_and_rounds(hub):
    at("2026-11-10 10:00")
    a = hub.ingest(comment_evt(key="k1"))
    b = hub.ingest(comment_evt(key="k2"))
    assert a["task_id"] == b["task_id"]                     # 防抖窗口内合并成一个任务
    assert hub.lease("w", None) is None                     # 还在 10 分钟防抖期
    at("2026-11-10 10:11")
    t1 = hub.lease("w", None)
    assert t1 and t1["agent"] == "claude" and len(t1["events"]) == 2
    c = hub.ingest(comment_evt(key="k3"))                   # 运行中又来了同组事件
    assert c["task_id"] != t1["task"]["id"]
    assert hub.q1("SELECT round FROM tasks WHERE id=?", (c["task_id"],))["round"] == 2
    at("2026-11-10 10:30")
    assert hub.lease("w", None) is None                     # 同组同时只跑一轮
    run(hub, t1)
    t2 = hub.lease("w", None)
    assert t2["task"]["id"] == c["task_id"]


def test_different_groups_run_in_parallel(hub):
    at("2026-11-10 10:00")
    hub.ingest(comment_evt("post:a", "ka"))
    hub.ingest(comment_evt("post:b", "kb"))
    at("2026-11-10 10:11")
    assert hub.lease("w", None) and hub.lease("w", None)


def test_agent_concurrency(hub):
    hub.cfg["agents"]["claude"]["concurrency"] = 1
    at("2026-11-10 10:00")
    hub.ingest(comment_evt("post:a", "ka"))
    hub.ingest(comment_evt("post:b", "kb"))
    at("2026-11-10 10:11")
    first = hub.lease("w", None)
    second = hub.lease("w", None)
    assert first["agent"] == "claude" and second["agent"] == "codex" and second["is_fallback"]


def test_quota_opens_circuit_and_falls_back(hub):
    at("2026-11-10 08:31")
    hub.ingest({"source": "cron", "type": "cron.daily_brief", "summary": "日报"})
    t = hub.lease("w", None)
    assert t["agent"] == "claude"
    r = fail(hub, t, "quota", "Error: usage limit reached, resets in 2 hours")
    assert r["state"] == "queued"
    assert hub.agent_states()["claude"]["state"] == "open"
    t2 = hub.lease("w", None)
    assert t2["agent"] == "codex" and t2["is_fallback"]
    run(hub, t2, summary="日报完成", message_markdown="日报正文")
    detail = hub.task_detail(t["task"]["id"])
    assert detail["task"]["state"] == "done"
    assert [x["agent"] for x in detail["runs"]] == ["claude", "codex"]
    # tell=always：日报正文进 outbox
    assert hub.q1("SELECT body FROM outbox WHERE kind='message'")["body"] == "日报正文"


def test_circuit_half_open_after_cooldown(hub):
    at("2026-11-10 08:31")
    hub.set_agent_state("claude", "open", open_for="60m")
    hub.ingest({"source": "cron", "type": "cron.daily_brief"})
    assert hub.lease("w", None)["agent"] == "codex"
    at("2026-11-10 09:40")
    hub.ingest({"source": "cron", "type": "cron.weekly_review"})
    t = hub.lease("w", None)
    assert t["agent"] == "claude"                      # 冷却结束，半开放行
    run(hub, t)
    assert hub.agent_states()["claude"]["state"] == "closed"


def test_chain_exhausted_fails_and_notifies(hub):
    at("2026-11-10 15:00")
    hub.ingest({"source": "cron", "type": "cron.topic_planning"})   # decide：claude → codex，不降到 workbuddy
    fail(hub, hub.lease("w", None))
    r = fail(hub, hub.lease("w", None))
    assert r["state"] == "failed"
    assert hub.lease("w", None) is None
    assert hub.q1("SELECT title FROM outbox WHERE title LIKE '任务失败%'")


def test_timeout_strikes(hub):
    at("2026-11-10 10:00")
    for i in range(3):
        hub.ingest(comment_evt(f"post:{i}", f"k{i}"))
    at("2026-11-10 10:11")
    for i in range(3):
        t = hub.lease("w", ["claude"])
        fail(hub, t, "timeout")
    assert hub.agent_states()["claude"]["state"] == "open"


def test_reviewer_is_not_producer_and_waits(hub):
    at("2026-11-10 10:00")
    hub.ingest({"source": "pipeline", "type": "content.script.ready", "subject": "content:c1",
                "data": {"producer": "claude"}})
    t = hub.lease("w", None)
    assert t["agent"] == "codex"
    r = fail(hub, t, "quota", "rate limit")
    assert r["state"] == "queued"                     # Codex 额度用尽：不让 Claude 审自己，排队等待
    assert hub.lease("w", None) is None
    at("2026-11-10 11:30")
    assert hub.lease("w", None)["agent"] == "codex"


def test_usage_policy_moves_p2_to_codex(hub):
    at("2026-11-10 10:00")
    hub.set_usage("claude", 0.75)
    hub.ingest({"source": "cron", "type": "cron.daily_brief"})        # P2
    assert hub.lease("w", None)["agent"] == "codex"
    hub.ingest({"source": "cron", "type": "cron.topic_planning"})     # P1 不受影响
    assert hub.lease("w", None)["agent"] == "claude"


def test_result_idempotent_and_stale(hub):
    at("2026-11-10 10:00")
    hub.ingest({"source": "cron", "type": "cron.daily_brief"})
    t = hub.lease("w", None)
    assert run(hub, t, message_markdown="x").get("state") == "done"
    assert run(hub, t, message_markdown="x").get("duplicate")


def test_lease_expiry_requeues(hub):
    at("2026-11-10 10:00")
    hub.ingest({"source": "cron", "type": "cron.daily_brief"})
    t = hub.lease("w", None)
    assert hub.renew(t["task"]["id"], "w")
    at("2026-11-10 10:05")
    hub.tick()
    row = hub.q1("SELECT state, lease_losses FROM tasks WHERE id=?", (t["task"]["id"],))
    assert row["state"] == "queued" and row["lease_losses"] == 1
    assert not hub.renew(t["task"]["id"], "w")
    # 旧租约的结果回来时作废，不覆盖
    assert run(hub, t, message_markdown="x").get("stale")


def test_follow_up_and_dogfood(hub):
    at("2026-11-10 10:00")
    hub.ingest({"source": "dingtalk", "type": "content.topic.approved", "subject": "content:c_20261110_01"})
    t = hub.lease("w", None)
    run(hub, t, scripts=[{"platform": "douyin", "path": "x.md"}],
        follow_up_events=[{"type": "content.script.ready", "subject": "content:c_20261110_01", "data": {"script_paths": ["x.md"]}}],
        dogfood_friction=[{"feature": "批量口播", "problem": "生成超时"}])
    ev = hub.q("SELECT type FROM events ORDER BY received_at")
    assert {"content.script.ready", "ops.dogfood.friction"} <= {e["type"] for e in ev}
    review = hub.q1("SELECT * FROM tasks WHERE rule='脚本合规复核'")
    assert review["producer"] == "claude" and review["parent_task"] == t["task"]["id"]


def _topic_card():
    return {"kind": "topic_calendar", "title": "下周 2 条选题", "proposal": "全部通过",
            "options": [{"key": "approve_all", "label": "全部通过",
                         "emit": [{"type": "content.topic.approved", "subject": "content:c_20261110_01", "data": {"title": "x"}}]},
                        {"key": "revise", "label": "改方向", "require_reason": True, "record": "decision"},
                        {"key": "skip", "label": "下周不排", "require_reason": True, "record": "not_doing"}],
            "on_expire": "revise"}


def test_card_flow_and_decision_emits(hub, notify_dir):
    at("2026-11-10 15:00")
    hub.ingest({"source": "cron", "type": "cron.topic_planning"})
    t = hub.lease("w", None)
    r = hub.submit_result(t["task"]["id"], {"run_id": "r1", "agent": "claude", "status": "ok",
                                            "output": ok_output(status="needs_decision", needs_decision=True,
                                                                decision_card=_topic_card(),
                                                                calendar=[])})
    assert r["state"] == "waiting_decision"
    hub.flush_outbox()
    files = list(notify_dir.glob("*-card.md"))
    assert files and "C1" in files[0].read_text() and "回复「C1 序号」" in files[0].read_text()
    assert hub.decide("C1", "9")["ok"] is False
    res = hub.ingest({"source": "dingtalk", "type": "decision.made", "data": {"card": "C1", "choice": "1", "by": "u1"}})
    assert res["ok"] and res["choice"] == "approve_all"
    assert hub.q1("SELECT state FROM tasks WHERE id=?", (t["task"]["id"],))["state"] == "done"
    assert hub.q1("SELECT * FROM tasks WHERE rule='选题批准后写脚本'")
    assert hub.decide("C1", "2")["already"] == "decided"


def test_reject_with_reason_appends_not_doing(hub, home):
    at("2026-11-10 15:00")
    cid = hub.create_card(_topic_card(), [])
    seq = hub.find_card(cid)["seq"]
    assert "原因" in hub.decide(f"C{seq}", "skip")["error"]
    r = hub.decide(f"C{seq}", "skip", reason="下周全员去展会，不排内容")
    assert r["entry"].startswith("N-20261110-")
    assert "下周全员去展会" in (home / "not-doing.md").read_text()


def test_decision_note(hub, home):
    at("2026-11-10 15:00")
    hub.ingest({"source": "dingtalk", "type": "decision.note", "data": {"text": "小红书一律不放站外链接"}})
    assert "小红书一律不放站外链接" in (home / "decisions.md").read_text()


def test_quiet_hours_defer_and_morning_batch(hub, notify_dir):
    at("2026-11-10 23:00")
    hub.create_card(_topic_card(), [], priority=2)
    hub.create_card({**_topic_card(), "title": "第二张"}, [], priority=2)
    hub.enqueue_notice("alert", "P0 告警", "线上不一致", priority=0)
    hub.flush_outbox()
    assert [p.name.split("-")[-1] for p in notify_dir.glob("*.md")] == ["alert.md"]   # 静默时段只发 P0
    at("2026-11-11 08:01")
    hub.flush_outbox()
    batch = list(notify_dir.glob("*-card_batch.md"))
    assert len(batch) == 1 and "待批清单" in batch[0].read_text()
    assert all(c["status"] == "sent" for c in hub.list_cards())


def test_daily_card_cap(hub, notify_dir):
    hub.cfg["max_cards_per_day"] = 1
    at("2026-11-10 10:00")
    hub.create_card(_topic_card(), [])
    hub.create_card({**_topic_card(), "title": "第二张"}, [])
    hub.flush_outbox()
    assert len(list(notify_dir.glob("*-card.md"))) == 1
    row = hub.q1("SELECT send_after, ref FROM outbox WHERE status='pending'")
    assert row["ref"] == "deferred" and row["send_after"].startswith("2026-11-11T08:00")


def test_card_expiry_applies_safe_default(hub):
    at("2026-11-10 17:00")
    card = {**_topic_card(), "expires_at": "2026-11-10T22:00:00+08:00", "on_expire": "revise"}
    hub.create_card(card, [])
    at("2026-11-10 22:01")
    hub.tick()
    c = hub.list_cards()[0]
    assert c["status"] == "expired" and c["choice"] == "revise" and c["decided_by"] == "timeout"
    assert not hub.q("SELECT * FROM tasks WHERE rule='选题批准后写脚本'")   # 超时不触发 emit


def test_schedules_fire_once_and_catch_up(hub):
    at("2026-11-10 08:00")
    assert hub.tick()["fired"] == []                         # 第一次只记录
    at("2026-11-10 08:31")
    assert "cron.daily_brief" in hub.tick()["fired"]
    assert "cron.daily_brief" not in hub.tick()["fired"]
    at("2026-11-11 10:15")                                   # 睡过了 08:30，醒来补发
    fired = hub.tick()["fired"]
    assert "cron.daily_brief" in fired
    ev = hub.q1("SELECT summary FROM events WHERE type='cron.daily_brief' ORDER BY received_at DESC")
    assert "补发" in ev["summary"]


def test_schedule_max_late(hub):
    at("2026-11-10 16:00")
    hub.tick()
    at("2026-11-10 21:00")                                   # 17:00 的发布卡片 max_late=3h，不补
    assert "cron.publish_batch_card" not in hub.tick()["fired"]


def test_pause_and_resume(hub):
    at("2026-11-10 10:00")
    hub.pause("douyin")
    hub.ingest(comment_evt())
    at("2026-11-10 10:11")
    assert hub.lease("w", None) is None
    hub.resume("douyin")
    assert hub.lease("w", None)


def test_retry_with_forced_agent(hub):
    at("2026-11-10 15:00")
    hub.ingest({"source": "cron", "type": "cron.topic_planning"})
    fail(hub, hub.lease("w", None))
    fail(hub, hub.lease("w", None))
    tid = hub.q1("SELECT id FROM tasks")["id"]
    hub.retry(tid, "claude")
    assert hub.lease("w", None)["agent"] == "claude"


def test_max_rounds(hub):
    at("2026-11-10 10:00")
    for i in range(4):
        r = hub.ingest({"source": "gitlab", "type": "gitlab.mr.updated", "subject": "mr:fe!1", "dedup_key": f"sha{i}",
                        "data": {"producer": "claude"}})
        t = hub.lease("w", None)
        if t:
            run(hub, t, findings=[])
    assert r["routed"] == "max_rounds"
    assert hub.q1("SELECT COUNT(*) n FROM tasks WHERE rule='MR 审查'")["n"] == 3


def test_digest(hub):
    at("2026-11-10 10:00")
    hub.ingest({"source": "popto", "type": "popto.feedback.new"})
    hub.ingest({"source": "cron", "type": "cron.daily_brief"})
    run(hub, hub.lease("w", None), message_markdown="x")
    d = hub.digest("2026-11-10T00:00:00+08:00")
    assert d["unread_feedback"] == 1 and "每日运营日报" in d["tasks_by_rule"]
    json.dumps(d, default=str)


def test_daily_brief_waits_for_catchup_collectors(hub):
    """睡眠醒来补发：日报和采集同一轮触发时，日报等采集跑完再派（最多等 wait_max）。"""
    at("2026-11-10 11:10")
    hub.ingest({"source": "cron", "type": "cron.comment_collect", "dedup_key": "c1"})
    hub.ingest({"source": "cron", "type": "cron.daily_brief", "dedup_key": "b1"})
    first = hub.lease("w", None)
    assert first["rule"]["name"] == "评论采集"
    assert hub.lease("w", None) is None                     # 采集还在跑，日报先不派
    run(hub, first, summary="采集完成")
    brief = hub.lease("w", None)
    assert brief and brief["agent"] == "claude"
    run(hub, brief, message_markdown="日报正文" * 5)
    # 采集卡住时最多等 30 分钟
    at("2026-11-10 12:00")
    hub.ingest({"source": "cron", "type": "cron.comment_collect", "dedup_key": "c2"})
    hub.ingest({"source": "cron", "type": "cron.daily_brief", "dedup_key": "b2"})
    stuck = hub.lease("w", ["script"])
    assert stuck and hub.lease("w", ["claude"]) is None
    at("2026-11-10 12:31")
    assert hub.lease("w", ["claude"]) is not None


def test_schedule_requires_config(hub):
    at("2026-11-10 10:00")
    hub.tick()                                              # 第一次只记录
    at("2026-11-10 10:31")
    fired = hub.tick()["fired"]
    assert "cron.ci_poll" not in fired                      # ci.projects 为空：不触发
    hub.cfg.setdefault("ci", {})["projects"] = ["popto/frontend_popto"]
    at("2026-11-10 11:01")
    assert "cron.ci_poll" in hub.tick()["fired"]


def test_ops_feedback_not_in_demand_pool(hub):
    at("2026-11-10 10:00")
    hub.ingest({"source": "cron", "type": "cron.daily_brief", "dedup_key": "b"})
    t = hub.lease("w", None)
    run(hub, t, message_markdown="日报正文" * 5,
        ops_feedback=[{"area": "日报", "problem": "补发时读到空数据"}],
        dogfood_friction=[{"feature": "批量口播", "problem": "导出超时"}])
    types = [r["type"] for r in hub.q("SELECT type FROM events")]
    assert "ops.system.feedback" in types and "ops.dogfood.friction" in types
    fb = hub.q1("SELECT * FROM events WHERE type='ops.system.feedback'")
    assert fb["routed"] == "none"                           # 只留底进日报，不派任务、不进需求池
    assert not hub.q("SELECT * FROM task_events WHERE event_id=?", (fb["id"],))


def test_decision_sends_ack_message(hub):
    at("2026-11-10 10:00")
    hub.ingest({"source": "cron", "type": "cron.daily_brief", "dedup_key": "b"})
    t = hub.lease("w", None)
    card = {"title": "做不做 X", "proposal": "做", "options": [
        {"key": "go", "label": "做", "emit": [{"type": "demand.approved", "subject": "demand:dm_1"}]},
        {"key": "no", "label": "不做", "require_reason": True}]}
    run(hub, t, status="needs_decision", needs_decision=True, decision_card=card, message_markdown="日报正文" * 5)
    seq = hub.q1("SELECT seq FROM cards")["seq"]
    hub.decide(f"C{seq}", "1", by="u1")
    ack = hub.q1("SELECT * FROM outbox WHERE ref LIKE 'ack:%'")
    assert ack and f"已收到 C{seq}" in ack["title"] and "需求批准 → PRD" in ack["body"]
    hub.decide(f"C{seq}", "1", by="u1")                     # 重复批红不重复回执
    assert hub.q1("SELECT COUNT(*) AS n FROM outbox WHERE ref LIKE 'ack:%'")["n"] == 1
