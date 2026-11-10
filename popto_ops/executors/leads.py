"""线索卡片与跟进登记（获客流水线：监听 → 线索入库 → 卡片 → 人批 → 待跟进）。

python -m popto_ops.executors.leads card   监听跑完后：把还没处理的新线索（stage=发现）做成一张卡片
python -m popto_ops.executors.leads run    人批红后：把线索标成 待跟进 / 不跟进

系统不替人互动：卡片批准只是把线索记成“待跟进”，评论、私信都由人用品牌号去做。
线索阶段：发现 → 已发卡 → 待跟进 / 不跟进。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .. import db, platforms
from ..scriptkit import emit_result, load_task
from ..timeutil import iso, now

MAX_PER_CARD = 10  # 当日建议互动的线索不超过 10 条（ops-social-listening §2.6）
TYPE_LABEL = {"team_lead": "团队长", "mcn": "MCN", "creator": "创作者", "other": "其他"}


def _fans(n) -> str:
    if not n:
        return "粉丝数未知"
    return f"{n / 10000:.1f} 万粉" if n >= 10000 else f"{n} 粉"


def pending(ops, limit: int = MAX_PER_CARD) -> list[dict]:
    """还没发过卡片的新线索：团队长 / MCN 优先，再按分数。"""
    return db.rows(ops.execute(
        "SELECT * FROM leads WHERE stage='发现' ORDER BY CASE WHEN lead_type IN ('team_lead','mcn') THEN 0 ELSE 1 END,"
        " score DESC, updated_at DESC LIMIT ?", (limit,)))


def build_card(leads: list[dict], detail: Path | None = None) -> dict:
    n_pri = sum(1 for ld in leads if ld.get("lead_type") in ("team_lead", "mcn"))
    items, approve = [], []
    for ld in leads:
        head = f"{ld.get('nickname') or '(未知昵称)'}（{TYPE_LABEL.get(ld.get('lead_type') or '', '线索')} · {ld.get('industry') or '行业未知'} · {_fans(ld.get('followers'))}）"
        note = f"{(ld.get('outreach_draft') or ld.get('why') or '')[:90]} {ld.get('profile_url') or ''}".strip()
        items.append({"platform": platforms.label(ld.get("platform")), "title": head, "note": note})
        approve.append({"type": "lead.followup.approved", "subject": f"lead:{ld['id']}",
                        "summary": f"跟进线索 {ld.get('nickname')}", "data": {"lead_id": ld["id"]}})
    ids = [ld["id"] for ld in leads]
    return {
        "kind": "lead_batch",
        "title": f"今日线索 {len(leads)} 条" + (f"（团队长 / MCN {n_pri} 条）" if n_pri else "") + "：要跟进哪些？",
        "proposal": "同意后线索记为“待跟进”，由人用品牌号按建议去互动；系统不会自动评论或私信。",
        "reasons": ["来自今天的社媒监听：命中痛点词 / 方案词，且作者符合目标画像（详情见附件）。"],
        "risks": ["互动话术是 AI 建议，发之前请人看一遍；不带链接、不提收益数字。"],
        "items": items,
        "options": [
            {"key": "approve", "label": "跟进（可只选几条）", "emit": approve},
            {"key": "skip", "label": "这批都不跟进", "emit": [{
                "type": "lead.followup.dismissed", "subject": f"lead-batch:{now().strftime('%Y%m%d-%H%M')}",
                "summary": f"{len(ids)} 条线索不跟进", "data": {"lead_ids": ids}}]},
        ],
        "on_expire": "skip",
        "links": [str(detail)] if detail else [],
    }


def write_detail(leads: list[dict], path: Path) -> Path:
    lines = [f"# 今日线索（{now().strftime('%Y-%m-%d')}）", ""]
    for i, ld in enumerate(leads, 1):
        lines += [f"## {i}. {ld.get('nickname')} · {platforms.label(ld.get('platform'))} · {_fans(ld.get('followers'))}",
                  f"- 主页：{ld.get('profile_url')}",
                  f"- 类型：{TYPE_LABEL.get(ld.get('lead_type') or '', ld.get('lead_type') or '-')} · 行业：{ld.get('industry') or '-'} · 评分：{ld.get('score') or '-'}",
                  f"- 为什么需要 Popto：{ld.get('why') or '-'}",
                  f"- 建议怎么互动：{ld.get('outreach_draft') or '-'}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def cmd_card() -> None:
    ops = db.open_ops()
    leads = pending(ops)
    if not leads:
        emit_result("没有待处理的新线索，不发卡片")
        return
    run_dir = os.environ.get("OPS_RUN_DIR")
    detail = write_detail(leads, Path(run_dir) / "今日线索.md") if run_dir else None
    card = build_card(leads, detail)
    for ld in leads:
        ops.execute("UPDATE leads SET stage='已发卡', updated_at=? WHERE id=? AND stage='发现'", (iso(), ld["id"]))
    ops.commit()
    emit_result(f"今日线索卡片：{len(leads)} 条，等人选要跟进的", status="needs_decision", needs_decision=True,
                decision_card=card, artifacts=[str(detail)] if detail else [])


def cmd_run() -> None:
    lease = load_task()
    ops = db.open_ops()
    follow = skip = 0
    for e in lease.get("events") or []:
        d = e.get("data") or {}
        ids = [d["lead_id"]] if d.get("lead_id") else list(d.get("lead_ids") or [])
        stage = "待跟进" if e.get("type") == "lead.followup.approved" else "不跟进"
        for lid in ids:
            n = ops.execute("UPDATE leads SET stage=?, updated_at=? WHERE id=? AND stage IN ('发现','已发卡')",
                            (stage, iso(), lid)).rowcount
            follow, skip = follow + (n if stage == "待跟进" else 0), skip + (n if stage == "不跟进" else 0)
    ops.commit()
    emit_result(f"线索登记：待跟进 {follow} 条，不跟进 {skip} 条")


if __name__ == "__main__":
    if sys.argv[1:2] == ["card"]:
        cmd_card()
    elif sys.argv[1:2] == ["run"]:
        cmd_run()
    else:
        print(__doc__)
        sys.exit(2)
