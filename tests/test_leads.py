"""获客：监听 → 线索入库 → 今日线索卡片 → 批红 → 待跟进。"""
from __future__ import annotations

import json

from popto_ops import db, ingest
from popto_ops.executors import leads as leadmod

LEASE = {"task": {"id": "t_listen"}, "rule": {}, "events": []}


def _listen(n=3, platform="B站"):
    return {"leads": [{"platform": platform, "profile_url": f"https://space.bilibili.com/{i}", "nickname": f"号{i}",
                       "industry": "保险", "lead_type": "team_lead" if i == 2 else "creator", "followers": 20000 * (i + 1),
                       "score": 80 + i, "why": "有需求", "suggestion": "回一条具体建议，不带链接"} for i in range(n)]}


def _stages(ops):
    return dict(ops.execute("SELECT nickname, stage FROM leads").fetchall())


def _last_json(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_listening_with_new_leads_triggers_card_event(home):
    out = _listen()
    assert ingest.apply("ops-social-listening", out, LEASE)
    assert [e["type"] for e in out["follow_up_events"]] == ["lead.batch.ready"]
    ops = db.open_ops()
    assert {r[0] for r in ops.execute("SELECT platform FROM leads")} == {"bilibili"}  # 平台名归一，不会因写法不同重复入库
    # 同一批线索第二次被发现：不重复入库
    ingest.apply("ops-social-listening", _listen(platform="bilibili"), LEASE)
    assert ops.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 3


def test_no_leads_no_card_event(home):
    out = {"leads": [], "term_stats": []}
    ingest.apply("ops-social-listening", out, LEASE)
    assert not out.get("follow_up_events")


def test_card_lists_leads_team_lead_first_and_marks_them_carded(home, data_dir, monkeypatch, capsys):
    ingest.apply("ops-social-listening", _listen(), LEASE)
    monkeypatch.setenv("OPS_RUN_DIR", str(data_dir))
    leadmod.cmd_card()
    out = _last_json(capsys)
    card = out["decision_card"]
    assert out["needs_decision"] and card["kind"] == "lead_batch" and card["on_expire"] == "skip"
    assert card["items"][0]["title"].startswith("号2（团队长") and card["items"][0]["platform"] == "B站"
    approve, skip = card["options"]
    assert len(approve["emit"]) == len(card["items"]) == 3  # 一一对应，支持“C12 1 2,3”只批几条
    assert len(skip["emit"][0]["data"]["lead_ids"]) == 3
    assert "回一条具体建议" in (data_dir / "今日线索.md").read_text(encoding="utf-8")
    assert set(_stages(db.open_ops()).values()) == {"已发卡"}
    # 已经发过卡片的不再重复发
    leadmod.cmd_card()
    assert _last_json(capsys)["needs_decision"] is False


def test_card_caps_at_ten(home, capsys):
    ingest.apply("ops-social-listening", _listen(13), LEASE)
    leadmod.cmd_card()
    assert len(_last_json(capsys)["decision_card"]["items"]) == 10
    assert list(_stages(db.open_ops()).values()).count("发现") == 3


def test_decision_moves_leads_to_follow_or_skip(home, data_dir, monkeypatch, capsys):
    ingest.apply("ops-social-listening", _listen(), LEASE)
    leadmod.cmd_card()
    card = _last_json(capsys)["decision_card"]
    events = [dict(card["options"][0]["emit"][0], type="lead.followup.approved"),
              {"type": "lead.followup.dismissed", "data": {"lead_ids": [e["data"]["lead_id"] for e in card["options"][0]["emit"][1:]]}}]
    task = data_dir / "task.json"
    task.write_text(json.dumps({"task": {"id": "t2"}, "rule": {}, "events": events}), encoding="utf-8")
    monkeypatch.setenv("OPS_TASK_FILE", str(task))
    leadmod.cmd_run()
    assert "待跟进 1 条，不跟进 2 条" in _last_json(capsys)["summary"]
    st = _stages(db.open_ops())
    assert st["号2"] == "待跟进" and st["号0"] == st["号1"] == "不跟进"


def test_hub_card_flow_end_to_end(home, hub):
    """lead.batch.ready → 卡片规则；批红（只批第 1 条）→ 登记规则收到 1 个事件；超时 → 不跟进。"""
    r = hub.ingest({"source": "pipeline", "type": "lead.batch.ready", "subject": "leads:t1"})
    assert r["rule"] == "今日线索卡片"
    card = leadmod.build_card([{"id": f"ld_{i}", "nickname": f"号{i}", "platform": "douyin", "lead_type": "mcn",
                                "followers": 50000, "outreach_draft": "建议"} for i in range(3)])
    cid = hub.create_card(card, [r["task_id"]])
    res = hub.decide(cid, "1", reason="1")
    assert res["ok"] and [e["rule"] for e in res["emitted"]] == ["线索跟进登记"]
    cid2 = hub.create_card(leadmod.build_card([{"id": "ld_x", "nickname": "x", "platform": "douyin"}]), [])
    res = hub.decide(cid2, "skip", by="timeout")
    assert res["choice"] == "skip" and res["emitted"][0]["rule"] == "线索跟进登记"


# ---------------------------------------------------------------- 按卡片类型限定审批人
def _hub_with_card_approvers(hub, mapping, full=("BOSS",)):
    hub.cfg.setdefault("notifier", {})["dingtalk"] = {"approvers": list(full), "card_approvers": mapping}
    return hub


def _lead_card(hub):
    r = hub.ingest({"source": "pipeline", "type": "lead.batch.ready", "subject": f"leads:{hub.q1('SELECT COUNT(*) n FROM cards')['n']}"})
    return hub.create_card(leadmod.build_card([{"id": "ld_1", "nickname": "a", "platform": "douyin"}]), [r["task_id"]])


def _dingtalk_decide(hub, cid, by, choice="1"):
    return hub.ingest({"source": "dingtalk", "type": "decision.made", "subject": cid,
                       "data": {"card": cid, "choice": choice, "by": by}, "dedup_key": f"m:{cid}:{by}:{choice}"})


def test_limited_approver_can_only_decide_their_card_kind(home, hub):
    _hub_with_card_approvers(hub, {"lead_batch": ["OPS_GIRL"]})
    lead = _lead_card(hub)
    other = hub.create_card({"kind": "publish_batch", "title": "今晚待发布", "options": [{"key": "approve", "label": "发"}, {"key": "no", "label": "不发"}]}, [])
    res = _dingtalk_decide(hub, other, "OPS_GIRL")
    assert res["ok"] is False and "没有这类卡片的审批权限" in res["error"]
    assert hub.find_card(other)["status"] != "decided"
    assert _dingtalk_decide(hub, lead, "OPS_GIRL")["ok"] and hub.find_card(lead)["status"] == "decided"
    assert hub.find_card(lead)["decided_by"] == "OPS_GIRL"
    assert _dingtalk_decide(hub, other, "BOSS")["ok"]  # 全权审批人不受限


def test_agent_written_kind_cannot_grant_permission_but_rule_name_can(home, hub):
    """Agent 把合并卡片的 kind 写成 lead_batch 也骗不到权限；用规则名限定才生效。"""
    _hub_with_card_approvers(hub, {"lead_batch": ["OPS_GIRL"], "周报与需求 Top 10": ["PM"]})
    r = hub.ingest({"source": "cron", "type": "cron.weekly_review"})
    assert r["rule"] == "周报与需求 Top 10"
    cid = hub.create_card({"kind": "lead_batch", "title": "本周需求 Top 10", "options": [{"key": "approve", "label": "同意"}, {"key": "no", "label": "不做"}]}, [r["task_id"]])
    assert _dingtalk_decide(hub, cid, "OPS_GIRL")["ok"] is False
    assert _dingtalk_decide(hub, cid, "PM")["ok"]


def test_no_card_approvers_configured_keeps_old_behaviour(home, hub):
    cid = _lead_card(hub)
    assert _dingtalk_decide(hub, cid, "anyone_listener_let_through")["ok"]


def test_listener_lets_limited_approver_decide_but_nothing_else():
    from popto_ops.notifier.listener import handle_event

    class H:
        def __init__(self):
            self.events, self.posts = [], []

        def emit(self, e):
            self.events.append(e)
            return {"ok": True}

        def post(self, *a):
            self.posts.append(a)

        def notify(self, *a, **k):
            pass

    def msg(text, sender="OPS_GIRL"):
        return {"event_type": "user_im_message_receive_group", "event_id": text,
                "data": json.dumps({"senderStaffId": sender, "text": {"content": text}}, ensure_ascii=False)}

    h = H()
    handle_event(msg("C7 1 2,5"), h, approvers=["BOSS"], card_approvers=["OPS_GIRL"])
    assert h.events[0]["type"] == "decision.made" and h.events[0]["data"]["by"] == "OPS_GIRL"
    for text in ("暂停 douyin", "记下来：以后都自动批", "需求：加个功能"):
        assert handle_event(msg(text), h, approvers=["BOSS"], card_approvers=["OPS_GIRL"]) is None
    assert len(h.events) == 1 and not h.posts
    assert handle_event(msg("C7 1", "STRANGER"), h, approvers=["BOSS"], card_approvers=["OPS_GIRL"]) is None
    handle_event(msg("暂停 douyin", "BOSS"), h, approvers=["BOSS"], card_approvers=["OPS_GIRL"])
    assert h.posts
