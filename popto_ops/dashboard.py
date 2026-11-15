"""静态看板：把 hub.db / ops.db 的统计导出成 JSON，嵌进 dashboards/index.html（02 §8）。

页面分六页：总览（待你处理、指标、预算、定时表）、内容、数据回收、需求与开发、获客、系统。
这里只取数，不做展示；状态值的中文名、颜色都在模板里。
"""
from __future__ import annotations

import json
import re
import urllib.request
from datetime import timedelta
from pathlib import Path

from . import db, paths, scheduler
from .config import load_agents_config, load_schedules
from .timeutil import in_window, iso, now, parse_iso, today_start

# 规则归到哪条流水线：按规则监听的第一个事件类型的前缀，从上往下第一条命中的算（docs/整体流程.md §三）
PIPELINES = [
    ("content", "内容流水线", ("content.", "cron.topic_planning", "cron.publish_", "release.note.")),
    ("data", "数据回收", ("social.post.", "social.metrics.", "social.comment.", "social.reply.", "cron.collect_metrics",
                      "cron.comment_collect", "cron.funnel_export", "cron.daily_brief", "cron.weekly_review")),
    ("dev", "需求 → 开发", ("demand.", "gitlab.", "cron.ci_poll", "deploy.", "ops.dogfood.", "popto.feedback.")),
    ("growth", "获客", ("cron.social_listening", "social.mention.", "lead.", "cron.geo_check", "hot.")),
    ("system", "兜底与系统", ("",)),
]

# 内容流转的各个环节：(名称, 事件类型, 这一步由谁完成)
CONTENT_STAGES = [
    ("选题批准", "content.topic.approved"), ("脚本送审", "content.script.ready"), ("合规通过", "content.compliance.passed"),
    ("素材完成", "content.media.ready"), ("质检通过", "content.qa.passed"), ("发布包就绪", "content.pack.ready"),
    ("批准发布", "content.publish.approved"), ("发布登记", "social.post.published"),
]
CONTENT_REWORK = [("合规打回", "content.script.revise"), ("合规不通过", "content.compliance.failed"),
                  ("质检不通过", "content.qa.failed"), ("素材返工", "content.media.rework")]

BUDGET_LABELS = {"popto_credits": "Popto 积分", "tikhub_requests": "TikHub 请求", "redfox_requests": "RedFox 请求"}


def _load(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def _clip(s, n: int = 240) -> str | None:
    if s is None:
        return None
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _cfg_path(cfg: dict, dotted: str):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _pipeline_of(rule: dict) -> str:
    types = (rule.get("when") or {}).get("types") or [""]
    for key, _, prefixes in PIPELINES:
        if any(types[0].startswith(p) for p in prefixes):
            return key
    return "system"


def _hub_online(cfg: dict) -> bool | None:
    """中台是否在线。只探本机 / 内网地址，1 秒超时；探不到算离线。"""
    hub = cfg.get("hub") or {}
    host, port = hub.get("host", "127.0.0.1"), hub.get("port", 8787)
    req = urllib.request.Request(f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}/v1/health")
    if hub.get("token"):
        req.add_header("X-Ops-Token", hub["token"])
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=1) as r:
            return bool(json.loads(r.read().decode() or "{}").get("ok"))
    except Exception:  # noqa: BLE001
        return False


def _listener_status() -> list[dict]:
    """钉钉监听的连接状态（notifier/listener.py 写的 listener.status.json）。"""
    p = paths.ops_data() / "listener.status.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    names = {"user_im_message_receive_group": "群消息", "user_card_action_triggered": "卡片回调", "group_poll": "群消息轮询"}
    return [{"key": k, "name": names.get(k, k), "connected": bool(v.get("connected")), "updated_at": v.get("updated_at"),
             "error": _clip(v.get("last_error"), 160) if not v.get("connected") else None}
            for k, v in data.items() if isinstance(v, dict)]


def _decisions(home: Path) -> list[dict]:
    """decisions.md / not-doing.md 里的条目：## D-20261109-01 标题 + “- 日期：”一行。"""
    out = []
    for fname, kind in (("decisions.md", "定的事"), ("not-doing.md", "不做的事")):
        try:
            text = (home / fname).read_text(encoding="utf-8")
        except OSError:
            continue
        for block in re.split(r"^## ", text, flags=re.M)[1:]:
            head, _, body = block.partition("\n")
            m = re.match(r"([A-Z]-\d{8}-\d+)\s*(.*)", head.strip())
            fields = dict(re.findall(r"^- (\S+?)：(.*)$", body, flags=re.M))
            out.append({"kind": kind, "id": m.group(1) if m else "", "title": (m.group(2) if m else head).strip(),
                        "date": fields.get("日期"), "scope": fields.get("适用") or fields.get("回复口径"),
                        "review": fields.get("复查")})
    return sorted(out, key=lambda d: d.get("date") or "", reverse=True)


def _day(ts: str | None) -> str | None:
    return ts[:10] if ts else None


def _daily(hub, since: str, days: list[str]) -> dict:
    """窗口内每天的量：事件、完成的任务、带卡片的任务、运行成功 / 失败、费用、发出的卡片。"""
    def series(sql: str, args: tuple = (since,)) -> list:
        got = {r[0]: r[1] for r in hub.execute(sql, args)}
        return [got.get(d) or 0 for d in days]

    return {
        "days": days,
        "events": series("SELECT substr(received_at,1,10), COUNT(*) FROM events WHERE received_at>=? GROUP BY 1"),
        "tasks_done": series("SELECT substr(updated_at,1,10), COUNT(*) FROM tasks WHERE state='done' AND updated_at>=? GROUP BY 1"),
        "tasks_card": series("SELECT substr(updated_at,1,10), COUNT(*) FROM tasks WHERE state IN ('done','waiting_decision')"
                             " AND card_id IS NOT NULL AND updated_at>=? GROUP BY 1"),
        "tasks_failed": series("SELECT substr(updated_at,1,10), COUNT(*) FROM tasks WHERE state='failed' AND updated_at>=? GROUP BY 1"),
        "runs_ok": series("SELECT substr(finished_at,1,10), COUNT(*) FROM runs WHERE status='ok' AND finished_at>=? GROUP BY 1"),
        "runs_fail": series("SELECT substr(finished_at,1,10), COUNT(*) FROM runs WHERE status<>'ok' AND finished_at>=? GROUP BY 1"),
        "fallback": series("SELECT substr(finished_at,1,10), COUNT(*) FROM runs WHERE is_fallback=1 AND finished_at>=? GROUP BY 1"),
        "cost": series("SELECT substr(finished_at,1,10), ROUND(SUM(COALESCE(cost_usd,0)),2) FROM runs WHERE finished_at>=? GROUP BY 1"),
        "cards": series("SELECT substr(sent_at,1,10), COUNT(*) FROM cards WHERE sent_at>=? GROUP BY 1"),
    }


def _rules_view(hub, home: Path, since: str) -> tuple[list[dict], list[dict]]:
    """每条规则在窗口内接到的事件数、任务分布、平均耗时、最近一次结果，按流水线分组。"""
    try:
        rules = json.loads((home / "rules.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rules = []
    stat: dict[str, dict] = {}
    for r in hub.execute("SELECT rule, state, COUNT(*) n, MAX(updated_at) last_at FROM tasks WHERE updated_at>=? GROUP BY rule, state", (since,)):
        s = stat.setdefault(r["rule"], {"states": {}, "n": 0, "last_at": None})
        s["states"][r["state"]] = r["n"]
        s["n"] += r["n"]
        s["last_at"] = max(s["last_at"] or "", r["last_at"] or "") or None
    dur = {r["rule"]: r["avg_s"] for r in hub.execute(
        "SELECT t.rule, ROUND(AVG(r.duration_s),1) avg_s FROM runs r JOIN tasks t ON t.id=r.task_id"
        " WHERE r.finished_at>=? AND r.status='ok' GROUP BY t.rule", (since,))}
    last = {r["rule"]: r for r in hub.execute(
        "SELECT rule, id, state, agent, summary, error FROM tasks t WHERE updated_at>=? AND updated_at="
        "(SELECT MAX(updated_at) FROM tasks WHERE rule=t.rule) GROUP BY rule", (since,))}
    evn = {r["rule"]: r["n"] for r in hub.execute("SELECT rule, COUNT(*) n FROM events WHERE received_at>=? AND rule IS NOT NULL GROUP BY rule",
                                                  (since,))}
    rows, seen = [], set()
    for r in rules:
        name = r.get("name")
        seen.add(name)
        s, lt = stat.get(name) or {"states": {}, "n": 0, "last_at": None}, last.get(name)
        rows.append({
            "rule": name, "pipeline": _pipeline_of(r), "to": r.get("to"), "tell": r.get("tell"), "tools": r.get("tools"),
            "skill": r.get("skill"), "enabled": r.get("enabled", True), "types": (r.get("when") or {}).get("types") or [],
            "credits_per_run": r.get("credits_per_run"), "states": s["states"], "n": s["n"], "events": evn.get(name, 0),
            "last_at": s["last_at"], "avg_s": dur.get(name), "last_task": lt["id"] if lt else None, "last_state": lt["state"] if lt else None,
            "last_agent": lt["agent"] if lt else None,
            "last_summary": _clip((lt["error"] if lt["state"] == "failed" else lt["summary"]) if lt else None, 200),
        })
    for name, s in stat.items():  # 规则改过名或已删除，但窗口里还有它的任务
        if name not in seen:
            lt = last.get(name)
            rows.append({"rule": name, "pipeline": "system", "to": None, "tell": None, "tools": None, "skill": None,
                         "enabled": None, "types": [], "credits_per_run": None, "states": s["states"], "n": s["n"],
                         "events": evn.get(name, 0),
                         "last_at": s["last_at"], "avg_s": dur.get(name), "last_task": lt["id"] if lt else None,
                         "last_state": lt["state"] if lt else None, "last_agent": lt["agent"] if lt else None,
                         "last_summary": _clip(lt["summary"] if lt else None, 200)})
    pipes = []
    for key, title, _ in PIPELINES:
        mine = [r for r in rows if r["pipeline"] == key]
        states: dict[str, int] = {}
        for r in mine:
            for st, n in r["states"].items():
                states[st] = states.get(st, 0) + n
        pipes.append({"key": key, "name": title, "rules": len(mine), "active_rules": sum(1 for r in mine if r["n"] or r["events"]),
                      "states": states, "n": sum(states.values()),
                      "last_at": max((r["last_at"] or "" for r in mine), default="") or None})
    return rows, pipes


def _agents_view(hub, cfg: dict, since: str) -> list[dict]:
    conf = cfg.get("agents") or {}
    state = {r["agent"]: dict(r) for r in hub.execute("SELECT * FROM agent_state")}
    agg: dict[str, dict] = {}
    for r in hub.execute("SELECT agent, status, error_kind, is_fallback, COUNT(*) n, SUM(duration_s) dur,"
                         " SUM(COALESCE(cost_usd,0)) cost FROM runs WHERE finished_at>=? GROUP BY 1,2,3,4", (since,)):
        a = agg.setdefault(r["agent"], {"runs": 0, "ok": 0, "fail": 0, "fallback": 0, "dur": 0.0, "cost": 0.0, "errors": {}})
        a["runs"] += r["n"]
        a["dur"] += r["dur"] or 0
        a["cost"] += r["cost"] or 0
        a["fallback"] += r["n"] if r["is_fallback"] else 0
        if r["status"] == "ok":
            a["ok"] += r["n"]
        else:
            a["fail"] += r["n"]
            k = r["error_kind"] or r["status"] or "error"
            a["errors"][k] = a["errors"].get(k, 0) + r["n"]
    out = []
    policy = cfg.get("usage_policy") or {}
    for name in list(conf) + [a for a in list(state) + list(agg) if a not in conf]:
        if any(o["agent"] == name for o in out):
            continue
        c, s, a = conf.get(name) or {}, state.get(name) or {}, agg.get(name) or {}
        out.append({
            "agent": name, "type": c.get("type"), "capabilities": c.get("capabilities") or [], "concurrency": c.get("concurrency"),
            "state": s.get("state") or "closed", "open_until": s.get("open_until"), "unavailable_since": s.get("unavailable_since"),
            "timeout_strikes": s.get("timeout_strikes") or 0, "usage_ratio": s.get("usage_ratio"),
            "usage_policy": policy.get(name), "last_error": _clip(s.get("last_error"), 200) if s.get("state") not in (None, "closed") else None,
            "runs": a.get("runs", 0), "ok": a.get("ok", 0), "fail": a.get("fail", 0), "fallback": a.get("fallback", 0),
            "avg_s": round(a["dur"] / a["runs"], 1) if a.get("runs") else None, "cost": round(a.get("cost", 0), 2),
            "errors": a.get("errors", {}),
        })
    return out


def _schedules_view(hub, home: Path, cfg: dict, rules: list[dict]) -> list[dict]:
    tnow = now()
    state = {r["type"]: dict(r) for r in hub.execute("SELECT * FROM schedule_state")}
    by_type = {t: r["rule"] for r in rules for t in r["types"]}
    out = []
    for s in load_schedules(home):
        typ = s["type"]
        last, nxt, st = scheduler.last_due(s, tnow), scheduler.next_due(s, tnow), state.get(typ) or {}
        fired = st.get("fired_at")
        if s.get("requires") and not _cfg_path(cfg, s["requires"]):
            status = "off"       # 依赖的配置没填，不触发
        elif fired and parse_iso(fired) >= last:
            status = "fired"
        elif tnow - last > scheduler.max_late(s):
            status = "missed"    # 错过太久（机器睡眠 / 中台没开），不再补发
        else:
            status = "pending"   # 到点了还没触发：中台下一次检查会补发
        out.append({"type": typ, "rule": by_type.get(typ), "spec": s.get("every") and f"每 {s['every']}" or str(s.get("at")),
                    "last_due": iso(last), "fired_at": fired, "next_due": iso(nxt), "status": status,
                    "requires": s.get("requires")})
    return sorted(out, key=lambda x: x["next_due"])


def _todo(hub, ops, since: str, agents: list[dict]) -> dict:
    """要人处理的事：待批卡片、等人手动发布的发布包、失败任务、熔断中的 Agent。"""
    cards = []
    for c in hub.execute("SELECT * FROM cards WHERE status IN ('pending','sent') ORDER BY priority, seq"):
        p = _load(c["payload"], {}) or {}
        cards.append({
            "seq": c["seq"], "kind": c["kind"], "title": c["title"], "status": c["status"], "priority": c["priority"],
            "created_at": c["created_at"], "sent_at": c["sent_at"], "expires_at": c["expires_at"],
            "proposal": _clip(p.get("proposal"), 200), "items": len(p.get("items") or []), "on_expire": p.get("on_expire"),
            "options": [{"n": i + 1, "key": o.get("key"), "label": o.get("label"), "require_reason": bool(o.get("require_reason"))}
                        for i, o in enumerate(p.get("options") or []) if isinstance(o, dict)],
        })
    manual = db.rows(ops.execute(
        "SELECT a.ref pack_id, a.approved_at, p.platform, p.title, p.scheduled_at, p.account FROM actions a"
        " LEFT JOIN publish_packs p ON p.id=a.ref WHERE a.kind='publish' AND a.status='awaiting_manual' ORDER BY a.approved_at"))
    failed = [{**r, "error": _clip(r["error"], 200)} for r in db.rows(hub.execute(
        "SELECT id, rule, agent, error, updated_at FROM tasks WHERE state='failed' AND updated_at>=? ORDER BY updated_at DESC LIMIT 20",
        (since,)))]
    return {"cards": cards, "manual_publish": manual, "failed_tasks": failed,
            "agents_open": [a for a in agents if a["state"] != "closed"]}


def _content_view(hub, ops, cfg: dict, since: str) -> dict:
    def stage(typ: str) -> dict:
        r = hub.execute("SELECT COUNT(*) n, COUNT(DISTINCT subject) uniq FROM events WHERE type=? AND received_at>=?", (typ, since)).fetchone()
        return {"events": r["n"], "items": r["uniq"]}

    contents = db.rows(ops.execute("SELECT id, parent_id, pillar, persona, topic, status, popto_features, created_at FROM contents ORDER BY id"))
    packs = db.rows(ops.execute(
        "SELECT p.id, p.content_id, p.platform, p.account, p.title, p.scheduled_at, p.publish_mode, p.status, p.updated_at,"
        " a.status action_status FROM publish_packs p LEFT JOIN actions a ON a.action_id='publish:'||p.id"
        " ORDER BY p.scheduled_at DESC, p.platform LIMIT 60"))
    packs_by_content: dict[str, list] = {}
    for p in packs:
        packs_by_content.setdefault(p["content_id"], []).append({"platform": p["platform"], "status": p["status"]})
    calendar: dict[str, list] = {}
    undated = 0
    for c in contents:
        c["features"] = _load(c.pop("popto_features"), []) or []
        c["packs"] = packs_by_content.get(c["id"], [])
        m = re.match(r"c_(\d{4})(\d{2})(\d{2})_", c["id"])  # 内容 ID 里带计划发布日
        if m:
            calendar.setdefault("-".join(m.groups()), []).append(c)
        else:
            undated += 1

    def dist(col: str) -> list[dict]:
        return db.rows(ops.execute(f"SELECT COALESCE({col}, '未填') label, COUNT(*) n FROM contents GROUP BY 1 ORDER BY n DESC"))

    return {
        "stages": [{"name": n, "type": t, **stage(t)} for n, t in CONTENT_STAGES],
        "rework": [{"name": n, "type": t, **stage(t)} for n, t in CONTENT_REWORK],
        "total": len(contents), "undated": undated,
        "status": dist("status"), "pillars": dist("pillar"), "personas": dist("persona"),
        "calendar": [{"date": d, "items": items} for d, items in sorted(calendar.items())],
        "packs": packs,
        "publishers": [{"platform": k, "mode": (v or {}).get("mode", "off")} for k, v in (cfg.get("publishers") or {}).items()],
    }


def _data_view(ops) -> dict:
    posts = db.rows(ops.execute(
        "SELECT p.id, p.platform, p.account, p.title, p.url, p.published_at, p.publish_mode, p.content_id, m.checkpoint,"
        " m.captured_at, m.source, m.views, m.likes, m.comments, m.shares, m.favorites, m.follows_gained, m.completion_rate, m.avg_watch_sec"
        " FROM posts p LEFT JOIN post_metrics m ON m.post_id=p.id AND m.captured_at=(SELECT MAX(captured_at) FROM post_metrics WHERE post_id=p.id)"
        " ORDER BY p.published_at DESC LIMIT 50"))
    jobs: dict[str, list] = {}
    tnow = iso()
    for j in ops.execute("SELECT * FROM metric_jobs ORDER BY due_at"):
        done = j["done_at"] or ""
        st = "skipped" if done.startswith("skipped") else "done" if done else "overdue" if (j["due_at"] or "") < tnow else "pending"
        jobs.setdefault(j["post_id"], []).append({"checkpoint": j["checkpoint"], "state": st, "due_at": j["due_at"],
                                                  "attempts": j["attempts"], "error": _clip(j["last_error"], 120)})
    for p in posts:
        p["jobs"] = jobs.get(p["id"], [])
    all_jobs = [j for js in jobs.values() for j in js]
    upcoming = db.rows(ops.execute(
        "SELECT j.post_id, j.checkpoint, j.due_at, j.attempts, j.last_error, p.platform, p.title FROM metric_jobs j"
        " LEFT JOIN posts p ON p.id=j.post_id WHERE j.done_at IS NULL ORDER BY j.due_at LIMIT 12"))

    def cdist(col: str) -> list[dict]:
        return db.rows(ops.execute(f"SELECT COALESCE({col}, '未分拣') label, COUNT(*) n FROM comments GROUP BY 1 ORDER BY n DESC"))

    funnel = db.rows(ops.execute("SELECT date, SUM(clicks) clicks, SUM(registrations) reg, SUM(first_done) first_done,"
                                 " SUM(second_done) second_done, SUM(paid) paid FROM funnel_daily GROUP BY date ORDER BY date DESC LIMIT 14"))
    channels = db.rows(ops.execute("SELECT COALESCE(channel_code,'未带渠道码') channel, SUM(clicks) clicks, SUM(registrations) reg,"
                                   " SUM(first_done) first_done, SUM(paid) paid FROM funnel_daily GROUP BY 1 ORDER BY reg DESC LIMIT 20"))
    return {
        "posts": posts, "upcoming_jobs": upcoming,
        "jobs": {s: sum(1 for j in all_jobs if j["state"] == s) for s in ("done", "pending", "overdue", "skipped")},
        "snapshots": ops.execute("SELECT COUNT(*) FROM post_metrics").fetchone()[0],
        "comments": {"total": ops.execute("SELECT COUNT(*) FROM comments").fetchone()[0], "category": cdist("category"),
                     "sentiment": cdist("sentiment"), "reply": cdist("reply_status")},
        "funnel": funnel, "channels": channels,
    }


def _demand_view(hub, ops, since: str) -> dict:
    demands = db.rows(ops.execute("SELECT id, title, kind, module, signal_count, persona_dist, value_factor, cost_size, score, status,"
                                  " prd_path, mr_links, updated_at FROM demands ORDER BY score DESC, updated_at DESC LIMIT 60"))
    for d in demands:
        d["persona_dist"] = _load(d["persona_dist"], {}) or {}
        d["mr_links"] = [x for x in (d["mr_links"] or "").split(",") if x]
        d["has_prd"] = bool(d.pop("prd_path"))

    def sdist(col: str) -> list[dict]:
        return db.rows(ops.execute(f"SELECT COALESCE({col}, '未填') label, COUNT(*) n FROM signals GROUP BY 1 ORDER BY n DESC LIMIT 12"))

    friction = []
    for e in hub.execute("SELECT data, summary, received_at FROM events WHERE type='ops.dogfood.friction' AND received_at>=?"
                         " ORDER BY received_at DESC LIMIT 20", (since,)):
        d = _load(e["data"], {}) or {}
        friction.append({"feature": d.get("feature"), "problem": _clip(d.get("problem") or e["summary"], 260), "at": e["received_at"]})
    return {
        "demands": demands,
        "status": db.rows(ops.execute("SELECT COALESCE(status,'open') label, COUNT(*) n FROM demands GROUP BY 1 ORDER BY n DESC")),
        "signals": {"total": ops.execute("SELECT COUNT(*) FROM signals").fetchone()[0],
                    "unassigned": ops.execute("SELECT COUNT(*) FROM signals WHERE demand_id IS NULL").fetchone()[0],
                    "source": sdist("source_type"), "kind": sdist("kind"),
                    "recent": [{**r, "quote": _clip(r["quote"], 160)} for r in db.rows(ops.execute(
                        "SELECT quote, kind, module, persona, intensity, source_type, demand_id, created_at FROM signals"
                        " ORDER BY created_at DESC, intensity DESC LIMIT 12"))]},
        "friction": friction,
    }


def _growth_view(hub, ops) -> dict:
    def ldist(col: str) -> list[dict]:
        return db.rows(ops.execute(f"SELECT COALESCE({col}, '未填') label, COUNT(*) n FROM leads GROUP BY 1 ORDER BY n DESC LIMIT 12"))

    def latest(rule: str) -> dict | None:
        r = hub.execute("SELECT id, state, agent, summary, error, updated_at FROM tasks WHERE rule=? ORDER BY updated_at DESC LIMIT 1",
                        (rule,)).fetchone()
        return {"id": r["id"], "state": r["state"], "agent": r["agent"], "at": r["updated_at"],
                "summary": _clip(r["error"] if r["state"] == "failed" else r["summary"], 400)} if r else None

    return {
        "total": ops.execute("SELECT COUNT(*) FROM leads").fetchone()[0],
        "stages": ldist("stage"), "types": ldist("lead_type"), "industries": ldist("industry"), "platforms": ldist("platform"),
        "leads": [{**r, "why": _clip(r["why"], 160), "outreach_draft": _clip(r["outreach_draft"], 200)} for r in db.rows(ops.execute(
            "SELECT id, platform, profile_url, nickname, industry, lead_type, followers, score, stage, why, outreach_draft, updated_at"
            " FROM leads ORDER BY score DESC, updated_at DESC LIMIT 40"))],
        "terms": db.rows(ops.execute("SELECT term, industry category, active, hits_7d, updated_at FROM listening_terms"
                                     " ORDER BY hits_7d DESC, term")),
        "listening": latest("社媒舆情监听"), "geo": latest("GEO 检查"),
    }


def collect(days: int = 7, probe_hub: bool = True) -> dict:
    home = paths.ops_home()
    cfg = load_agents_config(home)
    hub = db.open_hub()
    ops = db.open_ops()
    tnow = now()
    since_dt = today_start(tnow) - timedelta(days=days - 1)   # 整天对齐：近 7 天 = 今天 + 往前 6 天
    since = iso(since_dt)
    day_list = [(since_dt + timedelta(days=i)).date().isoformat() for i in range(days)]
    today = tnow.date().isoformat()

    daily = _daily(hub, since, day_list)
    rules, pipelines = _rules_view(hub, home, since)
    agents = _agents_view(hub, cfg, since)

    cards = db.rows(hub.execute("SELECT seq, kind, title, status, choice, decided_by, reason, priority, sent_at, decided_at, expires_at, created_at"
                                " FROM cards WHERE created_at>=? ORDER BY seq DESC", (since,)))
    durations = []
    for c in cards:
        c["decide_sec"] = None
        if c["sent_at"] and c["decided_at"] and c["decided_by"] != "timeout":
            c["decide_sec"] = round((parse_iso(c["decided_at"]) - parse_iso(c["sent_at"])).total_seconds())
            durations.append(c["decide_sec"])
    done, with_card = sum(daily["tasks_done"]), sum(daily["tasks_card"])
    runs_ok, runs_all = sum(daily["runs_ok"]), sum(daily["runs_ok"]) + sum(daily["runs_fail"])
    # 每天卡片数按“实际有事件的天数”平均：刚启用几天时不被空白天数摊薄
    active_days = max(1, sum(1 for n in daily["events"] if n))

    events = db.rows(hub.execute("SELECT type, COALESCE(routed,'') routed, COUNT(*) n, MAX(received_at) last_at FROM events"
                                 " WHERE received_at>=? GROUP BY type, routed ORDER BY n DESC", (since,)))
    failed_runs = [{**r, "error_detail": _clip(r["error_detail"], 200)} for r in db.rows(hub.execute(
        "SELECT r.task_id, t.rule, r.agent, r.is_fallback, r.status, r.error_kind, r.error_detail, r.duration_s, r.finished_at FROM runs r"
        " LEFT JOIN tasks t ON t.id=r.task_id WHERE r.status<>'ok' AND r.finished_at>=? ORDER BY r.finished_at DESC LIMIT 30", (since,)))]
    queue = [{**r, "summary": _clip(r["summary"], 120)} for r in db.rows(hub.execute(
        "SELECT id, rule, state, priority, agent, created_at, updated_at, not_before, summary FROM tasks"
        " WHERE state IN ('queued','leased','waiting_decision') ORDER BY priority, created_at LIMIT 40"))]
    feedback = []
    for e in hub.execute("SELECT data, summary, received_at, seen_count FROM events WHERE type='ops.system.feedback' AND received_at>=?"
                         " ORDER BY received_at DESC LIMIT 40", (since,)):
        d = _load(e["data"], {}) or {}
        feedback.append({"area": d.get("area"), "problem": _clip(d.get("problem") or e["summary"], 260),
                         "suggestion": _clip(d.get("suggestion"), 260), "task": d.get("task"), "at": e["received_at"]})
    feedback_total = hub.execute("SELECT COUNT(*) FROM events WHERE type='ops.system.feedback' AND received_at>=?", (since,)).fetchone()[0]
    outbox = db.rows(hub.execute("SELECT kind, status, COUNT(*) n FROM outbox WHERE created_at>=? GROUP BY 1,2 ORDER BY n DESC", (since,)))

    counters = {r["name"]: r["value"] for r in hub.execute("SELECT name, value FROM counters WHERE day=?", (today,))}
    budgets = [{"key": k, "label": BUDGET_LABELS.get(k, k), "used": counters.get(k, 0), "cap": v, "unit": "今天"}
               for k, v in (cfg.get("budgets") or {}).items()]
    cards_today = hub.execute("SELECT COUNT(*) FROM outbox WHERE kind IN ('card','card_batch') AND status='sent' AND sent_at>=?",
                              (iso(today_start(tnow)),)).fetchone()[0]
    budgets.append({"key": "cards", "label": "卡片", "used": cards_today, "cap": int(cfg.get("max_cards_per_day", 10)), "unit": "今天"})
    for a in agents:
        if a["usage_ratio"] is not None:
            budgets.append({"key": f"usage:{a['agent']}", "label": f"{a['agent']} 周用量", "used": round(a["usage_ratio"] * 100),
                            "cap": 100, "unit": "%", "marks": a.get("usage_policy")})

    state_counts = {r["state"]: r["n"] for r in hub.execute("SELECT state, COUNT(*) n FROM tasks GROUP BY state")}
    quiet = cfg.get("quiet_hours")
    data = {
        "generated_at": iso(tnow), "window": f"近 {days} 天", "days": days, "since": since,
        "health": {
            "hub_online": _hub_online(cfg) if probe_hub else None,
            "notifier": (cfg.get("notifier") or {}).get("adapter"),
            "listener": _listener_status(),
            "quiet_hours": quiet, "in_quiet": in_window(tnow, quiet),
            "paused": db.rows(hub.execute("SELECT key, reason, created_at FROM paused ORDER BY created_at")),
            "queue": {k: state_counts.get(k, 0) for k in ("queued", "leased", "waiting_decision")},
            "outbox_pending": hub.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0],
            "rules": len(rules), "rules_enabled": sum(1 for r in rules if r["enabled"]),
        },
        "kpi": {
            "auto_close_rate": round(1 - with_card / done, 3) if done else None,
            "cards_per_day": round(sum(daily["cards"]) / active_days, 1), "active_days": active_days,
            "avg_decision_sec": round(sum(durations) / len(durations)) if durations else None,
            "fallback_runs": sum(daily["fallback"]),
            "run_success_rate": round(runs_ok / runs_all, 3) if runs_all else None,
            "runs": runs_all, "tasks_done": done, "tasks_failed": sum(daily["tasks_failed"]),
            "events": sum(daily["events"]), "cost": round(sum(daily["cost"]), 2),
        },
        "daily": daily,
        "budgets": budgets,
        "pipelines": pipelines, "rules": rules, "agents": agents,
        "schedules": _schedules_view(hub, home, cfg, rules),
        "todo": _todo(hub, ops, since, agents),
        "cards": cards, "events": events, "failed_runs": failed_runs, "queue": queue,
        "feedback": {"total": feedback_total, "items": feedback}, "outbox": outbox,
        "briefs": {"daily": None, "weekly": None},
        "content": _content_view(hub, ops, cfg, since),
        "data": _data_view(ops),
        "demand": _demand_view(hub, ops, since),
        "growth": _growth_view(hub, ops),
        "decisions": _decisions(home),
    }
    for key, rule in (("daily", "每日运营日报"), ("weekly", "周报与需求 Top 10")):
        r = hub.execute("SELECT id, summary, updated_at, agent FROM tasks WHERE rule=? AND state IN ('done','waiting_decision')"
                        " ORDER BY updated_at DESC LIMIT 1", (rule,)).fetchone()
        if r:
            data["briefs"][key] = {"id": r["id"], "summary": _clip(r["summary"], 600), "at": r["updated_at"], "agent": r["agent"]}
    # 旧字段名：模板以外如果有人读这份 JSON（日报脚本等），保持能用
    data["failures"] = data["todo"]["failed_tasks"]
    data["posts"] = data["data"]["posts"]
    data["funnel"] = data["data"]["funnel"]
    hub.close()
    ops.close()
    return data


def build(days: int = 7) -> Path:
    data = collect(days)
    tpl = (paths.ops_home() / "dashboards" / "index.html").read_text(encoding="utf-8")
    html = tpl.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/"))
    outp = paths.ops_data() / "dashboard" / "index.html"
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(html, encoding="utf-8")
    return outp
