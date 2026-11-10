"""确定性执行器：只执行“已批准”且未过期的动作，按 action_id 幂等（01 §3.1、05 §4）。"""
from __future__ import annotations

import json
from datetime import timedelta

from ..timeutil import iso, now, parse_iso

APPROVAL_TTL = timedelta(hours=24)


class ActionLedger:
    def __init__(self, ops):
        self.ops = ops

    def begin(self, action_id: str, kind: str, ref: str, approved_at: str | None) -> tuple[bool, str]:
        """返回 (是否可以执行, 原因)。已执行过 / 审批过期都不执行。"""
        row = self.ops.execute("SELECT status FROM actions WHERE action_id=?", (action_id,)).fetchone()
        if row and row[0] in ("done", "running", "awaiting_manual"):
            return False, f"已处理过（{row[0]}）"
        if not approved_at:
            return False, "没有审批记录"
        if now() - parse_iso(approved_at) > APPROVAL_TTL:
            return False, "审批已超过 24 小时，需重新批准"
        self.ops.execute(
            "INSERT INTO actions(action_id,kind,ref,approved_at,status,created_at,updated_at) VALUES (?,?,?,?, 'running', ?, ?)"
            " ON CONFLICT(action_id) DO UPDATE SET status='running', updated_at=excluded.updated_at",
            (action_id, kind, ref, approved_at, iso(), iso()))
        return True, ""

    def finish(self, action_id: str, status: str, result: dict | None = None) -> None:
        self.ops.execute("UPDATE actions SET status=?, result=?, updated_at=? WHERE action_id=?",
                         (status, json.dumps(result or {}, ensure_ascii=False), iso(), action_id))
