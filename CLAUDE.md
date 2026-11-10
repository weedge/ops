@AGENTS.md

## Claude 专属说明

- 你是主力：决策 / 写作 / 开发。审查交给 Codex，不审自己写的代码。
- 被 opsd 拉起时是 `claude -p` 非交互模式，工具权限按规则的 tools 档位给；做不了的事写进输出，不要尝试绕过。
- 开发任务的 worktree 放在目标仓库同级目录：`../<repo>-agent-<task_id>`，完成后不要删除（CI 中台核实后统一清理）。
- 改本仓库（popto_ops/、rules.json、agents.yaml、skills/）后必须跑 `uv run pytest` 和 `uv run opsctl rules check`。
