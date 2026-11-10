"""静态看板：把 hub.db / ops.db 的统计导出成 JSON，嵌进 dashboards/index.html（02 §8）。"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from . import db, paths
from .timeutil import iso, now, parse_iso


def collect() -> dict:
    hub = db.open_hub()
    ops = db.open_ops()
    since = iso(now() - timedelta(days=7))
    tasks = db.rows(hub.execute("SELECT rule, state, COUNT(*) n FROM tasks WHERE updated_at>=? GROUP BY rule, state", (since,)))
    runs = db.rows(hub.execute("SELECT agent, status, is_fallback, COUNT(*) n, ROUND(AVG(duration_s),1) avg_s, "
                               "ROUND(SUM(COALESCE(cost_usd,0)),2) cost FROM runs WHERE finished_at>=? GROUP BY agent, status, is_fallback",
                               (since,)))
    cards = db.rows(hub.execute("SELECT seq, kind, title, status, choice, decided_by, sent_at, decided_at, created_at FROM cards"
                                " WHERE created_at>=? ORDER BY seq DESC", (since,)))
    durations = []
    for c in cards:
        if c["sent_at"] and c["decided_at"] and c["decided_by"] != "timeout":
            durations.append((parse_iso(c["decided_at"]) - parse_iso(c["sent_at"])).total_seconds())
    done = hub.execute("SELECT COUNT(*) FROM tasks WHERE state='done' AND updated_at>=?", (since,)).fetchone()[0]
    with_card = hub.execute("SELECT COUNT(*) FROM tasks WHERE state IN ('done','waiting_decision') AND card_id IS NOT NULL"
                            " AND updated_at>=?", (since,)).fetchone()[0]
    events = db.rows(hub.execute("SELECT type, COUNT(*) n FROM events WHERE received_at>=? GROUP BY type ORDER BY n DESC", (since,)))
    failures = db.rows(hub.execute("SELECT id, rule, agent, error, updated_at FROM tasks WHERE state='failed' AND updated_at>=?"
                                   " ORDER BY updated_at DESC LIMIT 20", (since,)))
    agents = db.rows(hub.execute("SELECT * FROM agent_state"))
    posts = db.rows(ops.execute(
        "SELECT p.platform, p.title, p.published_at, m.checkpoint, m.views, m.likes, m.comments, m.shares, m.favorites"
        " FROM posts p LEFT JOIN post_metrics m ON m.post_id=p.id AND m.captured_at=(SELECT MAX(captured_at) FROM post_metrics WHERE post_id=p.id)"
        " ORDER BY p.published_at DESC LIMIT 50"))
    funnel = db.rows(ops.execute("SELECT date, SUM(clicks) clicks, SUM(registrations) reg, SUM(first_done) first_done,"
                                 " SUM(paid) paid FROM funnel_daily GROUP BY date ORDER BY date DESC LIMIT 14"))
    return {
        "generated_at": iso(), "window": "近 7 天",
        "kpi": {
            "auto_close_rate": round(1 - with_card / done, 3) if done else None,
            "cards_per_day": round(len([c for c in cards if c["sent_at"]]) / 7, 1),
            "avg_decision_sec": round(sum(durations) / len(durations)) if durations else None,
            "fallback_runs": sum(r["n"] for r in runs if r["is_fallback"]),
            "run_success_rate": round(sum(r["n"] for r in runs if r["status"] == "ok") / max(1, sum(r["n"] for r in runs)), 3),
        },
        "tasks": tasks, "runs": runs, "cards": cards, "events": events, "failures": failures, "agents": agents,
        "posts": posts, "funnel": funnel,
    }


def build() -> Path:
    data = collect()
    tpl = (paths.ops_home() / "dashboards" / "index.html").read_text(encoding="utf-8")
    html = tpl.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/"))
    outp = paths.ops_data() / "dashboard" / "index.html"
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(html, encoding="utf-8")
    return outp
