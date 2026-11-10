"""各 Agent 的非交互调用（03 §2.2）。权限由规则的 tools 档位决定，不用人用的全权限包装脚本。"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import paths
from ..validate import extract_json, for_agent

# Claude 工具白名单（按档位）
_RO = ["Read", "Glob", "Grep"]
_WS = _RO + ["Write", "Edit", "Bash(python3:*)", "Bash(uv:*)", "Bash(ffmpeg:*)", "Bash(ffprobe:*)", "Bash(node:*)",
             "Bash(npx:*)", "Bash(ls:*)", "Bash(mkdir:*)", "Bash(cp:*)", "Bash(jq:*)"]
_DEV = _WS + ["Bash(git:*)", "Bash(npm:*)", "Bash(pnpm:*)", "Bash(go:*)", "Bash(make:*)", "Bash(pytest:*)",
              "Bash(python:*)", "Bash(glab:*)"]
CLAUDE_TOOLS = {"readonly": _RO, "workspace": _WS, "browser": _WS, "dev": _DEV}
# 经 ~/claude.sh（bypassPermissions）启动时 allowedTools 不再起限制作用，只有禁用清单生效，所以禁用清单要写全。
_DENY_ALWAYS = ["Bash(security:*)", "Bash(sudo:*)", "Bash(ssh:*)", "Bash(scp:*)", "Bash(dws:*)", "Bash(lark-cli:*)",
                "Bash(launchctl:*)", "Bash(crontab:*)", "Bash(osascript:*)"]
CLAUDE_DENY = {"readonly": ["Bash", "WebFetch", "WebSearch", "Write", "Edit", "NotebookEdit"],
               "workspace": ["WebFetch", "WebSearch", "Bash(rm:*)", "Bash(curl:*)", "Bash(wget:*)", "Bash(git:*)",
                             "Bash(glab:*)", "Bash(gh:*)"] + _DENY_ALWAYS,
               "browser": ["Bash(rm:*)", "Bash(git:*)", "Bash(glab:*)", "Bash(gh:*)"] + _DENY_ALWAYS,
               "dev": ["Bash(rm -rf:*)", "Bash(git push --force:*)", "Bash(git push -f:*)", "Bash(git push origin master:*)",
                       "Bash(git push origin main:*)", "Bash(glab mr merge:*)", "Bash(gh pr merge:*)"] + _DENY_ALWAYS}


@dataclass
class RunSpec:
    task_id: str
    run_dir: Path
    cwd: Path
    prompt: str
    schema: dict
    schema_path: Path
    tools: str
    effort: str | None
    model: str | None
    timeout: float
    agent_cfg: dict
    env: dict
    alive: Callable[[], bool] = lambda: True
    command: list[str] | None = None   # script 用
    extra_dirs: list[Path] = field(default_factory=list)  # 除运行目录外 Agent 需要访问 / 写入的目录（如主仓库 .git）


@dataclass
class RunResult:
    ok: bool
    output: dict | None = None
    raw_text: str = ""
    session_id: str | None = None
    cost_usd: float | None = None
    error_text: str = ""
    timed_out: bool = False
    lease_lost: bool = False
    extra: dict = field(default_factory=dict)


def run_process(cmd: list[str], *, cwd: Path, env: dict, timeout: float, log_path: Path,
                stdin: str | None = None, alive: Callable[[], bool] = lambda: True) -> tuple[int, str, str, bool, bool]:
    """跑子进程（独立进程组），超时或租约丢失时整组杀掉。返回 (rc, stdout, stderr, timed_out, lease_lost)。"""
    out_p, err_p = log_path.with_suffix(".stdout"), log_path.with_suffix(".stderr")
    with out_p.open("w") as fo, err_p.open("w") as fe:
        fe.write(f"$ {' '.join(cmd[:12])}{' …' if len(cmd) > 12 else ''}\n")
        fe.flush()
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                stdout=fo, stderr=fe, start_new_session=True, text=True)
        if stdin is not None:
            try:
                proc.stdin.write(stdin)
                proc.stdin.close()
            except BrokenPipeError:
                pass
        start = time.monotonic()
        timed_out = lost = False
        while proc.poll() is None:
            if time.monotonic() - start > timeout:
                timed_out = True
            elif not alive():
                lost = True
            if timed_out or lost:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(10)
                except Exception:  # noqa: BLE001
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:  # noqa: BLE001
                        pass
                break
            time.sleep(0.5)
        rc = proc.wait()
    return rc, out_p.read_text(errors="replace"), err_p.read_text(errors="replace"), timed_out, lost


def _read_output_file(run_dir: Path) -> dict | None:
    p = run_dir / "output.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def _bin(cfg: dict) -> str:
    return str(Path(os.path.expanduser(cfg["bin"])))


_PROXY_RE = re.compile(r'^\s*(?:export\s+)?PROXY=["\']?(https?://[^"\'\s]+)', re.M)


def wrapper_proxy(bin_path: str) -> str | None:
    """包装脚本（~/agy.sh 等）里 PROXY= 的值；不是脚本或没写代理时返回 None。"""
    p = Path(os.path.expanduser(bin_path))
    if p.suffix != ".sh" or not p.is_file():
        return None
    m = _PROXY_RE.search(p.read_text(errors="replace"))
    return m.group(1) if m else None


def with_proxy(env: dict, proxy: str | None) -> dict:
    if not proxy:
        return env
    env = dict(env)
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        env[k] = proxy
    return env


class Adapter:
    def run(self, spec: RunSpec, log_path: Path) -> RunResult:
        raise NotImplementedError

    @staticmethod
    def strict(spec: RunSpec) -> bool:
        """该档位是否绕开包装脚本直调 strict_bin（包装脚本的全权限参数会压过档位限制时）。"""
        c = spec.agent_cfg
        return bool(c.get("strict_bin")) and spec.tools in (c.get("strict_tiers") or [])

    def resolve_bin(self, spec: RunSpec) -> str:
        c = spec.agent_cfg
        return os.path.expanduser(c["strict_bin"]) if self.strict(spec) else _bin(c)

    def resolve_env(self, spec: RunSpec) -> dict:
        if not self.strict(spec):
            return spec.env  # 包装脚本自己设代理
        c = spec.agent_cfg
        return with_proxy(spec.env, c.get("proxy") or wrapper_proxy(c.get("bin", "")))

    def available(self, cfg: dict) -> bool:
        b = cfg.get("bin")
        return bool(b) and Path(os.path.expanduser(b)).exists()


class ClaudeAdapter(Adapter):
    def build_cmd(self, spec: RunSpec) -> list[str]:
        c = spec.agent_cfg
        cmd = [self.resolve_bin(spec), "-p", "--output-format", "json",
               "--permission-mode", "default" if spec.tools == "readonly" else "acceptEdits",
               "--allowedTools", ",".join(CLAUDE_TOOLS.get(spec.tools, _RO)),
               "--disallowedTools", ",".join(CLAUDE_DENY.get(spec.tools, [])),
               "--add-dir", str(spec.run_dir)]
        for d in spec.extra_dirs:
            cmd += ["--add-dir", str(d)]
        model = spec.model or c.get("default_model")
        if model:
            cmd += ["--model", model]
        if spec.effort:
            cmd += ["--effort", spec.effort]
        if c.get("structured_output", True):
            cmd += ["--json-schema", json.dumps(for_agent(spec.schema), ensure_ascii=False)]
        return cmd

    def run(self, spec, log_path):
        rc, out, err, to, lost = run_process(self.build_cmd(spec), cwd=spec.cwd, env=self.resolve_env(spec),
                                             timeout=spec.timeout, log_path=log_path, stdin=spec.prompt, alive=spec.alive)
        res = RunResult(ok=False, raw_text=out, timed_out=to, lease_lost=lost)
        data = extract_json(out) if out.strip() else None
        if isinstance(data, dict):
            res.session_id = data.get("session_id")
            res.cost_usd = data.get("total_cost_usd")
            if data.get("is_error") or rc != 0:
                res.error_text = f"{data.get('subtype')}: {data.get('result')}\n{err[-1500:]}"
                return res
            output = data.get("structured_output")
            if not isinstance(output, dict):
                output = _read_output_file(spec.run_dir) or extract_json(str(data.get("result") or ""))
            res.output = output if isinstance(output, dict) else None
            res.ok = True
            return res
        res.error_text = (err[-2000:] or out[-2000:]) or f"exit {rc}"
        return res


class CodexAdapter(Adapter):
    def build_cmd(self, spec: RunSpec, last_msg: Path) -> list[str]:
        c = spec.agent_cfg
        sandbox = "read-only" if spec.tools == "readonly" else "workspace-write"
        cmd = [self.resolve_bin(spec), "exec", "--skip-git-repo-check", "-C", str(spec.cwd), "-s", sandbox, "--json",
               "-o", str(last_msg)]
        if spec.tools == "browser":
            # 操作 opsd 预先开好的专用浏览器要连本机套接字；Chrome 本身不在沙箱里启动
            cmd += ["-c", "sandbox_workspace_write.network_access=true"]
        if sandbox != "read-only":
            cmd += ["--add-dir", str(spec.run_dir)]
            for d in spec.extra_dirs:
                cmd += ["--add-dir", str(d)]
        if spec.effort:
            eff = {"max": "xhigh"}.get(spec.effort, spec.effort)
            cmd += ["-c", f"model_reasoning_effort={eff}"]
        if spec.model or c.get("default_model"):
            cmd += ["-m", spec.model or c["default_model"]]
        if c.get("structured_output"):
            cmd += ["--output-schema", str(spec.schema_path)]
        cmd.append("-")  # 从 stdin 读提示词
        return cmd

    def run(self, spec, log_path):
        last = spec.run_dir / f"{log_path.stem}.last.txt"
        rc, out, err, to, lost = run_process(self.build_cmd(spec, last), cwd=spec.cwd, env=self.resolve_env(spec),
                                             timeout=spec.timeout, log_path=log_path, stdin=spec.prompt, alive=spec.alive)
        res = RunResult(ok=False, raw_text=out, timed_out=to, lease_lost=lost)
        errors, completed, failed = [], False, False
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "thread.started":
                res.session_id = ev.get("thread_id")
            elif t == "turn.completed":
                completed = True
            elif t == "turn.failed":
                failed = True
                errors.append(json.dumps(ev, ensure_ascii=False)[:500])
            elif t == "error":  # 含 "Reconnecting... 2/5" 这类过程提示，只有最终没完成时才算失败
                errors.append(json.dumps(ev, ensure_ascii=False)[:500])
        if rc != 0 or failed or not completed:
            # 只用 Codex 自己报的错误事件归类；stderr 里常有无关噪音（如 MCP 服务连不上），没有错误事件时才看它
            res.error_text = "\n".join(errors[-6:]) if errors else err[-1500:]
            return res
        text = last.read_text(errors="replace") if last.exists() else ""
        output = _read_output_file(spec.run_dir) or extract_json(text)
        res.output = output if isinstance(output, dict) else None
        res.ok = True
        return res


def readonly_allow_rules(spec: RunSpec) -> list[str]:
    """运行目录、附加目录，以及运行目录里软链接指向的真实目录（agy 按真实路径判权限）。"""
    dirs: list[Path] = [spec.run_dir.resolve(), *[Path(d).resolve() for d in spec.extra_dirs]]
    try:
        for p in spec.run_dir.rglob("*"):
            if p.is_symlink():
                t = p.resolve()
                dirs.append(t if t.is_dir() else t.parent)
    except OSError:
        pass
    seen, rules = set(), []
    for d in dirs:
        if str(d) in seen:
            continue
        seen.add(str(d))
        rules += [f"read_file({d}/**)", f"list_dir({d}/**)", f"list_dir({d})"]
    return rules


class AntigravityAdapter(Adapter):
    def build_cmd(self, spec: RunSpec) -> list[str]:
        c = spec.agent_cfg
        b = self.resolve_bin(spec)
        cmd = [b, "-p", spec.prompt, "--output-format", "json", "--add-dir", str(spec.run_dir),
               *[x for d in spec.extra_dirs for x in ("--add-dir", str(d))],
               "--print-timeout", f"{int(spec.timeout)}s"]
        if spec.effort:
            cmd += ["--effort", spec.effort]
        if spec.model or c.get("default_model"):
            cmd += ["--model", spec.model or c["default_model"]]
        if spec.tools == "readonly":
            cmd.append("--sandbox")
            # 无人值守时 agy 连读文件都会被拒（且一被拒就整次不输出）：只放行读 / 列目录，写仍然拦住
            cmd += ["--settings", json.dumps({"permissions": {"allow": readonly_allow_rules(spec)}}, ensure_ascii=False)]
        elif not b.endswith(".sh"):  # 包装脚本自己已带
            cmd.append("--dangerously-skip-permissions")
        if c.get("structured_output", True):
            cmd += ["--json-schema", str(spec.schema_path)]
        return cmd

    def run(self, spec, log_path):
        rc, out, err, to, lost = run_process(self.build_cmd(spec), cwd=spec.cwd, env=self.resolve_env(spec),
                                             timeout=spec.timeout + 60, log_path=log_path, alive=spec.alive)
        res = RunResult(ok=False, raw_text=out, timed_out=to, lease_lost=lost)
        data = extract_json(out)
        if rc != 0:
            res.error_text = (err[-2000:] or out[-2000:]) or f"exit {rc}"
            return res
        output = None
        if isinstance(data, dict):
            res.session_id = data.get("conversation_id") or data.get("conversationId") or data.get("session_id")
            if {"status", "summary"} <= set(data):
                output = data
            else:
                for k in ("structured_output", "result", "response", "output", "text"):
                    v = data.get(k)
                    if isinstance(v, dict):
                        output = v
                        break
                    if isinstance(v, str):
                        output = extract_json(v)
                        if output:
                            break
        output = output or _read_output_file(spec.run_dir)
        res.output = output if isinstance(output, dict) else None
        res.ok = True
        return res


class InboxAdapter(Adapter):
    """WorkBuddy：写收件箱，等发件箱（只承接不紧急的任务）。"""

    def available(self, cfg):
        return True

    def run(self, spec, log_path):
        c = spec.agent_cfg
        inbox = Path(os.path.expanduser(c["inbox"]))
        outbox = Path(os.path.expanduser(c["outbox"]))
        inbox.mkdir(parents=True, exist_ok=True)
        outbox.mkdir(parents=True, exist_ok=True)
        out_file = outbox / f"{spec.task_id}.json"
        msg = (f"{spec.prompt}\n\n---\n## 交付方式（WorkBuddy 收件箱任务）\n"
               f"处理完后把结果 JSON 写到：`{out_file}`（只写 JSON 对象）。处理完把本文件改名为 `{spec.task_id}.done.md`。\n")
        (inbox / f"{spec.task_id}.md").write_text(msg, encoding="utf-8")
        start = time.monotonic()
        while time.monotonic() - start < spec.timeout:
            if out_file.exists():
                time.sleep(1)
                out = extract_json(out_file.read_text(encoding="utf-8", errors="replace"))
                return RunResult(ok=isinstance(out, dict), output=out if isinstance(out, dict) else None,
                                 error_text="" if isinstance(out, dict) else "发件箱文件不是 JSON")
            if not spec.alive():
                return RunResult(ok=False, lease_lost=True, error_text="租约丢失")
            time.sleep(10)
        return RunResult(ok=False, timed_out=True, error_text="WorkBuddy 收件箱任务超时未处理")


class ScriptAdapter(Adapter):
    """确定性脚本：stdout 最后一个 JSON 对象就是 output（同样要符合公共 Schema）。"""

    def available(self, cfg):
        return True

    def run(self, spec, log_path):
        cmd = [sys.executable if x == "{python}" else x.replace("{run_dir}", str(spec.run_dir)) for x in spec.command or []]
        env = dict(spec.env)
        pkg_root = Path(__file__).resolve().parent.parent.parent
        env["PYTHONPATH"] = str(pkg_root) + os.pathsep + env.get("PYTHONPATH", "")
        rc, out, err, to, lost = run_process(cmd, cwd=paths.ops_home(), env=env, timeout=spec.timeout,
                                             log_path=log_path, alive=spec.alive)
        res = RunResult(ok=False, raw_text=out, timed_out=to, lease_lost=lost)
        if rc != 0:
            res.error_text = err[-2000:] or f"exit {rc}"
            return res
        output = None
        for line in reversed(out.strip().splitlines()):
            output = extract_json(line)
            if isinstance(output, dict):
                break
        output = output if isinstance(output, dict) else (_read_output_file(spec.run_dir) or extract_json(out))
        res.output = output if isinstance(output, dict) else None
        res.ok = True
        return res


class CommandAdapter(ScriptAdapter):
    """测试 / 自定义 Agent：bin + args，提示词从 stdin 进。"""

    def available(self, cfg):
        return True

    def run(self, spec, log_path):
        c = spec.agent_cfg
        cmd = [os.path.expanduser(x) for x in ([c["bin"]] + list(c.get("args") or []))]
        rc, out, err, to, lost = run_process(cmd, cwd=spec.cwd, env=spec.env, timeout=spec.timeout,
                                             log_path=log_path, stdin=spec.prompt, alive=spec.alive)
        res = RunResult(ok=False, raw_text=out, timed_out=to, lease_lost=lost)
        if rc != 0:
            res.error_text = (err[-2000:] or out[-2000:]) or f"exit {rc}"
            return res
        output = _read_output_file(spec.run_dir) or extract_json(out)
        res.output = output if isinstance(output, dict) else None
        res.ok = True
        return res


ADAPTERS: dict[str, Adapter] = {
    "claude": ClaudeAdapter(), "codex": CodexAdapter(), "antigravity": AntigravityAdapter(),
    "inbox": InboxAdapter(), "script": ScriptAdapter(), "command": CommandAdapter(),
}


def adapter_for(agent_cfg: dict) -> Adapter:
    return ADAPTERS[agent_cfg.get("type", "command")]
