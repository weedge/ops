import json
from datetime import datetime

from popto_ops import dashboard, db, scheduler
from popto_ops.timeutil import TZ, iso

from conftest import at, ok_output


def _card():
    return {"kind": "topic_calendar", "title": "下周内容日历：2 条，请批", "proposal": "按建议排", "reasons": ["依据"], "risks": [],
            "items": [{"title": "a"}, {"title": "b"}],
            "options": [{"key": "approve_all", "label": "全部同意", "emit": []},
                        {"key": "reject", "label": "不做", "require_reason": True, "record": "not_doing"}],
            "on_expire": "reject"}


def _run(hub, **out):
    t = hub.lease("w", None)
    hub.submit_result(t["task"]["id"], {"run_id": f"r_{t['task']['id']}", "agent": t["agent"], "status": "ok",
                                        "output": ok_output(**out)})
    return t["task"]["id"]


def test_next_due():
    now = datetime(2026, 11, 10, 20, 10, tzinfo=TZ)  # 周二
    assert iso(scheduler.next_due({"every": "30m"}, now)) == "2026-11-10T20:30:00+08:00"
    assert iso(scheduler.next_due({"at": "08:30"}, now)) == "2026-11-11T08:30:00+08:00"
    assert iso(scheduler.next_due({"at": "17:00"}, now.replace(hour=9))) == "2026-11-10T17:00:00+08:00"
    assert iso(scheduler.next_due({"at": "WED 10:00"}, now)) == "2026-11-11T10:00:00+08:00"
    assert iso(scheduler.next_due({"at": "MON 09:00"}, now)) == "2026-11-16T09:00:00+08:00"


def test_collect_on_empty_db(hub, home):
    at("2026-11-10 10:00")
    d = dashboard.collect(probe_hub=False)
    json.dumps(d, default=str)
    assert d["kpi"]["auto_close_rate"] is None and d["kpi"]["run_success_rate"] is None
    assert d["todo"] == {"cards": [], "manual_publish": [], "failed_tasks": [], "agents_open": []}
    assert len(d["daily"]["days"]) == 7 and d["daily"]["days"][-1] == "2026-11-10" and d["since"].startswith("2026-11-04T00:00")
    # 每条规则都归到一条流水线里，一条不漏
    rules = json.loads((home / "rules.json").read_text(encoding="utf-8"))
    assert sum(p["rules"] for p in d["pipelines"]) == len(rules) == len(d["rules"])
    by_name = {r["rule"]: r["pipeline"] for r in d["rules"]}
    assert by_name["选题批准后写脚本"] == "content" and by_name["到点的指标快照"] == "data"
    assert by_name["MR 审查"] == "dev" and by_name["社媒舆情监听"] == "growth" and by_name["周日大扫除"] == "system"
    # 定时表：没填 ci.projects 的不触发；每条都有下一次时间
    sched = {s["type"]: s for s in d["schedules"]}
    assert sched["cron.ci_poll"]["status"] == "off" and sched["cron.daily_brief"]["rule"] == "每日运营日报"
    assert all(s["next_due"] > d["generated_at"] for s in d["schedules"])
    assert {b["key"] for b in d["budgets"]} >= {"popto_credits", "tikhub_requests", "cards"}
    assert [x["id"] for x in d["decisions"] if x["kind"] == "定的事"]


def test_collect_reflects_activity(hub, home):
    at("2026-11-10 15:00")
    hub.ingest({"source": "cron", "type": "cron.daily_brief"})
    _run(hub, summary="日报：一切正常", message_markdown="x")
    hub.ingest({"source": "cron", "type": "cron.topic_planning"})
    waiting = _run(hub, status="needs_decision", needs_decision=True, decision_card=_card(), calendar=[])
    hub.flush_outbox()
    hub.ingest({"source": "cron", "type": "cron.weekly_review"})
    failed = hub.lease("w", None)["task"]["id"]
    hub._finish(failed, "failed", error="boom")
    hub.ingest({"source": "x", "type": "ops.system.feedback", "summary": "日报口径",
                "data": {"area": "日报", "problem": "口径不清", "suggestion": "写明窗口"}})
    hub.set_agent_state("codex", "open", open_for="60m", error="usage limit")
    hub.add_counter("popto_credits", 48)

    ops = db.open_ops()
    ops.execute("INSERT INTO contents(id,parent_id,pillar,persona,topic,popto_features,status,created_at)"
                " VALUES ('c_20261116_01',NULL,'A','保险','保险口播三段式','[\"跟创\"]','scripted',?)", (iso(),))
    ops.execute("INSERT INTO publish_packs(id,content_id,platform,title,scheduled_at,publish_mode,status,created_at,updated_at)"
                " VALUES ('pk_c1_douyin','c_20261116_01','douyin','保险口播三段式','2026-11-10T19:00:00+08:00','semi_auto','approved',?,?)",
                (iso(), iso()))
    ops.execute("INSERT INTO actions(action_id,kind,ref,approved_at,status,created_at,updated_at)"
                " VALUES ('publish:pk_c1_douyin','publish','pk_c1_douyin',?,'awaiting_manual',?,?)", (iso(), iso(), iso()))
    ops.execute("INSERT INTO posts(id,platform,platform_post_id,title,published_at) VALUES ('douyin_1','douyin','1','旧帖','2026-11-09T12:00:00+08:00')")
    ops.execute("INSERT INTO post_metrics(post_id,captured_at,checkpoint,views,likes) VALUES ('douyin_1',?,'24h',100,5)", (iso(),))
    for cp, due, done in (("1h", "2026-11-09T13:00:00+08:00", "skipped:2026-11-10T10:00:00+08:00"),
                          ("24h", "2026-11-10T12:00:00+08:00", iso()), ("72h", "2026-11-12T12:00:00+08:00", None),
                          ("late", "2026-11-10T09:00:00+08:00", None)):
        ops.execute("INSERT INTO metric_jobs(post_id,checkpoint,due_at,done_at) VALUES ('douyin_1',?,?,?)", (cp, due, done))
    ops.execute("INSERT INTO leads(id,platform,nickname,lead_type,score,stage,updated_at) VALUES ('ld_1','bilibili','某团队','team_lead',90,'已发卡',?)", (iso(),))
    ops.execute("INSERT INTO demands(id,title,score,status,mr_links,updated_at) VALUES ('dm_1','横屏口播',3.0,'in_dev','https://g/x/merge_requests/7',?)", (iso(),))
    ops.commit()
    ops.close()

    d = dashboard.collect(probe_hub=False)
    json.dumps(d, default=str)

    card = d["todo"]["cards"][0]
    assert card["seq"] == 1 and card["items"] == 2 and card["on_expire"] == "reject"
    assert [(o["n"], o["key"], o["require_reason"]) for o in card["options"]] == [(1, "approve_all", False), (2, "reject", True)]
    assert d["todo"]["manual_publish"][0]["pack_id"] == "pk_c1_douyin" and d["todo"]["manual_publish"][0]["platform"] == "douyin"
    assert [f["id"] for f in d["todo"]["failed_tasks"]] == [failed] and d["failures"] == d["todo"]["failed_tasks"]
    assert [a["agent"] for a in d["todo"]["agents_open"]] == ["codex"]

    k = d["kpi"]
    assert k["tasks_done"] == 1 and k["tasks_failed"] == 1 and k["run_success_rate"] == 1.0 and k["active_days"] == 1
    assert k["auto_close_rate"] == 0.0  # 沿用原口径：1 - 带卡片的任务 ÷ 完成的任务
    assert d["daily"]["runs_ok"][-1] == 2 and d["daily"]["cards"][-1] == 1 and sum(d["daily"]["runs_ok"][:-1]) == 0
    assert d["health"]["queue"]["waiting_decision"] == 1 and d["health"]["hub_online"] is None

    rules = {r["rule"]: r for r in d["rules"]}
    assert rules["下周内容日历"]["states"] == {"waiting_decision": 1} and rules["下周内容日历"]["last_task"] == waiting
    assert rules["周报与需求 Top 10"]["last_summary"] == "boom"
    assert rules["运营系统自身问题"]["events"] == 1 and rules["运营系统自身问题"]["n"] == 0
    pipes = {p["key"]: p for p in d["pipelines"]}
    assert pipes["content"]["states"] == {"waiting_decision": 1} and pipes["data"]["states"] == {"done": 1, "failed": 1}

    assert d["feedback"]["total"] == 1 and d["feedback"]["items"][0]["suggestion"] == "写明窗口"
    assert next(b for b in d["budgets"] if b["key"] == "popto_credits")["used"] == 48
    assert next(b for b in d["budgets"] if b["key"] == "cards")["used"] == 1
    assert d["briefs"]["daily"]["summary"] == "日报：一切正常" and d["briefs"]["weekly"] is None

    cal = d["content"]["calendar"]
    assert cal[0]["date"] == "2026-11-16" and cal[0]["items"][0]["features"] == ["跟创"]
    assert cal[0]["items"][0]["packs"] == [{"platform": "douyin", "status": "approved"}]
    assert d["content"]["packs"][0]["action_status"] == "awaiting_manual"
    post = d["data"]["posts"][0]
    assert post["views"] == 100 and {j["checkpoint"]: j["state"] for j in post["jobs"]} == {
        "1h": "skipped", "24h": "done", "72h": "pending", "late": "overdue"}
    assert d["data"]["jobs"] == {"done": 1, "pending": 1, "overdue": 1, "skipped": 1}
    assert d["demand"]["demands"][0]["mr_links"] == ["https://g/x/merge_requests/7"]
    assert d["growth"]["total"] == 1 and d["growth"]["stages"] == [{"label": "已发卡", "n": 1}]


def test_build_writes_page_and_escapes_data(hub, home, data_dir):
    at("2026-11-10 10:00")
    ops = db.open_ops()
    ops.execute("INSERT INTO leads(id,platform,nickname,stage,score,updated_at) VALUES ('ld_x','douyin','</script><img src=x>','发现',1,?)", (iso(),))
    ops.commit()
    ops.close()
    out = dashboard.build(days=3)
    html = out.read_text(encoding="utf-8")
    assert out == data_dir / "dashboard" / "index.html"
    assert "/*__DATA__*/null" not in html and '"window": "近 3 天"' in html
    assert html.count("</script>") == 1  # 数据里的 </script> 不能把页面脚本截断
    assert "<\\/script><img src=x>" in html
