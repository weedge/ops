"""Agent 产出 → ops.db（确定性写入，由 opsd 在输出通过校验后调用）。

Agent 自己不写业务库：它只输出结构化 JSON，这里按 Skill 名把结果落到对应的表。
"""
from __future__ import annotations

import hashlib
import json
import logging

import yaml

from . import db, paths
from .timeutil import iso

log = logging.getLogger("ingest")


def _sid(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def sync_terms(ops) -> int:
    p = paths.ops_home() / "listening" / "keywords.yaml"
    if not p.exists():
        return 0
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    n = 0
    for cat, terms in data.items():
        for t in terms or []:
            n += ops.execute("INSERT OR IGNORE INTO listening_terms(term, industry, active, created_at, updated_at)"
                             " VALUES (?,?,1,?,?)", (str(t), cat, iso(), iso())).rowcount
    return n


def _signals(ops, items: list[dict], source_type: str, source_ref: str) -> int:
    n = 0
    for s in items or []:
        if not isinstance(s, dict) or not s.get("quote"):
            continue
        sid = "sg_" + _sid(source_ref, s.get("quote", ""))
        n += ops.execute("INSERT OR IGNORE INTO signals(id,source_type,source_ref,quote,kind,module,persona,intensity,created_at)"
                         " VALUES (?,?,?,?,?,?,?,?,?)",
                         (sid, source_type, s.get("source_ref") or source_ref, s["quote"][:300], s.get("kind"), s.get("module"),
                          s.get("persona"), s.get("intensity"), iso())).rowcount
    return n


def comment_triage(ops, out: dict, lease: dict) -> None:
    for c in out.get("comments") or []:
        cid = str(c.get("id"))
        # 兼容 Agent 只给平台评论 ID 的情况：按后缀匹配
        row = ops.execute("SELECT id FROM comments WHERE id=? OR id LIKE ?", (cid, f"%:{cid}")).fetchone()
        if not row:
            continue
        ops.execute("UPDATE comments SET category=?, sentiment=?, reply_draft=?, reply_status=CASE WHEN ? IS NOT NULL"
                    " THEN 'drafted' ELSE reply_status END WHERE id=?",
                    (c.get("category"), c.get("sentiment"), c.get("reply_draft"), c.get("reply_draft"), row[0]))
    _signals(ops, out.get("signals"), "comment", lease["task"]["id"])


def score_demand(ops, did: str, scoring: dict | None = None, value_hint: str | None = None) -> float:
    """按信号汇总初始分与人群分布：需求分 = Σ(意图强度 × 人群权重) × 价值系数 ÷ 成本。"""
    if scoring is None:
        from .config import load_agents_config

        scoring = load_agents_config().get("demand_scoring") or {}
    pw = scoring.get("persona_weights") or {}
    dflt = float(scoring.get("default_persona_weight", 1.0))
    cost = scoring.get("cost") or {"S": 1, "M": 2, "L": 4}
    row = ops.execute("SELECT title, kind, cost_size, value_factor FROM demands WHERE id=?", (did,)).fetchone()
    if not row:
        return 0.0
    title, kind, size, vf = row
    total, dist = 0.0, {}
    for persona, intensity in ops.execute("SELECT persona, intensity FROM signals WHERE demand_id=?", (did,)):
        persona = persona or "未知"
        w = max([float(v) for k, v in pw.items() if k in persona] or [dflt])
        total += float(intensity or 1) * w
        dist[persona] = dist.get(persona, 0) + 1
    if vf is None:
        text = f"{value_hint or ''} {kind or ''} {title or ''}"
        vf = max([float(v) for k, v in (scoring.get("value_factors") or {}).items() if k in text] or [1.0])
    score = round(total * float(vf) / float(cost.get(size or "S", 1)), 2)
    ops.execute("UPDATE demands SET score=?, value_factor=?, persona_dist=?, updated_at=? WHERE id=?",
                (score, vf, json.dumps(dist, ensure_ascii=False), iso(), did))
    return score


def demand_mining(ops, out: dict, lease: dict) -> None:
    for a in out.get("assignments") or []:
        did = a.get("demand_id")
        nd = a.get("new_demand") or None
        if not did and nd and nd.get("title"):
            did = "dm_" + _sid(nd["title"])
            ops.execute("INSERT OR IGNORE INTO demands(id,title,kind,module,signal_count,cost_size,score,status,updated_at)"
                        " VALUES (?,?,?,?,0,?,0,'open',?)", (did, nd["title"], nd.get("kind"), nd.get("module"), nd.get("size"), iso()))
        if not did:
            continue
        sid = "sg_" + _sid(lease["task"]["id"], a.get("quote", ""))
        ops.execute("INSERT OR IGNORE INTO signals(id,source_type,source_ref,quote,kind,module,persona,intensity,demand_id,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sid, "mining", a.get("signal_ref") or lease["task"]["id"], a.get("quote", "")[:300], a.get("kind"),
                     a.get("module"), a.get("persona"), a.get("intensity"), did, iso()))
        ops.execute("UPDATE signals SET demand_id=? WHERE quote=? AND demand_id IS NULL", (did, a.get("quote", "")))
        cnt = ops.execute("SELECT COUNT(*) FROM signals WHERE demand_id=?", (did,)).fetchone()[0]
        ops.execute("UPDATE demands SET signal_count=?, updated_at=? WHERE id=?", (cnt, iso(), did))
        score_demand(ops, did, value_hint=(nd or {}).get("value"))


def weekly_review(ops, out: dict, lease: dict) -> None:
    for d in out.get("top_demands") or []:
        if d.get("demand_id"):
            ops.execute("UPDATE demands SET score=?, cost_size=COALESCE(?, cost_size), updated_at=? WHERE id=?",
                        (d.get("score"), d.get("size"), iso(), d["demand_id"]))


def social_listening(ops, out: dict, lease: dict) -> None:
    from . import platforms

    for ld in out.get("leads") or []:
        ld["platform"] = platforms.normalize(ld.get("platform")) or ""  # “B站”和 bilibili 算同一个平台，不重复入库
        lid = "ld_" + _sid(ld.get("platform", ""), ld.get("profile_url", ""))
        ops.execute("INSERT INTO leads(id,platform,profile_url,nickname,industry,lead_type,followers,score,stage,why,outreach_draft,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?, '发现', ?,?,?) ON CONFLICT(id) DO UPDATE SET score=excluded.score, why=excluded.why,"
                    " outreach_draft=excluded.outreach_draft, updated_at=excluded.updated_at",
                    (lid, ld.get("platform"), ld.get("profile_url"), ld.get("nickname"), ld.get("industry"), ld.get("lead_type"),
                     ld.get("followers"), ld.get("score"), ld.get("why"), ld.get("suggestion"), iso()))
    for t in out.get("term_stats") or []:
        ops.execute("UPDATE listening_terms SET hits_7d=?, updated_at=? WHERE term=?", (t.get("hits", 0), iso(), t.get("term")))
    _signals(ops, out.get("signals"), "listening", lease["task"]["id"])
    # 有还没发过卡片的新线索：接着触发“今日线索卡片”（确定性脚本按库里的线索生成，不靠 Agent 写卡片）
    if ops.execute("SELECT 1 FROM leads WHERE stage='发现' LIMIT 1").fetchone():
        out.setdefault("follow_up_events", []).append({
            "type": "lead.batch.ready", "subject": f"leads:{lease['task']['id']}", "summary": "监听发现新线索，生成今日线索卡片"})


def topic_planning(ops, out: dict, lease: dict) -> None:
    for c in out.get("calendar") or []:
        ops.execute("INSERT OR IGNORE INTO contents(id,parent_id,pillar,persona,topic,popto_features,status,created_at)"
                    " VALUES (?,?,?,?,?,?, 'proposed', ?)",
                    (c["content_id"], c.get("parent_id"), c.get("pillar"), c.get("persona"), c.get("title"),
                     json.dumps(c.get("popto_features") or [], ensure_ascii=False), iso()))


def script_writing(ops, out: dict, lease: dict) -> None:
    cid = out.get("content_id")
    if cid:
        paths_ = [s.get("path") for s in out.get("scripts") or []]
        ops.execute("UPDATE contents SET script_path=?, status='scripted' WHERE id=?", (json.dumps(paths_, ensure_ascii=False), cid))


def incident_triage(ops, out: dict, lease: dict) -> None:
    _signals(ops, out.get("signals"), "incident", lease["task"]["id"])


def _demand_ids(ops, lease: dict) -> list[str]:
    ids = []
    for e in lease.get("events") or []:
        d = e.get("data") or {}
        if d.get("demand_id"):
            ids.append(str(d["demand_id"]))
        elif str(e.get("subject") or "").startswith("demand:"):
            ids.append(str(e["subject"]).split(":", 1)[1])
        elif d.get("iid"):  # 修复 MR：按 MR 链接反查需求
            for r in ops.execute("SELECT id FROM demands WHERE mr_links LIKE ?", (f"%/merge_requests/{d['iid']}%",)):
                ids.append(r[0])
    return list(dict.fromkeys(ids))


def prd_writing(ops, out: dict, lease: dict) -> None:
    """需求 → PRD / 开发：把 PRD 路径、MR 链接、状态写回需求池（approved → prd_review / in_dev）。"""
    for did in _demand_ids(ops, lease):
        row = ops.execute("SELECT mr_links FROM demands WHERE id=?", (did,)).fetchone()
        if not row:
            continue
        links = [x for x in (row[0] or "").split(",") if x]
        if out.get("mr_url") and out["mr_url"] not in links:
            links.append(out["mr_url"])
        status = "in_dev" if links else ("prd_review" if out.get("needs_decision") else "approved")
        ops.execute("UPDATE demands SET status=?, prd_path=COALESCE(?, prd_path), mr_links=?, updated_at=? WHERE id=?",
                    (status, out.get("prd_path"), ",".join(links) or None, iso(), did))


def demand_merged(project: str, iid: int, ops_db=None) -> list[str]:
    """MR 合并（卡片合并或在 GitLab 上直接合并）：关联的需求标为 done。"""
    ops = db.open_ops(ops_db)
    try:
        like = f"%{project}/merge_requests/{iid}%"
        ids = [r[0] for r in ops.execute("SELECT id FROM demands WHERE mr_links LIKE ? AND status<>'done'", (like,))]
        for did in ids:
            ops.execute("UPDATE demands SET status='done', updated_at=? WHERE id=?", (iso(), did))
        ops.commit()
        return ids
    finally:
        ops.close()


HANDLERS = {
    "ops-comment-triage": comment_triage, "ops-demand-mining": demand_mining, "ops-weekly-review": weekly_review,
    "ops-social-listening": social_listening, "ops-topic-planning": topic_planning, "ops-script-writing": script_writing,
    "ops-incident-triage": incident_triage, "ops-prd-writing": prd_writing,
}


def apply(skill: str | None, out: dict, lease: dict, ops_db=None) -> bool:
    fn = HANDLERS.get(skill or "")
    if not fn:
        return False
    ops = db.open_ops(ops_db)
    try:
        ops.execute("BEGIN")
        fn(ops, out, lease)
        ops.execute("COMMIT")
        return True
    except Exception:  # noqa: BLE001
        ops.execute("ROLLBACK")
        log.exception("入库失败：%s", skill)
        return False
    finally:
        ops.close()
