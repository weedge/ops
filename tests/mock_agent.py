"""测试用的假 Agent：从 stdin 读 input.md，按 MOCK_<AGENT> 环境变量决定行为。

ok            按 input.md 里的输出 Schema 填满必填字段
quota         模拟额度用尽（stderr + 退出码 1）
invalid       输出不合格的 JSON
invalid_once  第一次不合格，带着校验错误重试时合格
sleep         睡 30 秒（测超时）
decide        输出需要人决定的结果（带卡片）
"""
import json
import os
import re
import sys
import time

agent = sys.argv[1]
mode = os.environ.get(f"MOCK_{agent.upper()}", "ok")
prompt = sys.stdin.read()
log = os.environ.get("OPS_RUN_DIR")
if log:
    with open(os.path.join(log, f"mock-{agent}.prompt.md"), "w") as f:
        f.write(prompt)


def fill(schema: dict) -> dict:
    out = {"status": "done", "summary": f"{agent} 完成", "needs_decision": False, "decision_card": None,
           "follow_up_events": [], "dogfood_friction": []}
    for k in schema.get("required", []):
        if k in out:
            continue
        p = schema["properties"].get(k, {})
        t = p.get("type")
        if "enum" in p:
            out[k] = p["enum"][0]
        elif t == "string":
            out[k] = "这是一段自动生成的测试正文，用来满足最小长度。"
        elif t == "array":
            out[k] = []
        elif t == "boolean":
            out[k] = True
        elif t in ("number", "integer"):
            out[k] = 0
        else:
            out[k] = {}
    return out


m = re.search(r"## 输出 Schema\n```json\n(.*?)\n```", prompt, re.S)
schema = json.loads(m.group(1)) if m else {"required": [], "properties": {}}

if mode == "quota":
    print("Error: You've hit your usage limit. Try again in 2 hours.", file=sys.stderr)
    sys.exit(1)
if mode == "sleep":
    time.sleep(30)
if mode == "invalid" or (mode == "invalid_once" and "上一次输出没有通过校验" not in prompt):
    print(json.dumps({"hello": "world"}))
    sys.exit(0)
out = fill(schema)
if mode == "decide":
    out.update(status="needs_decision", needs_decision=True, decision_card={
        "kind": "demand_top", "title": "测试卡片", "proposal": "同意",
        "options": [{"key": "approve", "label": "同意"}, {"key": "reject", "label": "不同意", "require_reason": True}],
        "on_expire": "reject"})
print("一些思考过程……")
print(json.dumps(out, ensure_ascii=False))
