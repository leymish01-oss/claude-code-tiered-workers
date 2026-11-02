#!/usr/bin/env python3
"""Log what each Claude Code subagent used: one CSV row per subagent run (stdlib only).

  --hook                       read a SubagentStop hook JSON from stdin and log that run. Never fails the hook:
                               any error is swallowed, exit code 0, nothing printed.
  --scan SESSION_DIR           backfill every <SESSION_DIR>/subagents/agent-*.jsonl that is not logged yet.
  --outcome AGENT_ID pass|fail [--check "CMD"]
                               record whether the run's executable acceptance check passed.
  --summary [--since DATE]     totals per agent type / model: requests, tokens, cache hit, list-price cost, pass/fail.
  --csv FILE                   CSV to use (default: DEFAULT_CSV below, under $CLAUDE_PROJECT_DIR or the cwd).
  --prices FILE                prices.json to use (default: the one next to this script, else built-in copy).

Token definitions (same everywhere):
  total_tokens    = input + cache_creation + cache_read + output
  uncached_tokens = input + cache_creation + output
  cache_hit       = cache_read / (input + cache_creation + cache_read)
  max_context     = the largest single request's input + cache_creation + cache_read
A streaming transcript repeats one message id on several lines: each message.id counts once (its last usage).
Cost is a LIST-price estimate from prices.json, not an invoice.
"""
import argparse
import contextlib
import csv
import datetime
import glob
import io
import json
import os
import re
import sys
import time

DEFAULT_CSV = "agent_tiers.csv"

COLUMNS = [
    "logged_at_utc", "session_id", "agent_id", "agent_type", "description", "models", "requests",
    "input_tokens", "cache_creation_tokens", "cache_read_tokens", "output_tokens", "total_tokens",
    "cache_hit", "max_context_tokens", "cost_usd_list", "outcome", "check",
]

# Built-in copy of prices.json, used only when no prices.json can be found (a test keeps the two equal).
DEFAULT_PRICES = {
    "models": {
        "opus": {"input": 4, "output": 20},
        "sonnet": {"input": 2, "output": 10},
        "haiku": {"input": 0.10, "output": 0.50, "long_context_threshold_tokens": 100000,
                  "long_input": 0.50, "long_output": 2.50},
        "fable": {"input": 10, "output": 50},
    },
    "cache": {"read_multiplier": 0.1, "write_5m_multiplier": 1.25, "write_1h_multiplier": 2},
}


# ---------------------------------------------------------------- small helpers
def _n(x):
    return int(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0


def _trim(text, limit=120):
    return " ".join(str(text or "").split())[:limit]


def project_root(payload=None):
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        m = re.match(r"^/([a-zA-Z])/(.*)$", env) if os.name == "nt" else None  # Git Bash style /c/Users/...
        return "%s:/%s" % (m.group(1), m.group(2)) if m else env
    if isinstance(payload, dict) and payload.get("cwd"):
        return str(payload["cwd"])
    return os.getcwd()


def csv_path(arg, payload=None):
    return arg if arg else os.path.join(project_root(payload), DEFAULT_CSV)


def load_prices(explicit=None):
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        explicit,
        os.environ.get("AGENT_TIER_PRICES"),
        os.path.join(here, "prices.json"),
        os.path.join(here, "..", "prices.json"),
        os.path.join(here, "..", "products", "008-tiered-workers", "prices.json"),
        os.path.join(project_root(), "prices.json"),
    ]
    for c in candidates:
        if not c:
            continue
        try:
            with open(c, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("models"), dict):
                return data
        except (OSError, ValueError):
            continue
    return DEFAULT_PRICES


def family(prices, model):
    name = str(model or "").lower()
    for key in prices.get("models", {}):
        if key.lower() in name:
            return key
    return None


def _cache_split(usage):
    total = _n(usage.get("cache_creation_input_tokens"))
    br = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    cc1 = min(_n(br.get("ephemeral_1h_input_tokens")), total)
    cc5 = max(0, total - cc1)  # everything that is not marked 1-hour is billed as a 5-minute write
    return cc5, cc1


def request_cost(prices, model, usage):
    """List-price US$ for one request. Unknown model -> 0.0."""
    fam = family(prices, model)
    if not fam:
        return 0.0
    p = prices["models"][fam]
    inp, out = float(p.get("input", 0)), float(p.get("output", 0))
    i, cr, o = _n(usage.get("input_tokens")), _n(usage.get("cache_read_input_tokens")), _n(usage.get("output_tokens"))
    cc5, cc1 = _cache_split(usage)
    threshold = p.get("long_context_threshold_tokens")
    if threshold and (i + cc5 + cc1 + cr) > threshold:
        inp, out = float(p.get("long_input", inp)), float(p.get("long_output", out))
    c = prices.get("cache", {})
    rm = float(c.get("read_multiplier", 0.1))
    m5 = float(c.get("write_5m_multiplier", 1.25))
    m1 = float(c.get("write_1h_multiplier", 2))
    return (i * inp + cc5 * inp * m5 + cc1 * inp * m1 + cr * inp * rm + o * out) / 1_000_000


# ---------------------------------------------------------------- transcript parsing
def parse_transcript(path, prices):
    """Totals for one agent transcript; each message.id counted once, with its last usage."""
    by_id = {}
    session_id = ""
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        for n, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if not session_id and isinstance(row.get("sessionId"), str):
                session_id = row["sessionId"]
            msg = row.get("message")
            if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                continue
            if row.get("type") != "assistant" and msg.get("role") != "assistant":
                continue
            model = msg.get("model")
            if not model or model == "<synthetic>":
                continue
            by_id[msg.get("id") or ("line", n)] = (str(model), msg["usage"])
    t = {"requests": 0, "input": 0, "cc": 0, "cr": 0, "out": 0, "max_ctx": 0, "cost": 0.0,
         "models": set(), "session_id": session_id}
    for model, u in by_id.values():
        i, cc, cr, o = (_n(u.get("input_tokens")), _n(u.get("cache_creation_input_tokens")),
                        _n(u.get("cache_read_input_tokens")), _n(u.get("output_tokens")))
        if i + cc + cr + o == 0:
            continue
        t["requests"] += 1
        t["input"] += i
        t["cc"] += cc
        t["cr"] += cr
        t["out"] += o
        t["max_ctx"] = max(t["max_ctx"], i + cc + cr)
        t["cost"] += request_cost(prices, model, u)
        t["models"].add(model)
    return t


def read_meta(agent_jsonl):
    try:
        with open(agent_jsonl[:-len(".jsonl")] + ".meta.json", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def agent_id_from_path(path):
    base = os.path.basename(path)
    if base.startswith("agent-") and base.endswith(".jsonl"):
        return base[len("agent-"):-len(".jsonl")]
    return os.path.splitext(base)[0]


def build_row(agent_jsonl, prices, session_id="", agent_id="", agent_type=""):
    t = parse_transcript(agent_jsonl, prices)
    meta = read_meta(agent_jsonl)
    denom = t["input"] + t["cc"] + t["cr"]
    return {
        "logged_at_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_id": session_id or t["session_id"] or os.path.basename(os.path.dirname(os.path.dirname(
            os.path.abspath(agent_jsonl)))),
        "agent_id": agent_id or agent_id_from_path(agent_jsonl),
        "agent_type": str(meta.get("agentType") or agent_type or ""),
        "description": _trim(meta.get("description")),
        "models": ";".join(sorted(t["models"])),
        "requests": t["requests"],
        "input_tokens": t["input"],
        "cache_creation_tokens": t["cc"],
        "cache_read_tokens": t["cr"],
        "output_tokens": t["out"],
        "total_tokens": t["input"] + t["cc"] + t["cr"] + t["out"],
        "cache_hit": round(t["cr"] / denom, 4) if denom else 0,
        "max_context_tokens": t["max_ctx"],
        "cost_usd_list": round(t["cost"], 6),
        "outcome": "",
        "check": "",
    }


# ---------------------------------------------------------------- CSV storage
@contextlib.contextmanager
def locked(path, timeout=5.0):
    lock = path + ".lock"
    fd = None
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > 30:  # stale lock
                    os.remove(lock)
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                break
            time.sleep(0.05)
        except OSError:
            break
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
            try:
                os.remove(lock)
            except OSError:
                pass


def read_rows(path):
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return [dict(r) for r in csv.DictReader(fh)]
    except OSError:
        return []


def _write_all(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(COLUMNS)
        for r in rows:
            w.writerow([r.get(c, "") for c in COLUMNS])
    os.replace(tmp, path)


def _append(path, rows):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fresh = not os.path.exists(path) or os.path.getsize(path) == 0
    needs_nl = False
    if not fresh:
        with open(path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            needs_nl = fh.read(1) not in (b"\n", b"\r")
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    if fresh:
        w.writerow(COLUMNS)
    for r in rows:
        w.writerow([r.get(c, "") for c in COLUMNS])
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(("\n" if needs_nl else "") + buf.getvalue())


def upsert(path, row):
    """Append the row; if this agent_id is already logged, refresh its numbers and keep outcome/check."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with locked(path):
        rows = read_rows(path)
        for r in rows:
            if r.get("agent_id") == row["agent_id"]:
                for c in COLUMNS:
                    if c not in ("logged_at_utc", "outcome", "check"):
                        r[c] = row[c]
                _write_all(path, rows)
                return "updated"
        _append(path, [row])
        return "added"


# ---------------------------------------------------------------- modes
def _subagents_dir(transcript_path, session_id):
    return os.path.join(os.path.dirname(transcript_path), session_id, "subagents")


def run_hook(csv_arg, prices_arg, stdin_text):
    payload = json.loads(stdin_text)
    if not isinstance(payload, dict):
        return
    session_id = str(payload.get("session_id") or "")
    transcript_path = str(payload.get("transcript_path") or "")
    agent_id = str(payload.get("agent_id") or "")
    apath = str(payload.get("agent_transcript_path") or "")
    out_csv = csv_path(csv_arg, payload)
    prices = load_prices(prices_arg)
    if not (apath and os.path.isfile(apath)):
        apath = ""
        sub = _subagents_dir(transcript_path, session_id) if transcript_path and session_id else ""
        if sub and agent_id and os.path.isfile(os.path.join(sub, "agent-%s.jsonl" % agent_id)):
            apath = os.path.join(sub, "agent-%s.jsonl" % agent_id)
        elif sub and not agent_id:
            logged = {r.get("agent_id") for r in read_rows(out_csv)}
            files = [f for f in glob.glob(os.path.join(sub, "agent-*.jsonl"))
                     if agent_id_from_path(f) not in logged]
            if files:
                apath = max(files, key=os.path.getmtime)
    if not apath:
        return
    row = build_row(apath, prices, session_id=session_id, agent_id=agent_id,
                    agent_type=str(payload.get("agent_type") or ""))
    upsert(out_csv, row)


def run_scan(session_dir, csv_arg, prices_arg):
    sub = os.path.join(session_dir, "subagents")
    if not os.path.isdir(sub):
        sub = session_dir
    prices = load_prices(prices_arg)
    out_csv = csv_path(csv_arg)
    files = sorted(glob.glob(os.path.join(sub, "agent-*.jsonl")))
    added = skipped = failed = 0
    os.makedirs(os.path.dirname(os.path.abspath(out_csv)), exist_ok=True)
    with locked(out_csv):
        logged = {r.get("agent_id") for r in read_rows(out_csv)}
        new_rows = []
        session_name = os.path.basename(os.path.normpath(session_dir)) if sub != session_dir else ""
        for f in files:
            aid = agent_id_from_path(f)
            if aid in logged:
                skipped += 1
                continue
            try:
                row = build_row(f, prices, session_id=session_name)
            except OSError:
                failed += 1
                continue
            logged.add(aid)
            new_rows.append(row)
            added += 1
        if new_rows:
            _append(out_csv, new_rows)
    print("scan %s: %d logged, %d already logged, %d unreadable -> %s" % (sub, added, skipped, failed, out_csv))
    return 0


def run_outcome(agent_id, result, check, csv_arg):
    out_csv = csv_path(csv_arg)
    with locked(out_csv):
        rows = read_rows(out_csv)
        hits = [r for r in rows if r.get("agent_id") == agent_id] or \
               [r for r in rows if r.get("agent_id", "").startswith(agent_id)]
        if len(hits) != 1:
            sys.stderr.write("outcome: %s row(s) match agent_id %r in %s\n" % (len(hits) or "no", agent_id, out_csv))
            return 1
        hits[0]["outcome"] = result
        if check is not None:
            hits[0]["check"] = check
        _write_all(out_csv, rows)
    print("outcome: %s -> %s" % (hits[0]["agent_id"], result))
    return 0


def run_summary(csv_arg, since):
    rows = read_rows(csv_path(csv_arg))
    if since:
        rows = [r for r in rows if (r.get("logged_at_utc") or "")[:10] >= since]
    if not rows:
        print("no rows")
        return 0
    groups = {}
    for r in rows:
        g = groups.setdefault((r.get("agent_type") or "?", r.get("models") or "?"),
                              {"runs": 0, "req": 0, "tot": 0, "unc": 0, "cr": 0, "ctx": 0, "cost": 0.0,
                               "pass": 0, "fail": 0})
        i, cc, cr, o = (_n(_f(r.get("input_tokens"))), _n(_f(r.get("cache_creation_tokens"))),
                        _n(_f(r.get("cache_read_tokens"))), _n(_f(r.get("output_tokens"))))
        g["runs"] += 1
        g["req"] += int(_f(r.get("requests")))
        g["tot"] += i + cc + cr + o
        g["unc"] += i + cc + o
        g["cr"] += cr
        g["ctx"] += i + cc + cr
        g["cost"] += _f(r.get("cost_usd_list"))
        if r.get("outcome") in ("pass", "fail"):
            g[r["outcome"]] += 1
    head = ["agent_type", "models", "runs", "requests", "total_tokens", "uncached_tokens", "cache_hit",
            "cost_usd_list", "pass", "fail"]
    table = [head]
    keys = sorted(groups)
    tot = {"runs": 0, "req": 0, "tot": 0, "unc": 0, "cr": 0, "ctx": 0, "cost": 0.0, "pass": 0, "fail": 0}
    for k in keys:
        g = groups[k]
        for f in tot:
            tot[f] += g[f]
        table.append(_summary_line(k[0], k[1], g))
    table.append(_summary_line("TOTAL", "", tot))
    widths = [max(len(str(row[c])) for row in table) for c in range(len(head))]
    for row in table:
        print("  ".join(str(v).ljust(widths[c]) if c < 2 else str(v).rjust(widths[c]) for c, v in enumerate(row)))
    return 0


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _summary_line(a, m, g):
    hit = "%.1f%%" % (100.0 * g["cr"] / g["ctx"]) if g["ctx"] else "n/a"
    return [a, m, g["runs"], g["req"], g["tot"], g["unc"], hit, "%.4f" % g["cost"], g["pass"], g["fail"]]


# ---------------------------------------------------------------- CLI
def build_parser():
    p = argparse.ArgumentParser(description="Log Claude Code subagent usage, one CSV row per run.")
    p.add_argument("--hook", action="store_true", help="SubagentStop hook mode (JSON on stdin; never fails)")
    p.add_argument("--scan", metavar="SESSION_DIR", help="backfill a session's subagents/agent-*.jsonl")
    p.add_argument("--outcome", nargs=2, metavar=("AGENT_ID", "RESULT"), help="RESULT is pass or fail")
    p.add_argument("--check", help="the acceptance command (with --outcome)")
    p.add_argument("--summary", action="store_true", help="totals per agent type / model")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="only rows logged on or after this date (with --summary)")
    p.add_argument("--csv", help="CSV file to use")
    p.add_argument("--prices", help="prices.json to use")
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--hook" in argv:  # must never fail or print
        try:
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                args = build_parser().parse_args(argv)
                text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
                run_hook(args.csv, args.prices, text)
        except BaseException:
            pass
        return 0
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.scan:
        return run_scan(args.scan, args.csv, args.prices)
    if args.outcome:
        if args.outcome[1] not in ("pass", "fail"):
            parser.error("--outcome RESULT must be pass or fail")
        return run_outcome(args.outcome[0], args.outcome[1], args.check, args.csv)
    if args.summary:
        if args.since:
            try:
                datetime.date.fromisoformat(args.since)
            except ValueError:
                parser.error("--since must be YYYY-MM-DD")
        return run_summary(args.csv, args.since)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
