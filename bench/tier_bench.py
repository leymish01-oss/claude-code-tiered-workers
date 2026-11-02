#!/usr/bin/env python3
"""tier_bench: measure tiered workers (cheap model first, escalate on a failed check) against one strong
leader model, on tasks mined from your own git history. Standard library only.

  python tier_bench.py doctor                       # is the claude CLI usable headless? (no model call)
  python tier_bench.py validate --tasks tasks.json  # base + new tests must FAIL, commit must PASS
  python tier_bench.py run --tasks tasks.json --arms leader,tiered --repeats 2 --jobs 2

Without headless CLI access, a leader session can drive the same arms with its own subagents:
  prepare <task> <arm> <rep>                     export the run dir, print the next step's handoff prompt
  record  <task> <arm> <rep> --step S --transcript agent-<id>.jsonl    log that subagent's usage
  check   <task> <arm> <rep> <step>              restore tests, run the acceptance command, PASS/FAIL
Both runners write the same two CSVs (column `runner` = cli | subagent); report.py reads either.

A task is a past commit that changed code and its tests. Each run gets a fresh directory OUTSIDE the
repository holding only `git archive <base> -- <paths>` plus the commit's test files, then a one-commit git
repo ("base") so the harness can diff. The repository's history never goes in, so the answer can't leak
through `git log`. Every model call is `claude -p` (headless) with project settings only and no MCP servers.

Arms
  leader          --model opus. On a failed acceptance check, one retry with --resume and the failure log.
  tiered          medium tasks: a read-only haiku-scout first; then sonnet-builder; on a failed check ONE
                  escalation to a fresh opus-architect with the failure log and the builder's diff.
  tiered-noscout  tiered without the scout step.

The pass/fail signal is always the task's executable acceptance command, never a model's opinion.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_PRICES_FILE = HERE.parent / "prices.json"
AGENT_FILES = ("haiku-scout.md", "sonnet-builder.md", "opus-architect.md")
ARMS = ("leader", "tiered", "tiered-noscout")

CLAUDE_TIMEOUT_S = 20 * 60
ACCEPT_TIMEOUT_S = 10 * 60
LOG_TAIL_LINES = 80
SCOUT_REPORT_CHARS = 16_000
DIFF_CHARS = 20_000
SYNTHETIC_MODEL = "<synthetic>"

# Lets a headless `claude -p` edit files inside its cwd and run commands unattended. acceptEdits auto-approves
# edits inside the working directory only; anything else that would prompt is denied (no one is there to
# answer). Network and history-rewriting commands are denied outright.
PERMISSION_ARGS = [
    "--permission-mode", "acceptEdits",
    "--permission-prompts", "none",
    "--allowedTools", "Read", "Edit", "Write", "Glob", "Grep", "Bash",
    "--disallowedTools", "Agent", "Task", "WebFetch", "WebSearch", "Bash(git push:*)", "Bash(curl:*)",
    "Bash(wget:*)",
]
BASE_ARGS = ["-p", "--output-format", "json", "--setting-sources", "project", "--strict-mcp-config"]

# Environment a parent Claude Code / desktop session exports to its children. A nested `claude -p` that
# inherits them tries the parent's auth channel and its minimal mode (which skips project agents), so they
# are removed. Your own auth variables (ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, provider switches) stay.
HOST_ENV_EXACT = {
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_PREVIEW_CLASSIFIER_FLOOR", "BAGGAGE", "AI_AGENT",
    "CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH",
    "CLAUDE_CODE_OAUTH_SCOPES", "CLAUDE_CODE_ACCOUNT_UUID", "CLAUDE_CODE_ORGANIZATION_UUID",
    "CLAUDE_CODE_USER_EMAIL", "CLAUDE_CODE_DESKTOP_APP_VERSION", "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", "CLAUDE_CODE_ENABLE_ASK_USER_QUESTION_TOOL",
    "CLAUDE_CODE_REPORT_FINDINGS", "CLAUDE_CODE_EAGER_FLUSH", "CLAUDE_CODE_DISABLE_CRON",
    "CLAUDE_CODE_DISABLE_TERMINAL_TITLE", "CLAUDE_CODE_STARTUP_FAILURE_RESULTS", "CLAUDE_CODE_TERMINAL_MCP_TOOLS",
}
HOST_ENV_PREFIXES = ("CLAUDE_CODE_EMIT_", "CLAUDE_CODE_MESSAGING_", "CLAUDE_AGENT_SDK_")
HOST_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH")

RUN_FIELDS = [
    "task", "difficulty", "arm", "repeat", "runner", "step", "agent", "model", "session_id",
    "input_tokens", "cache_creation_tokens", "cache_read_tokens", "output_tokens", "total_tokens",
    "uncached_tokens", "cost_cli", "cost_list", "num_turns", "duration_s", "requests", "max_context",
    "haiku_over_100k", "transcript_tokens", "exit", "is_error", "contamination", "note", "attempt_id",
    "started_at_utc",
]
TASK_FIELDS = [
    "task", "difficulty", "arm", "repeat", "runner", "passed_first", "passed_final", "attempts", "escalated", "calls",
    "input_tokens", "cache_creation_tokens", "cache_read_tokens", "output_tokens", "total_tokens",
    "uncached_tokens", "cost_cli", "cost_list", "cache_hit", "max_context", "haiku_over_100k", "wall_s",
    "tests_edited", "contamination", "attempt_id", "finished_at_utc",
]
TOKEN_KEYS = ("input_tokens", "cache_creation_tokens", "cache_read_tokens", "output_tokens")
TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]*\.py$|_test\.(py|go)$|\.(test|spec)\.[cm]?[jt]sx?$")
GIT_HISTORY_RE = re.compile(r"\bgit\b[^|;&\n]*?\b(log|show|reflog|rev-list|blame|whatchanged)\b")
EXCLUDES = "__pycache__/\n*.pyc\n.pytest_cache/\nnode_modules/\n.venv/\n.bench/\n"
BENCH_DIR = ".bench"  # inside a run dir, git-excluded: failure log + diff written by `check` for the next handoff


class BenchError(RuntimeError):
    pass


# ---------------------------------------------------------------- small helpers

def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s) -> dt.datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def tail(text: str, n: int = LOG_TAIL_LINES) -> str:
    return "\n".join((text or "").splitlines()[-n:])


def rmtree(path: Path) -> None:
    path = Path(path)
    if not path.exists():
        return

    def _retry(func, p, *_):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_retry)
    else:  # pragma: no cover
        shutil.rmtree(path, onerror=_retry)


def split_cmd(cmd) -> list[str]:
    """`--claude-cmd` / TIER_BENCH_CLAUDE: a JSON list, or a command line."""
    if isinstance(cmd, list):
        return cmd
    cmd = cmd.strip()
    if cmd.startswith("["):
        return json.loads(cmd)
    if os.name == "nt":
        return [t[1:-1] if len(t) > 1 and t[0] == t[-1] == '"' else t for t in shlex.split(cmd, posix=False)]
    return shlex.split(cmd)


def resolve_claude_cmd(cmd: str | None) -> tuple[list[str], bool]:
    """Returns (argv prefix, is_default_cli)."""
    cmd = cmd or os.environ.get("TIER_BENCH_CLAUDE")
    if not cmd:
        exe = shutil.which("claude") or "claude"
        return [exe], True
    argv = split_cmd(cmd)
    found = shutil.which(argv[0])
    if found:
        argv = [found, *argv[1:]]
    return argv, False


def clean_env(base: dict | None = None) -> dict:
    env = dict(os.environ if base is None else base)
    if env.get("TIER_BENCH_KEEP_ENV") == "1":
        return env
    hosted = any(k in env for k in HOST_MARKERS)
    for k in list(env):
        if k in HOST_ENV_EXACT or k.startswith(HOST_ENV_PREFIXES):
            env.pop(k)
    if hosted:
        env.pop("ANTHROPIC_BASE_URL", None)  # the parent's own endpoint, not one you configured
    return env


def projects_dir(override: str | None = None) -> Path:
    if override:
        return Path(override)
    if os.environ.get("TIER_BENCH_PROJECTS_DIR"):
        return Path(os.environ["TIER_BENCH_PROJECTS_DIR"])
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects"
    return Path.home() / ".claude" / "projects"


# ---------------------------------------------------------------- prices

DEFAULT_PRICES = {
    "models": {
        "opus": {"input": 4, "output": 20},
        "sonnet": {"input": 2, "output": 10},
        "haiku": {"input": 0.10, "output": 0.50, "long_context_threshold_tokens": 100_000,
                  "long_input": 0.50, "long_output": 2.50},
        "fable": {"input": 10, "output": 50},
    },
    "cache": {"read_multiplier": 0.1, "write_5m_multiplier": 1.25, "write_1h_multiplier": 2},
}


def load_prices(path: str | os.PathLike | None = None) -> dict:
    p = Path(path) if path else DEFAULT_PRICES_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data.get("models"), dict) and data["models"]:
            data.setdefault("cache", DEFAULT_PRICES["cache"])
            return data
    except (OSError, ValueError):
        pass
    return DEFAULT_PRICES


def family(model: str) -> str:
    m = (model or "").lower()
    for fam in ("opus", "sonnet", "haiku", "fable"):
        if fam in m:
            return fam
    return ""


def request_cost(model: str, usage: dict, prices: dict, long_context: bool = True) -> float:
    """List-price cost of one API request (US$). long_context=False ignores the long-context price."""
    entry = prices["models"].get(family(model))
    if not entry:
        return 0.0
    cache = prices.get("cache", DEFAULT_PRICES["cache"])
    inp = int(usage.get("input_tokens") or 0)
    cw = int(usage.get("cache_creation_input_tokens") or 0)
    cr = int(usage.get("cache_read_input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    p_in, p_out = float(entry["input"]), float(entry["output"])
    threshold = entry.get("long_context_threshold_tokens")
    if long_context and threshold and inp + cw + cr > threshold:
        p_in = float(entry.get("long_input", p_in))
        p_out = float(entry.get("long_output", p_out))
    split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    cw_1h = min(int(split.get("ephemeral_1h_input_tokens") or 0), cw)
    cw_5m = cw - cw_1h  # every write not marked 1-hour is billed as a 5-minute write
    cost = (inp * p_in + cr * p_in * float(cache["read_multiplier"])
            + cw_5m * p_in * float(cache["write_5m_multiplier"])
            + cw_1h * p_in * float(cache["write_1h_multiplier"]) + out * p_out)
    return cost / 1_000_000


def is_over_cliff(model: str, usage: dict, prices: dict) -> bool:
    entry = prices["models"].get(family(model)) or {}
    threshold = entry.get("long_context_threshold_tokens")
    if not threshold:
        return False
    ctx = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                 "cache_read_input_tokens"))
    return ctx > threshold


# ---------------------------------------------------------------- claude output parsing

def parse_result(stdout: str) -> dict:
    text = (stdout or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    return data
            except ValueError:
                continue
    return {}


def result_tokens(res: dict) -> tuple[dict, list[str], float]:
    """Tokens (summed over models), model names and the CLI's cost from a `--output-format json` result."""
    tokens = dict.fromkeys(TOKEN_KEYS, 0)
    models = []
    model_usage = res.get("modelUsage") or {}
    if isinstance(model_usage, dict) and model_usage:
        for name, mu in model_usage.items():
            if name == SYNTHETIC_MODEL or not isinstance(mu, dict):
                continue
            models.append(name)
            tokens["input_tokens"] += int(mu.get("inputTokens") or mu.get("input_tokens") or 0)
            tokens["cache_creation_tokens"] += int(mu.get("cacheCreationInputTokens")
                                                   or mu.get("cache_creation_input_tokens") or 0)
            tokens["cache_read_tokens"] += int(mu.get("cacheReadInputTokens")
                                               or mu.get("cache_read_input_tokens") or 0)
            tokens["output_tokens"] += int(mu.get("outputTokens") or mu.get("output_tokens") or 0)
    else:
        u = res.get("usage") or {}
        tokens["input_tokens"] = int(u.get("input_tokens") or 0)
        tokens["cache_creation_tokens"] = int(u.get("cache_creation_input_tokens") or 0)
        tokens["cache_read_tokens"] = int(u.get("cache_read_input_tokens") or 0)
        tokens["output_tokens"] = int(u.get("output_tokens") or 0)
    cost = res.get("total_cost_usd")
    if cost is None and isinstance(model_usage, dict):
        cost = sum(float((mu or {}).get("costUSD") or 0) for mu in model_usage.values() if isinstance(mu, dict))
    return tokens, models, float(cost or 0)


def find_transcript(session_id: str, pdir: Path) -> Path | None:
    if not session_id or not pdir.is_dir():
        return None
    hits = sorted(pdir.glob(f"*/{session_id}.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


def read_transcript(path: Path | None, since: dt.datetime | None = None, skip_ids=()):
    """Assistant requests as (message.id, model, usage) - one per message.id, its LAST usage, because streaming
    transcripts repeat an id - plus the tool calls (name, input) and the final assistant text. Rows before
    `since` and ids in `skip_ids` (already counted) are left out."""
    requests: dict[str, tuple[str, dict]] = {}
    order: list[str] = []
    tools: dict[str, tuple[str, dict]] = {}
    texts: dict[str, list[str]] = {}
    last_text_id = ""
    if not path:
        return [], [], ""
    try:
        fh = open(path, encoding="utf-8-sig", errors="replace")
    except OSError:
        return [], [], ""
    skip = set(skip_ids)
    with fh:
        for n, line in enumerate(fh):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            msg = d.get("message")
            if not isinstance(msg, dict) or (d.get("type") != "assistant" and msg.get("role") != "assistant"):
                continue
            ts = parse_ts(d.get("timestamp"))
            if since and ts and ts < since:
                continue
            mid = msg.get("id") or d.get("uuid") or f"line{n}"
            if mid in skip:
                continue
            for c in msg.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    tools[c.get("id") or f"line{n}"] = (c.get("name") or "", c.get("input") or {})
                elif c.get("type") == "text" and str(c.get("text") or "").strip():
                    texts.setdefault(mid, []).append(c["text"])
                    last_text_id = mid
            model = msg.get("model") or ""
            if not model or model == SYNTHETIC_MODEL or not isinstance(msg.get("usage"), dict):
                continue
            if mid not in requests:
                order.append(mid)
            requests[mid] = (model, msg["usage"])
    final_text = "\n".join(texts.get(last_text_id, []))
    return [(k, *requests[k]) for k in order], list(tools.values()), final_text


def parse_transcript(path: Path | None, since: dt.datetime | None = None):
    """(model, usage) per request and the tool calls, since `since`."""
    reqs, tools, _ = read_transcript(path, since)
    return [(m, u) for _, m, u in reqs], tools


def _usage_total(u: dict) -> int:
    return sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                             "cache_read_input_tokens", "output_tokens"))


def transcript_stats(requests, prices) -> dict:
    """Totals over (model, usage) pairs. Requests with no tokens at all are not counted (as the usage logger)."""
    tot = dict.fromkeys(TOKEN_KEYS, 0)
    max_ctx = over = 0
    cost = 0.0
    requests = [(m, u) for m, u in requests if _usage_total(u) > 0]
    for model, u in requests:
        i = int(u.get("input_tokens") or 0)
        cw = int(u.get("cache_creation_input_tokens") or 0)
        cr = int(u.get("cache_read_input_tokens") or 0)
        o = int(u.get("output_tokens") or 0)
        tot["input_tokens"] += i
        tot["cache_creation_tokens"] += cw
        tot["cache_read_tokens"] += cr
        tot["output_tokens"] += o
        max_ctx = max(max_ctx, i + cw + cr)
        cost += request_cost(model, u, prices)
        if family(model) == "haiku" and is_over_cliff(model, u, prices):
            over += 1
    return {"tokens": tot, "max_context": max_ctx, "haiku_over_100k": over, "cost_list": cost,
            "requests": len(requests)}


def contamination(tool_calls, markers, run_dir: Path | None = None) -> str:
    """'' when clean; otherwise why: a tool call touched a forbidden path or read git history."""
    rd = str(run_dir).lower().replace("\\", "/") if run_dir else ""
    marks = [m.lower().replace("\\", "/") for m in markers if m]
    for name, inp in tool_calls:
        if not isinstance(inp, dict):
            continue
        texts = []
        if name in ("Bash", "PowerShell"):
            texts.append(str(inp.get("command") or ""))
        for key in ("file_path", "path", "notebook_path"):
            if isinstance(inp.get(key), str):
                texts.append(inp[key])
        if name == "Glob" and isinstance(inp.get("pattern"), str):
            texts.append(inp["pattern"])
        for t in texts:
            low = t.lower().replace("\\", "/")
            if rd:
                low = low.replace(rd, "")
            for m in marks:
                if m in low:
                    return f"path:{m}"
            if name in ("Bash", "PowerShell") and GIT_HISTORY_RE.search(t):
                return "git-history"
    return ""


def default_markers(repo: Path) -> list[str]:
    p = str(Path(repo).resolve())
    out = [p, p.replace("\\", "/")]
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    if m:  # Git Bash / MSYS spelling of the same path
        out.append(f"/{m.group(1).lower()}/{m.group(2)}".replace("\\", "/"))
    return out


# ---------------------------------------------------------------- git export

def git(repo, *args, binary=False, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if check and r.returncode != 0:
        raise BenchError(f"git {' '.join(args[:3])}: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def existing_paths(repo, rev: str, paths) -> list[str]:
    want = [p.replace("\\", "/").strip("/") for p in paths]
    out = git(repo, "ls-tree", "-r", "-z", "--name-only", rev, "--", *want)
    files = [f for f in out.split("\0") if f]
    return [p for p in want if any(f == p or f.startswith(p + "/") for f in files)]


def extract(repo, rev: str, paths, dest: Path) -> list[str]:
    keep = existing_paths(repo, rev, paths)
    if not keep:
        return []
    data = git(repo, "archive", "--format=tar", rev, "--", *keep, binary=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        try:
            tf.extractall(dest, filter="data")
        except TypeError:  # pragma: no cover  (Python < 3.12)
            tf.extractall(dest)
    return keep


def init_snapshot(d: Path) -> str:
    subprocess.run(["git", "init", "-q", str(d)], capture_output=True, check=True)
    for k, v in (("user.name", "tier-bench"), ("user.email", "tier-bench@localhost"),
                 ("core.autocrlf", "false"), ("core.safecrlf", "false")):
        git(d, "config", k, v)
    info = d / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "exclude").write_text(EXCLUDES, encoding="utf-8")
    git(d, "add", "-A")
    git(d, "commit", "-q", "--allow-empty", "-m", "base")
    return git(d, "rev-parse", "HEAD").strip()


def parse_agent_md(text: str) -> tuple[dict, str]:
    """Frontmatter (simple `key: value` lines) and body of a Claude Code agent file."""
    text = text.lstrip("﻿")
    m = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?(.*)$", text, re.S)
    if not m:
        return {}, text.strip()
    front = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            k, v = line.split(":", 1)
            front[k.strip()] = v.strip().strip('"').strip("'")
    return front, m.group(2).strip()


def agents_definition(agents_dir: Path) -> dict:
    """The worker agents as a `claude --agents` JSON object. Passed explicitly because a headless session in an
    untrusted folder may ignore the project's .claude/agents/."""
    out = {}
    for fname in AGENT_FILES:
        p = Path(agents_dir) / fname
        if not p.is_file():
            continue
        front, body = parse_agent_md(p.read_text(encoding="utf-8"))
        name = front.get("name") or fname[:-3]
        spec = {"description": front.get("description", name), "prompt": body}
        if front.get("tools"):
            spec["tools"] = [t.strip() for t in front["tools"].split(",") if t.strip()]
        if front.get("model"):
            spec["model"] = front["model"]
        out[name] = spec
    return out


def export(repo, task: dict, dest: Path, *, rev: str, tests_from: str | None = None,
           agents_dir: Path | None = None) -> str:
    """Fresh run dir: `rev`'s paths, the test files from `tests_from`, the worker agent files, one commit."""
    dest = Path(dest)
    rmtree(dest)
    dest.mkdir(parents=True)
    extract(repo, rev, task["paths"], dest)
    if tests_from:
        got = extract(repo, tests_from, task["test_files"], dest)
        missing = set(p.replace("\\", "/") for p in task["test_files"]) - set(got)
        if missing:
            raise BenchError(f"{task['id']}: test files not in {tests_from}: {sorted(missing)}")
    if agents_dir:
        target = dest / ".claude" / "agents"
        for name in AGENT_FILES:
            src = Path(agents_dir) / name
            if src.is_file():
                target.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, target / name)
    for cmd in task.get("setup") or []:
        subprocess.run(cmd, shell=True, cwd=dest, capture_output=True, timeout=ACCEPT_TIMEOUT_S)
    return init_snapshot(dest)


def run_shell(cmd: str, cwd: Path, timeout: int = ACCEPT_TIMEOUT_S) -> tuple[int, str]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, env=env)
        return r.returncode, r.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or b"").decode("utf-8", "replace") + f"\n[timeout after {timeout}s]"


def accept(task: dict, cwd: Path, timeout: int = ACCEPT_TIMEOUT_S) -> tuple[bool, str]:
    code, out = run_shell(task["accept"], cwd, timeout)
    log = f"$ {task['accept']}\n{out}\n[exit {code}]"
    ok = code == 0
    if task.get("regress"):
        rcode, rout = run_shell(task["regress"], cwd, timeout)
        log += f"\n$ {task['regress']}\n{rout}\n[exit {rcode}]"
        ok = ok and rcode == 0
    return ok, log


def changed_tests(d: Path, base_sha: str, test_files) -> list[str]:
    names = [n for n in git(d, "diff", "--name-only", "-z", base_sha, "--").split("\0") if n]
    tf = {p.replace("\\", "/") for p in test_files}
    return [n for n in names if n in tf or TEST_PATH_RE.search(n)]


def restore_tests(d: Path, base_sha: str, paths) -> None:
    if paths:
        git(d, "checkout", base_sha, "--", *paths, check=False)


def work_diff(d: Path, base_sha: str) -> str:
    git(d, "add", "-A", "-N", check=False)
    return git(d, "diff", base_sha, "--", ".", ":(exclude).claude", check=False)


def commit_stats(repo, base: str, commit: str, test_files, paths=()) -> tuple[int, int, list[str]]:
    """Changed non-test lines and files between base and commit (within the task's exported paths when given),
    plus the added non-test lines."""
    tf = {p.replace("\\", "/") for p in test_files}
    spec = ["--", *[p.replace("\\", "/") for p in paths]] if paths else []
    lines = files = 0
    for row in git(repo, "diff", "--numstat", base, commit, *spec).splitlines():
        parts = row.split("\t")
        if len(parts) != 3 or parts[2] in tf or TEST_PATH_RE.search(parts[2]):
            continue
        a, r = parts[0], parts[1]
        files += 1
        lines += (int(a) if a.isdigit() else 0) + (int(r) if r.isdigit() else 0)
    added, current = [], ""
    for row in git(repo, "diff", "-U0", base, commit, *spec).splitlines():
        if row.startswith("+++ "):
            current = row[6:] if row.startswith("+++ b/") else ""
        elif row.startswith("+") and current and current not in tf and not TEST_PATH_RE.search(current):
            added.append(row[1:])
    return lines, files, added


def prompt_leaks(prompt: str, added_lines, min_len: int = 30) -> int:
    """How many added solution lines (long ones) appear verbatim in the prompt."""
    return sum(1 for ln in set(a.strip() for a in added_lines) if len(ln) >= min_len and ln in prompt)


# ---------------------------------------------------------------- task file

REQUIRED = ("id", "difficulty", "commit", "base", "paths", "test_files", "accept", "prompt")


def load_tasks(path, only: str | None = None) -> tuple[list[dict], dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    tasks = data["tasks"] if isinstance(data, dict) else data
    meta = data if isinstance(data, dict) else {}
    for t in tasks:
        missing = [k for k in REQUIRED if k not in t]
        if missing:
            raise BenchError(f"task {t.get('id')}: missing {missing}")
        if t["difficulty"] not in ("easy", "medium"):
            raise BenchError(f"task {t['id']}: difficulty must be easy or medium")
    if only:
        keep = {x.strip() for x in only.split(",") if x.strip()}
        tasks = [t for t in tasks if t["id"] in keep]
    return tasks, meta


def find_repo(arg: str | None, tasks_path) -> Path:
    if arg:
        return Path(arg).resolve()
    for start in (Path.cwd(), Path(tasks_path).resolve().parent):
        r = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"], capture_output=True)
        if r.returncode == 0:
            return Path(r.stdout.decode().strip()).resolve()
    raise BenchError("not inside a git repository: pass --repo")


def default_run_root() -> Path:
    return Path(tempfile.gettempdir()) / "tier-bench"


# ---------------------------------------------------------------- validate

def validate_task(repo, task: dict, root: Path, keep: bool = False, timeout: int = ACCEPT_TIMEOUT_S) -> dict:
    t0 = time.time()
    row = {"id": task["id"], "difficulty": task["difficulty"], "base_exit": None, "commit_exit": None,
           "valid": False, "error": ""}
    try:
        parent = git(repo, "rev-parse", f"{task['commit']}^").strip()
        base_full = git(repo, "rev-parse", task["base"]).strip()
        row["parent_ok"] = parent == base_full
        lines, files, added = commit_stats(repo, task["base"], task["commit"], task["test_files"], task["paths"])
        row.update(lines=lines, files=files, leaks=prompt_leaks(task["prompt"], added))
        bdir, cdir = root / f"{task['id']}-base", root / f"{task['id']}-commit"
        export(repo, task, bdir, rev=task["base"], tests_from=task["commit"])
        ok_b, log_b = accept(task, bdir, timeout)
        export(repo, dict(task, paths=list(task["paths"]) + list(task["test_files"])), cdir, rev=task["commit"])
        ok_c, log_c = accept(task, cdir, timeout)
        row["base_exit"] = "PASS" if ok_b else "FAIL"
        row["commit_exit"] = "PASS" if ok_c else "FAIL"
        row["valid"] = (not ok_b) and ok_c
        if not row["valid"]:
            row["error"] = tail(log_c if not ok_c else log_b, 6).replace("\n", " | ")[:300]
        if not keep:
            rmtree(bdir)
            rmtree(cdir)
    except (BenchError, OSError, subprocess.SubprocessError) as e:
        row["error"] = str(e)[:300]
    row["secs"] = round(time.time() - t0, 1)
    return row


def validate_table(rows) -> str:
    out = ["| task | diff | lines | files | base+new tests | commit | valid | leaks | secs |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['id']} | {r['difficulty']} | {r.get('lines', '')} | {r.get('files', '')} | "
                   f"{r['base_exit'] or 'ERR'} | {r['commit_exit'] or 'ERR'} | {'yes' if r['valid'] else 'NO'} | "
                   f"{r.get('leaks', '')} | {r['secs']} |")
    bad = [r for r in rows if not r["valid"]]
    out.append(f"\n{len(rows) - len(bad)}/{len(rows)} valid")
    for r in bad:
        out.append(f"- {r['id']}: {r['error']}")
    return "\n".join(out)


def cmd_validate(a) -> int:
    tasks, _ = load_tasks(a.tasks, a.only)
    repo = find_repo(a.repo, a.tasks)
    root = Path(a.run_root) / "validate" if a.run_root else default_run_root() / "validate"
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        for row in ex.map(lambda t: validate_task(repo, t, root, a.keep), tasks):
            rows.append(row)
            print(f"{row['id']}: base={row['base_exit']} commit={row['commit_exit']} "
                  f"valid={row['valid']} ({row['secs']}s)", file=sys.stderr, flush=True)
    print(validate_table(rows))
    if a.json:
        Path(a.json).write_text(json.dumps({"validated_at_utc": iso(utcnow()), "rows": rows}, indent=1),
                                encoding="utf-8")
    return 0 if all(r["valid"] for r in rows) else 1


# ---------------------------------------------------------------- prompts

def scout_prompt(task: dict) -> str:
    return (
        "You are the scout for a builder who will make the change below. Do NOT edit any file.\n"
        "Locate what the change needs: the files, functions and line numbers to touch, the test file and what "
        "its failing tests expect, and any helper the code already has. Run the acceptance command once and "
        "summarise the failures (exact error lines). Report in at most 400 words with path:line evidence; no "
        "proposed code.\n\n--- TASK ---\n" + task["prompt"])


def builder_prompt(task: dict, scout_report: str = "") -> str:
    p = task["prompt"]
    if scout_report.strip():
        p += ("\n\n--- SCOUT REPORT (read-only findings from a cheaper model; verify before relying on it) ---\n"
              + scout_report[:SCOUT_REPORT_CHARS])
    p += ("\n\n--- RULES ---\nDo not edit the test files. Run the acceptance command before you finish. If you "
          "cannot make it pass, stop with `ESCALATE: <one line>` followed by the failure log.")
    return p


def escalate_prompt(task: dict, failure_log: str, diff: str) -> str:
    return (
        task["prompt"]
        + "\n\n--- ESCALATION ---\nA cheaper worker attempted this task and the acceptance check failed. Its "
        "changes are still in the working tree. Fix the task; do not edit the test files; run the acceptance "
        "command before you finish.\n\nFailure log (last 80 lines):\n```\n" + tail(failure_log)
        + "\n```\n\nThe worker's changes so far (git diff against the starting point):\n```diff\n"
        + (diff[:DIFF_CHARS] or "(no changes)") + "\n```")


def retry_prompt(task: dict, failure_log: str) -> str:
    return (f"The acceptance check `{task['accept']}` still fails after your change. Last 80 lines:\n```\n"
            + tail(failure_log) + "\n```\nFix it. Do not edit the test files. Run the acceptance command "
            "before you finish.")


# ---------------------------------------------------------------- run (shared by both runners)

STEPS = {
    "leader1": {"agent": None, "model": "opus", "effort": None},
    "leader2": {"agent": None, "model": "opus", "effort": None},
    "scout": {"agent": "haiku-scout", "model": "haiku", "effort": "medium"},
    "build": {"agent": "sonnet-builder", "model": "sonnet", "effort": None},
    "escalate": {"agent": "opus-architect", "model": "opus", "effort": None},
}
ARM_STEPS = {"leader": ("leader1", "leader2"), "tiered": ("scout", "build", "escalate"),
             "tiered-noscout": ("build", "escalate")}


class Ctx:
    def __init__(self, a, tasks_meta: dict):
        g = lambda name: getattr(a, name, None)  # noqa: E731  (subcommands carry different options)
        self.repo = find_repo(g("repo"), a.tasks)
        self.out_dir = Path(g("out_dir") or "bench-results")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.runs_csv = self.out_dir / "tier_bench_runs.csv"
        self.tasks_csv = self.out_dir / "tier_bench_tasks.csv"
        self.run_root = Path(g("run_root")) if g("run_root") else default_run_root()
        self.agents_dir = Path(g("agents_dir")) if g("agents_dir") else self.repo / ".claude" / "agents"
        self.claude, self.default_cli = resolve_claude_cmd(g("claude_cmd"))
        self.env = clean_env()
        self.pdir = projects_dir(g("projects_dir"))
        self.prices = load_prices(g("prices"))
        self.claude_timeout = g("claude_timeout") or CLAUDE_TIMEOUT_S
        self.accept_timeout = g("accept_timeout") or ACCEPT_TIMEOUT_S
        self.markers = default_markers(self.repo) + list(tasks_meta.get("contamination_markers") or [])
        self.lock = threading.Lock()
        self.agents_file: Path | None = None

    def write_agents_file(self) -> None:
        """One `--agents` JSON for the whole run, outside every run dir, from the CURRENT agent files."""
        spec = agents_definition(self.agents_dir)
        if spec:
            self.run_root.mkdir(parents=True, exist_ok=True)
            self.agents_file = self.run_root / "_agents.json"
            self.agents_file.write_text(json.dumps(spec, indent=1), encoding="utf-8")

    def append(self, path: Path, fields, row: dict) -> None:
        with self.lock:
            new = not path.exists() or path.stat().st_size == 0
            with open(path, "a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow(row)

    def names(self, task: dict, arm: str, rep: int) -> tuple[Path, Path]:
        name = f"{task['id']}-{arm}-{rep}"
        return self.run_root / name, self.run_root / "_logs" / name


def done_rows(tasks_csv: Path) -> set:
    if not tasks_csv.exists():
        return set()
    with open(tasks_csv, newline="", encoding="utf-8") as fh:
        return {(r.get("task"), r.get("arm"), str(r.get("repeat"))) for r in csv.DictReader(fh)}


def check_step(ctx: Ctx, task: dict, d: Path, base_sha: str, label: str, logdir: Path) -> tuple[bool, str, list]:
    """Restore any test file the worker touched, run accept (+regress), and leave the failure log and the
    worker's diff in <run dir>/.bench/ for the next handoff."""
    touched = changed_tests(d, base_sha, task["test_files"])
    restore_tests(d, base_sha, touched)
    ok, log = accept(task, d, ctx.accept_timeout)
    logdir.mkdir(parents=True, exist_ok=True)
    (logdir / f"{label}.accept.txt").write_text(log, encoding="utf-8")
    bench = d / BENCH_DIR
    bench.mkdir(exist_ok=True)
    (bench / "failure.log").write_text("" if ok else tail(log), encoding="utf-8")
    (bench / "diff.patch").write_text(work_diff(d, base_sha), encoding="utf-8")
    return ok, log, touched


def task_row(head: dict, calls: list, first: bool, final: bool, attempts: int, escalated: bool,
             tests_edited: bool, wall_s: float) -> dict:
    tok = {k: sum(int(c.get(k) or 0) for c in calls) for k in TOKEN_KEYS}
    total = sum(tok.values())
    denom = tok["input_tokens"] + tok["cache_creation_tokens"] + tok["cache_read_tokens"]
    cli = [c.get("cost_cli") for c in calls]
    return dict(head, passed_first=int(first), passed_final=int(final), attempts=attempts,
                escalated=int(escalated), calls=len(calls), **tok, total_tokens=total,
                uncached_tokens=total - tok["cache_read_tokens"],
                cost_cli="" if any(x in ("", None) for x in cli) else round(sum(float(x) for x in cli), 6),
                cost_list=round(sum(float(c.get("cost_list") or 0) for c in calls), 6),
                cache_hit=round(tok["cache_read_tokens"] / denom, 4) if denom else 0,
                max_context=max((int(c.get("max_context") or 0) for c in calls), default=0),
                haiku_over_100k=sum(int(c.get("haiku_over_100k") or 0) for c in calls),
                wall_s=round(wall_s, 1), tests_edited=int(bool(tests_edited)),
                contamination=";".join(sorted({c["contamination"] for c in calls if c.get("contamination")})),
                finished_at_utc=iso(utcnow()))


# ---------------------------------------------------------------- runner: cli (headless claude -p)

def call_claude(ctx: Ctx, d: Path, head: dict, *, step: str, model: str, prompt: str, agent: str | None = None,
                effort: str | None = None, resume: str | None = None, prev: dict | None = None,
                logdir: Path | None = None) -> dict:
    argv = [*ctx.claude, *BASE_ARGS, "--model", model]
    if agent:
        if ctx.agents_file:
            argv += ["--agents", str(ctx.agents_file)]
        argv += ["--agent", agent]
    if effort:
        argv += ["--effort", effort]
    if resume:
        argv += ["--resume", resume]
    argv += PERMISSION_ARGS
    started = utcnow()
    t0 = time.time()
    try:
        p = subprocess.run(argv, cwd=d, input=prompt.encode("utf-8"), capture_output=True,
                           timeout=ctx.claude_timeout, env=ctx.env)
        code, out, err = p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        code, out = 124, (e.stdout or b"").decode("utf-8", "replace")
        err = f"timeout after {ctx.claude_timeout}s"
    wall = time.time() - t0
    if logdir:
        logdir.mkdir(parents=True, exist_ok=True)
        (logdir / f"{step}.prompt.txt").write_text(prompt, encoding="utf-8")
        (logdir / f"{step}.json").write_text(out, encoding="utf-8")
        if err:
            (logdir / f"{step}.stderr.txt").write_text(err, encoding="utf-8")
    res = parse_result(out)
    tokens, models, cost_cli = result_tokens(res)
    sid = res.get("session_id") or ""
    # A resumed session's transcript also holds the earlier call's messages (same file, or copied into a new one):
    # count only message ids the previous call didn't.
    skip = prev.get("_ids", []) if (resume and prev) else ()
    reqs, tool_calls, _ = read_transcript(find_transcript(sid, ctx.pdir), since=started - dt.timedelta(seconds=2),
                                          skip_ids=skip)
    requests = [(m, u) for _, m, u in reqs]
    ts = transcript_stats(requests, ctx.prices)
    note = []
    if resume and prev and requests:
        # A resumed session may report totals that include the earlier call; keep only this call's share.
        prev_total = sum(int(prev.get(k) or 0) for k in TOKEN_KEYS)
        cur_total, new_total = sum(tokens.values()), sum(ts["tokens"].values())
        if prev_total and cur_total > new_total and abs(cur_total - prev_total - new_total) <= 0.1 * cur_total:
            tokens = {k: max(tokens[k] - int(prev.get(k) or 0), 0) for k in TOKEN_KEYS}
            cost_cli = max(cost_cli - float(prev.get("cost_cli") or 0), 0.0)
            note.append("resume-totals-were-cumulative")
    if not any(tokens.values()) and requests:
        tokens = dict(ts["tokens"])
        note.append("tokens-from-transcript")
    if not requests and sid:
        note.append("no-transcript")
    if not models:
        models = sorted({m for m, _ in requests})
    main_models = {m for m, _ in requests} or set(models)
    if main_models and not any(family(m) == family(model) for m in main_models):
        note.append("model-mismatch")  # the session did not run on the model this step asked for
    if requests:
        cost_list = ts["cost_list"]
    else:  # no per-request detail: price the totals at the first model's base (short-context) rate
        cost_list = request_cost(models[0] if models else "", {
            "input_tokens": tokens["input_tokens"], "cache_creation_input_tokens": tokens["cache_creation_tokens"],
            "cache_read_input_tokens": tokens["cache_read_tokens"], "output_tokens": tokens["output_tokens"],
        }, ctx.prices, long_context=False)
    total = sum(tokens.values())
    row = dict(head, runner="cli", step=step, agent=agent or "", model="+".join(models), session_id=sid, **tokens,
               total_tokens=total, uncached_tokens=total - tokens["cache_read_tokens"],
               cost_cli=round(cost_cli, 6), cost_list=round(cost_list, 6),
               num_turns=res.get("num_turns", ""), duration_s=round((res.get("duration_ms") or wall * 1000) / 1000, 1),
               requests=ts["requests"], max_context=ts["max_context"], haiku_over_100k=ts["haiku_over_100k"],
               transcript_tokens=sum(ts["tokens"].values()), exit=code,
               is_error=int(bool(res.get("is_error")) or code != 0 or not res),
               contamination=contamination(tool_calls, ctx.markers, d), note=";".join(note),
               started_at_utc=iso(started))
    ctx.append(ctx.runs_csv, RUN_FIELDS, row)
    row["_result"] = res.get("result") or ""
    row["_ids"] = list(skip) + [mid for mid, _, _ in reqs]
    return row


def run_task(ctx: Ctx, task: dict, arm: str, rep: int) -> dict:
    if arm not in ARM_STEPS:
        raise BenchError(f"unknown arm {arm}")
    d, logdir = ctx.names(task, arm, rep)
    rmtree(logdir)
    t0 = time.time()
    base_sha = export(ctx.repo, task, d, rev=task["base"], tests_from=task["commit"], agents_dir=ctx.agents_dir)
    head = {"task": task["id"], "difficulty": task["difficulty"], "arm": arm, "repeat": rep,
            "runner": "cli", "attempt_id": uuid.uuid4().hex[:12]}
    calls: list[dict] = []
    edited: set[str] = set()

    def check(label: str) -> tuple[bool, str]:
        ok, log, touched = check_step(ctx, task, d, base_sha, label, logdir)
        edited.update(touched)
        return ok, log

    def call(step: str, prompt: str, **kw) -> dict:
        spec = STEPS[step]
        r = call_claude(ctx, d, head, step=step, model=spec["model"], agent=spec["agent"], effort=spec["effort"],
                        prompt=prompt, logdir=logdir, **kw)
        calls.append(r)
        return r

    escalated = False
    if arm == "leader":
        r1 = call("leader1", task["prompt"])
        first, log = check("leader1")
        final, attempts = first, 1
        if not first:
            if r1["session_id"]:
                call("leader2", retry_prompt(task, log), resume=r1["session_id"], prev=r1)
            else:  # the first call died before a session existed: start over with the failure log
                call("leader2", task["prompt"] + "\n\n" + retry_prompt(task, log))
            final, _ = check("leader2")
            attempts = 2
    else:
        report = ""
        if arm == "tiered" and task["difficulty"] == "medium":
            report = call("scout", scout_prompt(task))["_result"][:SCOUT_REPORT_CHARS]
        call("build", builder_prompt(task, report))
        first, log = check("build")
        final, attempts = first, 1
        if not first:
            call("escalate", escalate_prompt(task, log, (d / BENCH_DIR / "diff.patch").read_text(encoding="utf-8")))
            final, _ = check("escalate")
            attempts, escalated = 2, True
    row = task_row(head, calls, first, final, attempts, escalated, bool(edited), time.time() - t0)
    ctx.append(ctx.tasks_csv, TASK_FIELDS, row)
    return row


# ---------------------------------------------------------------- runner: subagent (prepare / check / record)
# For a leader session that orchestrates its own subagents instead of headless CLI calls: `prepare` exports the
# run dir and prints the handoff prompt for a step, the leader spawns the matching subagent, `record` reads that
# subagent's transcript, `check` runs the acceptance command. Same CSVs, runner=subagent.

def _load_usage_logger():
    """The package's usage logger (hooks/log_usage.py): its transcript parser is reused for subagent records."""
    p = HERE.parent / "hooks" / "log_usage.py"
    if not p.is_file():
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("tier_bench_log_usage", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod if hasattr(mod, "parse_transcript") else None
    except Exception:
        return None


def workspace_preamble(d: Path, task: dict) -> str:
    return (f"WORKSPACE: {d}\nWork only inside this folder. Use absolute paths under it for every file tool and "
            f"start every shell command with `cd \"{d}\" && `. Do not read, search or run anything outside it, and "
            f"do not use git history commands. The acceptance command runs from that folder: {task['accept']}\n\n")


def _state_path(logdir: Path) -> Path:
    return logdir / "state.json"


def load_state(logdir: Path) -> dict | None:
    try:
        return json.loads(_state_path(logdir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_state(logdir: Path, state: dict) -> None:
    logdir.mkdir(parents=True, exist_ok=True)
    tmp = _state_path(logdir).with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, _state_path(logdir))


def _first_and_second(arm: str) -> tuple[str, str]:
    return ("leader1", "leader2") if arm == "leader" else ("build", "escalate")


def next_step(state: dict, task: dict) -> str | None:
    """The step the leader should hand out next, or None while a check is pending / when finished."""
    arm, steps, checks = state["arm"], state["steps"], state["checks"]
    first, second = _first_and_second(arm)
    if arm == "tiered" and task["difficulty"] == "medium" and not steps.get("scout", {}).get("row"):
        return "scout"
    if first not in steps:
        return first
    if first not in checks or checks[first]:
        return None
    if second not in steps:
        return second
    return None


def step_prompt(state: dict, task: dict, step: str, d: Path, scout_report: str | None = None) -> str:
    bench = d / BENCH_DIR
    failure = (bench / "failure.log").read_text(encoding="utf-8") if (bench / "failure.log").exists() else ""
    if step == "leader1":
        body = task["prompt"]
    elif step == "leader2":
        body = retry_prompt(task, failure)
    elif step == "scout":
        body = scout_prompt(task)
    elif step == "build":
        report = scout_report if scout_report is not None else state.get("scout_report", "")
        body = builder_prompt(task, report)
    else:
        diff = (bench / "diff.patch").read_text(encoding="utf-8") if (bench / "diff.patch").exists() else ""
        body = escalate_prompt(task, failure, diff)
    return workspace_preamble(d, task) + body


def prepare(ctx: Ctx, task: dict, arm: str, rep: int, step: str | None = None, fresh: bool = False,
            scout_report: str | None = None) -> dict:
    if arm not in ARM_STEPS:
        raise BenchError(f"unknown arm {arm}")
    if (task["id"], arm, str(rep)) in done_rows(ctx.tasks_csv):
        raise BenchError(f"{task['id']} {arm} #{rep} is already in {ctx.tasks_csv}")
    d, logdir = ctx.names(task, arm, rep)
    state = None if fresh else load_state(logdir)
    if state is None or not d.is_dir():
        rmtree(logdir)
        base_sha = export(ctx.repo, task, d, rev=task["base"], tests_from=task["commit"], agents_dir=ctx.agents_dir)
        state = {"task": task["id"], "arm": arm, "repeat": rep, "difficulty": task["difficulty"],
                 "attempt_id": uuid.uuid4().hex[:12], "base_sha": base_sha, "run_dir": str(d),
                 "started": time.time(), "steps": {}, "checks": {}, "tests_edited": [], "counted": {},
                 "finished": False}
    if state.get("finished"):
        raise BenchError(f"{task['id']} {arm} #{rep} is finished")
    step = step or next_step(state, task)
    if not step:
        raise BenchError("nothing to prepare: run `check` for the last step first")
    if step not in ARM_STEPS[arm]:
        raise BenchError(f"step {step} is not part of arm {arm}")
    if step == "scout" and task["difficulty"] != "medium":
        raise BenchError("the scout step runs on medium tasks only")
    first, second = _first_and_second(arm)
    if step == "build" and next_step(state, task) == "scout" and scout_report is None:
        raise BenchError("record the scout step first (or pass --scout-report)")
    if step == second and state["checks"].get(first) is not False:
        raise BenchError(f"{second} only follows a FAILED check of {first}")
    prompt = step_prompt(state, task, step, d, scout_report)
    logdir.mkdir(parents=True, exist_ok=True)
    pfile = logdir / f"{step}.prompt.txt"
    pfile.write_text(prompt, encoding="utf-8")
    state["steps"].setdefault(step, {})["prepared"] = iso(utcnow())
    save_state(logdir, state)
    spec = STEPS[step]
    if step == "leader2":
        spawn = "continue the leader1 subagent (same conversation) with this prompt"
    elif spec["agent"]:
        spawn = f"subagent_type={spec['agent']} model={spec['model']}"
    else:
        spawn = f"subagent_type=general-purpose model={spec['model']}"
    return {"run_dir": str(d), "step": step, "agent": spec["agent"] or "", "model": spec["model"],
            "effort": spec["effort"] or "", "spawn": spawn, "prompt_file": str(pfile), "prompt": prompt}


def maybe_finalize(ctx: Ctx, task: dict, state: dict, logdir: Path) -> dict | None:
    """Append the task row once the outcome is known and every handed-out step has been recorded."""
    if state.get("finished"):
        return None
    first, second = _first_and_second(state["arm"])
    checks = state["checks"]
    if first not in checks:
        return None
    if checks[first]:
        final, attempts = True, 1
    elif second in checks:
        final, attempts = checks[second], 2
    else:
        return None
    steps = state["steps"]
    if any(not s.get("row") for s in steps.values()):
        return None
    calls = [steps[s]["row"] for s in ARM_STEPS[state["arm"]] if s in steps]
    head = {"task": state["task"], "difficulty": state["difficulty"], "arm": state["arm"],
            "repeat": state["repeat"], "runner": "subagent", "attempt_id": state["attempt_id"]}
    row = task_row(head, calls, bool(checks[first]), bool(final), attempts,
                   state["arm"] != "leader" and attempts == 2, bool(state["tests_edited"]),
                   time.time() - float(state["started"]))
    ctx.append(ctx.tasks_csv, TASK_FIELDS, row)
    state["finished"] = True
    save_state(logdir, state)
    return row


def check_cmd(ctx: Ctx, task: dict, arm: str, rep: int, step: str) -> tuple[bool, str, dict | None]:
    d, logdir = ctx.names(task, arm, rep)
    state = load_state(logdir)
    if not state:
        raise BenchError(f"no prepared run for {task['id']} {arm} #{rep}: run `prepare` first")
    if step not in state["steps"]:
        raise BenchError(f"step {step} was not prepared")
    if step == "scout":
        raise BenchError("the scout step has no acceptance check")
    ok, log, touched = check_step(ctx, task, d, state["base_sha"], step, logdir)
    state["checks"][step] = ok
    state["tests_edited"] = sorted(set(state["tests_edited"]) | set(touched))
    save_state(logdir, state)
    return ok, log, maybe_finalize(ctx, task, state, logdir)


def transcript_span(path: Path, skip_ids=()) -> str:
    """Seconds from the first to the last timestamped line of this step (requests already counted are skipped)."""
    import datetime as _dt
    stamps = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        mid = (d.get("message") or {}).get("id") if isinstance(d.get("message"), dict) else None
        t = d.get("timestamp")
        try:
            when = _dt.datetime.fromisoformat(t.replace("Z", "+00:00")) if t else None
        except ValueError:
            when = None
        if when:
            stamps.append((when, bool(mid and mid in skip_ids)))
    cutoff = max((w for w, old in stamps if old), default=None)  # a continued conversation: only the new part
    new = [w for w, old in stamps if not old and (cutoff is None or w > cutoff)]
    return f"{(max(new) - min(new)).total_seconds():.1f}" if len(new) > 1 else ""


def record_cmd(ctx: Ctx, task: dict, arm: str, rep: int, step: str, transcript: Path) -> dict:
    d, logdir = ctx.names(task, arm, rep)
    state = load_state(logdir)
    if not state:
        raise BenchError(f"no prepared run for {task['id']} {arm} #{rep}: run `prepare` first")
    if step not in state["steps"]:
        raise BenchError(f"step {step} was not prepared")
    if state["steps"][step].get("row"):
        raise BenchError(f"step {step} is already recorded")
    if not Path(transcript).is_file():
        raise BenchError(f"transcript not found: {transcript}")
    key = str(Path(transcript).resolve())
    seen = state["counted"].get(key, [])
    reqs, tool_calls, final_text = read_transcript(Path(transcript), skip_ids=seen)
    pairs = [(m, u) for _, m, u in reqs]
    ts = transcript_stats(pairs, ctx.prices)
    tokens, max_ctx, cost_list, n_req = ts["tokens"], ts["max_context"], ts["cost_list"], ts["requests"]
    note = []
    logger = _load_usage_logger() if not seen else None
    if logger:  # the same parser the usage hook uses, so both logs agree
        t = logger.parse_transcript(str(transcript), ctx.prices)
        tokens = {"input_tokens": t["input"], "cache_creation_tokens": t["cc"], "cache_read_tokens": t["cr"],
                  "output_tokens": t["out"]}
        max_ctx, cost_list, n_req = t["max_ctx"], t["cost"], t["requests"]
        note.append("parsed-by-usage-logger")
    elif seen:  # a continued conversation (leader2): only the requests after the earlier record count
        note.append(f"continued-transcript:skipped-{len(seen)}-earlier-requests")
    models = sorted({m for m, _ in pairs})
    spec = STEPS[step]
    if models and not any(family(m) == family(spec["model"]) for m in models):
        note.append("model-mismatch")
    total = sum(tokens.values())
    sid = Path(transcript).stem
    sid = sid[len("agent-"):] if sid.startswith("agent-") else sid
    head = {"task": state["task"], "difficulty": state["difficulty"], "arm": arm, "repeat": rep,
            "runner": "subagent", "attempt_id": state["attempt_id"]}
    row = dict(head, step=step, agent=spec["agent"] or "", model="+".join(models), session_id=sid, **tokens,
               total_tokens=total, uncached_tokens=total - tokens["cache_read_tokens"], cost_cli="",
               cost_list=round(cost_list, 6), num_turns="", duration_s=transcript_span(Path(transcript), seen), requests=n_req, max_context=max_ctx,
               haiku_over_100k=ts["haiku_over_100k"], transcript_tokens=sum(ts["tokens"].values()), exit="",
               is_error=0, contamination=contamination(tool_calls, ctx.markers, d), note=";".join(note),
               started_at_utc=state["steps"][step].get("prepared", ""))
    ctx.append(ctx.runs_csv, RUN_FIELDS, row)
    state["steps"][step]["row"] = row
    state["counted"][key] = seen + [mid for mid, _, _ in reqs]
    if step == "scout":
        state["scout_report"] = final_text[:SCOUT_REPORT_CHARS]
    save_state(logdir, state)
    maybe_finalize(ctx, task, state, logdir)
    return row


def _one_task(a) -> tuple[Ctx, dict]:
    tasks, meta = load_tasks(a.tasks, a.task)
    if not tasks:
        raise BenchError(f"no task {a.task} in {a.tasks}")
    return Ctx(a, meta), tasks[0]


def cmd_prepare(a) -> int:
    ctx, task = _one_task(a)
    report = Path(a.scout_report).read_text(encoding="utf-8") if a.scout_report else None
    out = prepare(ctx, task, a.arm, a.rep, a.step, a.fresh, report)
    if a.json:
        print(json.dumps(out, indent=1))
    else:
        print(f"RUN_DIR: {out['run_dir']}\nSTEP: {out['step']}\nSPAWN: {out['spawn']}\n"
              f"PROMPT_FILE: {out['prompt_file']}\n--- PROMPT ---\n{out['prompt']}")
    return 0


def cmd_check(a) -> int:
    ctx, task = _one_task(a)
    ok, log, row = check_cmd(ctx, task, a.arm, a.rep, a.step)
    print("PASS" if ok else "FAIL")
    print(tail(log))
    if row:
        print(f"task-run finished: first={row['passed_first']} final={row['passed_final']} "
              f"tokens={row['total_tokens']}", file=sys.stderr)
    return 0 if ok else 1


def cmd_record(a) -> int:
    ctx, task = _one_task(a)
    row = record_cmd(ctx, task, a.arm, a.rep, a.step, Path(a.transcript))
    print(json.dumps({k: row[k] for k in ("step", "model", "total_tokens", "cost_list", "requests", "max_context",
                                          "contamination", "note")}))
    return 0


def auth_status(claude: list[str], env: dict) -> dict:
    try:
        r = subprocess.run([*claude, "auth", "status"], capture_output=True, timeout=60, env=env)
        return parse_result(r.stdout.decode("utf-8", "replace"))
    except (OSError, subprocess.SubprocessError):
        return {}


def headless_auth_ok(claude: list[str], env: dict) -> tuple[bool, str]:
    if env.get("ANTHROPIC_API_KEY") or env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True, "auth from environment variable"
    st = auth_status(claude, env)
    if st.get("loggedIn"):
        return True, f"logged in ({st.get('authMethod', '?')})"
    return False, ("the claude CLI is not logged in for headless use: run `claude auth login` once in a "
                   "terminal, or create a token with `claude setup-token` and export CLAUDE_CODE_OAUTH_TOKEN")


def cmd_run(a) -> int:
    tasks, meta = load_tasks(a.tasks, a.only)
    arms = [x.strip() for x in a.arms.split(",") if x.strip()]
    for arm in arms:
        if arm not in ARMS:
            raise BenchError(f"unknown arm {arm} (choose from {', '.join(ARMS)})")
    ctx = Ctx(a, meta)
    if a.valid_from:
        rows = json.loads(Path(a.valid_from).read_text(encoding="utf-8"))["rows"]
        valid = {r["id"] for r in rows if r.get("valid")}
        skipped = [t["id"] for t in tasks if t["id"] not in valid]
        if skipped:
            print(f"skipping tasks that did not validate: {', '.join(skipped)}", file=sys.stderr)
        tasks = [t for t in tasks if t["id"] in valid]
    if ctx.default_cli and not a.skip_preflight:
        ok, why = headless_auth_ok(ctx.claude, ctx.env)
        if not ok:
            print(f"preflight: {why}", file=sys.stderr)
            return 2
    missing = [n for n in AGENT_FILES if not (ctx.agents_dir / n).is_file()]
    if missing and any(arm != "leader" for arm in arms):
        raise BenchError(f"agent files missing in {ctx.agents_dir}: {missing}")
    done = done_rows(ctx.tasks_csv)
    work = [(t, arm, rep) for rep in range(1, a.repeats + 1) for t in tasks for arm in arms
            if (t["id"], arm, str(rep)) not in done]
    print(f"{len(work)} task-runs to do ({len(done)} already in {ctx.tasks_csv})", file=sys.stderr)
    ctx.run_root.mkdir(parents=True, exist_ok=True)
    ctx.write_agents_file()

    def one(item):
        t, arm, rep = item
        try:
            row = run_task(ctx, t, arm, rep)
            print(f"{t['id']} {arm} #{rep}: first={row['passed_first']} final={row['passed_final']} "
                  f"tokens={row['total_tokens']} cost_cli={row['cost_cli']}", file=sys.stderr, flush=True)
            return True
        except Exception as e:  # keep the other runs going; this row stays undone and is retried next time
            print(f"{t['id']} {arm} #{rep}: ERROR {e}", file=sys.stderr, flush=True)
            return False

    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        results = list(ex.map(one, work))
    return 0 if all(results) else 1


def cmd_doctor(a) -> int:
    claude, default = resolve_claude_cmd(a.claude_cmd)
    env = clean_env()
    print(f"claude command : {' '.join(claude)}")
    try:
        v = subprocess.run([*claude, "--version"], capture_output=True, timeout=60, env=env)
        print(f"version        : {v.stdout.decode('utf-8', 'replace').strip()}")
    except OSError as e:
        print(f"version        : not runnable ({e})")
        return 2
    ok, why = headless_auth_ok(claude, env) if default else (True, "custom --claude-cmd (not checked)")
    print(f"headless auth  : {'OK' if ok else 'NO'} - {why}")
    print(f"projects dir   : {projects_dir(a.projects_dir)}")
    agents = Path(a.agents_dir) if a.agents_dir else Path.cwd() / ".claude" / "agents"
    for n in AGENT_FILES:
        print(f"agent {n:<18}: {'found' if (agents / n).is_file() else 'MISSING'} ({agents})")
    print("permission args: " + " ".join(shlex.quote(x) for x in PERMISSION_ARGS))
    return 0 if ok else 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--repo", help="git repository the tasks come from (default: the current one)")
        p.add_argument("--run-root", help="parent folder for run dirs (default: <temp>/tier-bench); keep it "
                                          "outside the repository")
        p.add_argument("--only", help="comma-separated task ids")
        p.add_argument("--jobs", type=int, default=1)

    v = sub.add_parser("validate", help="base+new tests must fail, commit must pass")
    v.add_argument("--tasks", required=True)
    common(v)
    v.add_argument("--keep", action="store_true", help="keep the validation run dirs")
    v.add_argument("--json", help="also write the rows to this JSON file")
    v.set_defaults(func=cmd_validate)

    r = sub.add_parser("run", help="run the arms (resumable)")
    r.add_argument("--tasks", required=True)
    common(r)
    r.add_argument("--arms", default="leader,tiered")
    r.add_argument("--repeats", type=int, default=2)
    r.add_argument("--out-dir", default="bench-results")
    r.add_argument("--agents-dir", help="folder with the worker agent files (default: <repo>/.claude/agents)")
    r.add_argument("--claude-cmd", help="command used instead of `claude` (JSON list or command line); "
                                        "also env TIER_BENCH_CLAUDE")
    r.add_argument("--projects-dir", help="where claude saves session transcripts (default ~/.claude/projects)")
    r.add_argument("--prices", help=f"prices JSON (default {DEFAULT_PRICES_FILE.name} next to the package)")
    r.add_argument("--valid-from", help="JSON written by `validate --json`; tasks not valid there are skipped")
    r.add_argument("--claude-timeout", type=int, default=CLAUDE_TIMEOUT_S)
    r.add_argument("--accept-timeout", type=int, default=ACCEPT_TIMEOUT_S)
    r.add_argument("--skip-preflight", action="store_true", help="don't check `claude auth status` first")
    r.set_defaults(func=cmd_run)

    def step_args(p, with_step_positional: bool):
        p.add_argument("task")
        p.add_argument("arm", choices=ARMS)
        p.add_argument("rep", type=int)
        if with_step_positional:
            p.add_argument("step", choices=tuple(STEPS))
        p.add_argument("--tasks", required=True)
        p.add_argument("--repo")
        p.add_argument("--run-root")
        p.add_argument("--out-dir", default="bench-results")
        p.add_argument("--agents-dir")
        p.add_argument("--prices")
        p.add_argument("--accept-timeout", type=int, default=ACCEPT_TIMEOUT_S)

    pp = sub.add_parser("prepare", help="subagent runner: export the run dir and print the step's handoff prompt")
    step_args(pp, False)
    pp.add_argument("--step", choices=tuple(STEPS), help="default: the next step for this arm")
    pp.add_argument("--scout-report", help="file with the scout's report (default: the recorded scout step)")
    pp.add_argument("--fresh", action="store_true", help="start this task-run over")
    pp.add_argument("--json", action="store_true")
    pp.set_defaults(func=cmd_prepare)

    pc = sub.add_parser("check", help="subagent runner: restore tests, run accept/regress, PASS/FAIL")
    step_args(pc, True)
    pc.set_defaults(func=cmd_check)

    pr = sub.add_parser("record", help="subagent runner: log a subagent's usage from its transcript")
    step_args(pr, False)
    pr.add_argument("--step", required=True, choices=tuple(STEPS))
    pr.add_argument("--transcript", required=True, help="the subagent's agent-<id>.jsonl")
    pr.set_defaults(func=cmd_record)

    d = sub.add_parser("doctor", help="check the claude CLI can run headless (no model call)")
    d.add_argument("--claude-cmd")
    d.add_argument("--projects-dir")
    d.add_argument("--agents-dir")
    d.set_defaults(func=cmd_doctor)
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.func(a)
    except BenchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
