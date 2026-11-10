# popto-ops · Popto 产品运营本地多 Agent 自动化

按 `docs/popto-local-agent-ops/` 的设计实现：**把任务的起点从人身上拿掉**。事件和定时器触发 Agent，Agent 自己推进，只有要拍板的事才变成一张卡片推给人（批奏折）。

```text
事件源（定时 / 采集器 / 钉钉回复 / CI）
   → 事件中台 hub（去重、rules.json 路由、分组串行、防抖、留底、唯一通知出口）
   → 接单程序 opsd（长轮询领任务，按降级链拉起 Claude / Codex / Antigravity / WorkBuddy / 确定性脚本）
   → 结果：日报 / 卡片（票拟）/ 后续事件 / 需求池
   → 人在钉钉回复「C12 1」批红 → 中台发出后续事件 → 确定性执行器执行
```

## 目录

| 路径 | 内容 |
| --- | --- |
| `rules.json` | 事件 → Agent 路由规则（44 条，对应设计 02 §3 的事件目录） |
| `agents.yaml` | Agent 启动方式、能力、并发、降级链、熔断、预算、静默时段、通知出口 |
| `schedules.yaml` | 定时事件（中台每分钟检查，睡眠错过的会补发最近一次） |
| `decisions.md` / `not-doing.md` | 定的事 / 定了不做的事（只追加） |
| `skills/ops-*/` | 15 个流水线 Skill：`SKILL.md`（7 节 SOP）+ `schema.json`（输出校验） |
| `listening/keywords.yaml` | 监听意图词 |
| `popto_ops/hub/` | 事件中台：`core.py` 业务逻辑，`server.py` HTTP（127.0.0.1:8787） |
| `popto_ops/opsd/` | 接单程序：适配器、熔断识别、运行目录与 `input.md`、视频降级预处理 |
| `popto_ops/notifier/` | 通知出口（console / file / dingtalk / lark）、卡片渲染、钉钉回复监听 |
| `popto_ops/collectors/` | 指标快照（T+1h/24h/72h/7d + 异常检测；抖音 / 视频号 / 小红书读创作者后台 `creator.py`）、评论采集、漏斗导入、TikHub 客户端 |
| `popto_ops/agent_browser.py` | Agent 专用浏览器 `ops-browser`（playwright-cli + 本机 Chrome + 固定登录态目录），监听和 GEO 检查用 |
| `popto_ops/executors/` | 发布、评论回复、运营清理执行器（只执行已批准、24 小时内、按 action_id 幂等） |
| `popto_ops/ci/` | CI 中台：发现 Agent MR、审查结论写回、合并闸门、线上核实 |
| `popto_ops/ingest.py` | Agent 结构化产出 → `ops.db`（Agent 自己不写业务库） |
| `dashboards/index.html` | 静态看板模板（`opsctl dashboard` 生成） |
| `launchd/template.plist` | 三个常驻进程的 plist 模板 |
| `docs/整体流程.md` | 整体流程：结构、任务生命周期、四条流水线、一天的节奏 |
| `tests/` | 53 个测试，含真实 HTTP 中台 + opsd + 假 Agent 的端到端测试 |

数据目录 `~/popto-ops-data/`（不进 git，700 权限）：`hub.db`、`ops.db`、`runs/<task_id>/`（input.md、context/、schema.json、output.json、每次尝试的日志）、`notify/`、`logs/`、`local.yaml`（本机配置：代理、钉钉群、审批人、GitLab 项目）。

## 上手

```bash
cd ops
uv sync
uv run opsctl init          # 建数据目录和数据库，生成 ~/popto-ops-data/local.yaml（自动读取 ~/claude.sh 里的代理）
uv run opsctl doctor        # 检查 Agent CLI、dws、ffmpeg、代理、钥匙串、规则
uv run pytest -q            # 53 个测试

# 前台联调（通知先走 console，只打印不外发）
uv run python -m popto_ops.hub &
uv run python -m popto_ops.opsd &
uv run opsctl emit cron.daily_brief --source cron --summary "手动触发日报"
uv run opsctl tasks
tail ~/popto-ops-data/notify/console.log
```

### `正式启用前要配的`

1. `钉钉：local.yaml 里把 notifier.adapter 改成 dingtalk，填运营群 chat_id（dws chat +chat-search --query 运营）和 approvers（能批红的人：群消息事件只带 openDingTalkId，卡片回调带 userId，两个都填；不认昵称）。群号可用 dws chat +chat-get-by-id --group-id <群号> 换成 openConversationId。`
2. `密钥（只放钥匙串）：security add-generic-password -s popto-ops/tikhub -a popto-ops -w <key>；同样配置 redfox、gitlab（机器人账号，api 权限）。`
3. `CI：local.yaml 的 ci.projects 填要接入的仓库（如 popto/frontend_popto）；ci.auto_merge 保持 false（合并都发卡片），跑稳后再打开。`
4. `常驻：uv run opsctl launchd install 生成 plist，确认无误后 uv run opsctl launchd install --load。`
5. `Agent 专用浏览器：先 uv run opsctl browse install`（把 playwright-cli 装到数据目录 `tools/`，需要走代理时加 `--proxy <npm 代理>`），再 `uv run opsctl browse login` 打开有界面的 Chrome，把要用的站点（抖音、小红书、快手、知乎、各家 AI 助手等，`opsctl browse sites` 看清单，也可只登其中几个：`opsctl browse login douyin doubao`）逐个登录后回车。登录态存在 `~/popto-ops-data/browser-profiles/agent-browse`，和发布用的各平台目录分开；建议用专门的小号，不要用发布账号。`opsctl browse check` 检查。
6. `Skill 分发（可选）：uv run opsctl skills link` 把 `ops-*` 软链接到 `~/.claude/skills` 和仓库内 `.claude/skills`、`.agents/skills`。

## 批红

dws 目前发的卡片不支持按钮回调，所以卡片以 Markdown 消息发出，每张带编号（C12）。在群里回复即可：

| 回复 | 作用 |
| --- | --- |
| `C12 1` / `C12 approve_all` | 选第 1 个选项 / 选 key 为 approve_all 的选项 |
| `C12 3 这周人手不够` | 需要原因的选项，原因写在后面（会按选项写进 not-doing.md 或 decisions.md） |
| `记下来：……` | 追加到 decisions.md |
| `需求：……` | 人工转入需求池（demand.manual） |
| `已发 pk_c1_douyin [链接]` | 半自动发布完成后补登记，自动排定数据采集；视频号没有链接、或给的是短链时，采集时按标题在后台对上作品 |
| `暂停 douyin` / `恢复 douyin` | 暂停 / 恢复某个平台、规则或 Agent 的全部自动化 |

只接受 `approvers` 里的人；同样的操作也可以用 `opsctl decide C12 1`、`opsctl note`、`opsctl demand`、`opsctl published`。互动卡片回调（`user_card_action_triggered`）也已接入监听，dws 支持按钮卡片后可直接使用。

## 关键行为

- **启动方式**：Agent 走本机包装脚本 `~/claude.sh`、`~/codex.sh`、`~/agy.sh`（和终端别名一致，代理各用各的）。包装脚本自带全权限参数，实测档位收紧情况：claude 的禁用清单仍生效（只读档位安全；workspace / dev 档位白名单失效，只靠禁用清单，已禁 `security`、`dws`、`ssh`、`git push -f` 等）；codex 的 `exec -s read-only` 能覆盖；agy 的 `--sandbox` 会被 `--dangerously-skip-permissions` 压过，所以 agy 的只读档位改为直调 `~/.local/bin/agy`，代理取 `agy.sh` 里的 `PROXY=`。哪些档位绕开包装脚本由 `agents.yaml` 的 `strict_tiers` 决定。
- **专用浏览器**：Antigravity / Codex 自己没有浏览器工具。`tools: browser` 的任务（社媒监听、GEO 检查）由 opsd 在 `input.md` 里给出 `ops-browser` 的用法，Agent 通过它操作专用 Chrome；包装层固定会话和登录态目录，拦掉 Cookie / 本地存储读写、上传、改请求等命令。浏览器由 opsd 在拉起 Agent 前开好、结束后关掉（Codex 的沙箱里启动不了 Chrome，但能操作已开着的），浏览器档位任务串行执行。默认有界面运行（`agent_browser.headed`）：无头模式下抖音搜索会直接跳滑块验证码，任务期间桌面上会有一个 Chrome 窗口。没登录的站点 Agent 不会尝试登录，会写进 `ops_feedback`。配置在 `agents.yaml` 的 `agent_browser`。
- **降级**：额度 / 认证错误 → 熔断该 Agent（额度 60 分钟或解析出的恢复时间，认证 6 小时并告警），任务立即走下一个 Agent；超时、输出不合格 → 换下一个（不合格先带着校验错误同一 Agent 重试 1 次）；同一 Agent 连续 3 次超时熔断 30 分钟；冷却结束自动半开。
- **不降级到弱模型**：decide / dev 只在 Claude ↔ Codex 之间；review 强制审查者 ≠ 生产者，Codex 不可用时排队等待。
- **额度策略**：`opsctl agents usage claude 0.75` 登记周用量后，P2 任务改由 Codex 主跑，P3 不再用 Claude。
- **积分预算**：带 `credits_per_run` 的规则超出 `budgets.popto_credits` 单日上限就不跑，进日报。
- **通知**：静默时段 22:30–08:00 只发 P0；每天最多 10 张卡片，超出的顺延到次日早上，积压 ≥ 2 张合并成一条“待批清单”；卡片超时按 `on_expire`（必须是不做对外动作的安全选项）处理。
- **不可信输入**：评论、帖子、外部来源事件摘要都放进 `<<<UNTRUSTED_DATA … UNTRUSTED_DATA>>>`；处理它们的规则一律 `readonly` 档位。Agent 子进程环境变量里不带任何 TOKEN / SECRET / API_KEY。
- **全量留底**：每个事件、任务、运行（含会话 ID、费用、耗时、是否降级）、卡片、决定都在 `hub.db`；`opsctl task <id>` 看完整链路。

## 实现状态

| 设计阶段 | 状态 |
| --- | --- |
| 0 骨架：中台、opsd、规则校验、熔断降级、通知出口、批红、决策档案、日报 | ✅ 已实现并实测：真实 Claude / Codex / Antigravity 都跑通了结构化输出；本机全链路跑通了日报 |
| 1 数据回收：发布登记、T+1h/24h/72h/7d 快照、异常 → 单条复盘、评论采集与分拣、漏斗导入 | ✅ 已实现；抖音 / 视频号 / 小红书的指标直接读创作者后台（复用发布登录态，同一平台的发布 / 采集 / 登录排队用浏览器），抖音读不到时 TikHub 兜底；没有作品 ID 的帖子按「标题 + 发布时间」对上后补登记 ID；评论采集目前只有抖音（TikHub）；其他平台用 `opsctl metrics import` 导入后台导出 |
| 2 内容流水线：选题 → 脚本 → 合规 → 素材 → 质检 → 发布包 → 17:00 卡片 → 发布 | ✅ 全部事件链与 Skill 已接好；抖音 / 视频号 / 小红书已有 Playwright 自动发布器（`opsctl publish login|check|try`），按平台 `publishers.<平台>.mode` 切换 off / dry_run / live，失败自动转人工；其余平台半自动 |
| 3 需求与 CI：需求池、Top 10 卡片、PRD、MR 审查 ≤ 3 轮、合并闸门、线上核实 | ✅ 已实现；GitLab 调用未连真实仓库验证（需配置 Token 和项目） |
| 4 获客：监听、线索、GEO | ✅ Skill 与入库已接好，浏览器操作走 `ops-browser`（需先 `opsctl browse login`）；中台迁内网服务器：配置 `hub.host` / `hub.token` 即可 |
| 5 放开自动回复 / 自动发布 | 未开始（按设计需连续 4 周无事故再评估） |
