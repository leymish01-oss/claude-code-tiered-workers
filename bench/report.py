#!/usr/bin/env python3
"""report: per-arm results and the ship verdict from tier_bench's tasks CSV. Standard library only.

  python report.py --tasks-csv bench-results/tier_bench_tasks.csv [--md report.md]

Per arm: pass rate on the first attempt and after the one retry/escalation (each with a Wilson 95% CI), mean
tokens per task-run (total and uncached), mean cost (the CLI's own figure and a list-price estimate), cache hit,
escalation rate and requests above the Haiku long-context price threshold. Against the baseline arm (default
`leader`): deltas paired by task (repeats averaged per task and arm, tasks resampled, 10,000 draws, seed 82) with
95% bootstrap CIs, and the verdict: SHIP when the pass-rate delta is at least -3 points AND total tokens fall by at
least 25% (point estimates), else NO-SHIP.
"""
from __future__ import annotations

import argparse
import csv
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

Z95 = 1.959963984540054
DRAWS = 10_000
SEED = 82
SHIP_PASS_DELTA_PTS = -3.0
SHIP_TOKEN_CUT_PCT = 25.0
NUMERIC = ("passed_first", "passed_final", "attempts", "escalated", "calls", "input_tokens", "cache_creation_tokens",
           "cache_read_tokens", "output_tokens", "total_tokens", "uncached_tokens", "cost_list", "max_context",
           "haiku_over_100k", "wall_s", "tests_edited")


def wilson(k: float, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials (as fractions)."""
    if n <= 0:
        return 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _num(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def load(paths) -> list[dict]:
    rows = []
    for p in paths:
        with open(p, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                for k in NUMERIC:
                    r[k] = _num(r.get(k)) or 0.0
                r["cost_cli"] = _num(r.get("cost_cli"))
                rows.append(r)
    return rows


def mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    k1 = sum(r["passed_first"] for r in rows)
    k2 = sum(r["passed_final"] for r in rows)
    inp = sum(r["input_tokens"] + r["cache_creation_tokens"] + r["cache_read_tokens"] for r in rows)
    cli = [r["cost_cli"] for r in rows if r["cost_cli"] is not None]
    return {
        "runs": n, "tasks": len({r["task"] for r in rows}),
        "pass_first": k1 / n if n else 0.0, "pass_first_ci": wilson(k1, n),
        "pass_final": k2 / n if n else 0.0, "pass_final_ci": wilson(k2, n),
        "tokens": mean(r["total_tokens"] for r in rows), "uncached": mean(r["uncached_tokens"] for r in rows),
        "cost_cli": mean(cli) if len(cli) == n and n else None, "cost_list": mean(r["cost_list"] for r in rows),
        "cache_hit": sum(r["cache_read_tokens"] for r in rows) / inp if inp else 0.0,
        "escalation": mean(r["escalated"] for r in rows),
        "haiku_over_100k": int(sum(r["haiku_over_100k"] for r in rows)),
        "tests_edited": int(sum(1 for r in rows if r["tests_edited"])),
        "contaminated": int(sum(1 for r in rows if (r.get("contamination") or "").strip())),
    }


def per_task(rows: list[dict], field: str) -> dict[str, float]:
    """Mean of `field` per task (repeats averaged); None values make that task's mean None."""
    acc = defaultdict(list)
    for r in rows:
        acc[r["task"]].append(r[field])
    return {t: (None if any(v is None for v in vs) else mean(vs)) for t, vs in acc.items()}


def _delta_pts(a, b):
    return (mean(a) - mean(b)) * 100


def _cut_pct(a, b):
    sb = sum(b)
    return (1 - sum(a) / sb) * 100 if sb else float("nan")


def percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    pos = q * (len(sorted_vals) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def bootstrap(arm_vals: list[float], base_vals: list[float], stat, draws: int = DRAWS, seed: int = SEED):
    """Point estimate and 95% CI of stat(arm, base), resampling paired tasks."""
    n = len(arm_vals)
    point = stat(arm_vals, base_vals)
    if n == 0:
        return point, (float("nan"), float("nan"))
    rng = random.Random(seed)
    out = []
    for _ in range(draws):
        idx = [rng.randrange(n) for _ in range(n)]
        v = stat([arm_vals[i] for i in idx], [base_vals[i] for i in idx])
        if not math.isnan(v):
            out.append(v)
    out.sort()
    return point, (percentile(out, 0.025), percentile(out, 0.975))


def compare(rows: list[dict], arm: str, base: str, draws: int = DRAWS, seed: int = SEED) -> dict:
    a_rows = [r for r in rows if r["arm"] == arm]
    b_rows = [r for r in rows if r["arm"] == base]
    out = {}
    tasks = sorted(set(r["task"] for r in a_rows) & set(r["task"] for r in b_rows))
    out["tasks"] = len(tasks)
    metrics = [("pass_first_pts", "passed_first", _delta_pts), ("pass_final_pts", "passed_final", _delta_pts),
               ("token_cut_pct", "total_tokens", _cut_pct), ("uncached_cut_pct", "uncached_tokens", _cut_pct),
               ("cost_list_cut_pct", "cost_list", _cut_pct), ("cost_cli_cut_pct", "cost_cli", _cut_pct)]
    for name, field, stat in metrics:
        a, b = per_task(a_rows, field), per_task(b_rows, field)
        ts = [t for t in tasks if a.get(t) is not None and b.get(t) is not None]
        if not ts or (field == "cost_cli" and len(ts) < len(tasks)):
            out[name] = (None, (None, None))
            continue
        out[name] = bootstrap([a[t] for t in ts], [b[t] for t in ts], stat, draws, seed)
    return out


def verdict(pass_delta_pts: float | None, token_cut_pct: float | None) -> str:
    if pass_delta_pts is None or token_cut_pct is None:
        return "NO-SHIP"
    ok = pass_delta_pts >= SHIP_PASS_DELTA_PTS and token_cut_pct >= SHIP_TOKEN_CUT_PCT
    return "SHIP" if ok else "NO-SHIP"


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _ci(ci, scale=100.0, unit="%") -> str:
    lo, hi = ci
    return "" if lo is None or (isinstance(lo, float) and math.isnan(lo)) else f" [{lo * scale:.1f}, {hi * scale:.1f}]{unit}"


def _est(pair, unit) -> str:
    point, (lo, hi) = pair
    if point is None or (isinstance(point, float) and math.isnan(point)):
        return "n/a"
    return f"{point:+.1f}{unit} [{lo:+.1f}, {hi:+.1f}]"


def render(rows: list[dict], sources, base: str = "leader", draws: int = DRAWS, seed: int = SEED) -> str:
    arms = sorted({r["arm"] for r in rows}, key=lambda a: (a != base, a))
    runners = sorted({r.get("runner") or "cli" for r in rows})
    out = ["# Tier bench report", "",
           f"{len(rows)} task-runs from {', '.join(str(s) for s in sources)}; runner: {', '.join(runners)}; "
           f"{len({r['task'] for r in rows})} tasks.", "",
           "## Per arm", "",
           "| arm | runs | tasks | pass first (95% CI) | pass final (95% CI) | tokens/run | uncached/run | "
           "cost/run CLI | cost/run list price* | cache hit | escalated | Haiku >100k req | tests edited | flagged |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for arm in arms:
        s = summarize([r for r in rows if r["arm"] == arm])
        cli = "n/a" if s["cost_cli"] is None else f"${s['cost_cli']:.4f}"
        out.append(f"| {arm} | {s['runs']} | {s['tasks']} | {_pct(s['pass_first'])}{_ci(s['pass_first_ci'])} | "
                   f"{_pct(s['pass_final'])}{_ci(s['pass_final_ci'])} | {s['tokens']:,.0f} | {s['uncached']:,.0f} | "
                   f"{cli} | ${s['cost_list']:.4f} | {_pct(s['cache_hit'])} | {_pct(s['escalation'])} | "
                   f"{s['haiku_over_100k']} | {s['tests_edited']} | {s['contaminated']} |")
    out += ["", "## Pass rate (final) by difficulty", "", "| arm | " + " | ".join(
        sorted({r['difficulty'] for r in rows})) + " |", "|---|" + "---|" * len({r['difficulty'] for r in rows})]
    for arm in arms:
        cells = []
        for d in sorted({r["difficulty"] for r in rows}):
            sub = [r for r in rows if r["arm"] == arm and r["difficulty"] == d]
            cells.append(f"{_pct(summarize(sub)['pass_final'])} (n={len(sub)})" if sub else "n/a")
        out.append(f"| {arm} | " + " | ".join(cells) + " |")
    others = [a for a in arms if a != base]
    if base in arms and others:
        out += ["", f"## Against `{base}` (paired by task; bootstrap 95% CI, {draws:,} draws, seed {seed})", "",
                "| arm | tasks | pass first Δ | pass final Δ | total tokens cut | uncached cut | list-price cost cut* | "
                "CLI cost cut |", "|---|---|---|---|---|---|---|---|"]
        verdicts = []
        for arm in others:
            c = compare(rows, arm, base, draws, seed)
            out.append(f"| {arm} | {c['tasks']} | {_est(c['pass_first_pts'], ' pts')} | "
                       f"{_est(c['pass_final_pts'], ' pts')} | {_est(c['token_cut_pct'], '%')} | "
                       f"{_est(c['uncached_cut_pct'], '%')} | {_est(c['cost_list_cut_pct'], '%')} | "
                       f"{_est(c['cost_cli_cut_pct'], '%')} |")
            pd, tc = c["pass_final_pts"][0], c["token_cut_pct"][0]
            v = verdict(pd, tc)
            pd_s = "n/a" if pd is None else f"{pd:+.1f} pts"
            tc_s = "n/a" if tc is None else f"{tc:.1f}%"
            verdicts.append(f"- **{arm}: {v}**: pass-rate delta {pd_s} (needs ≥ {SHIP_PASS_DELTA_PTS:+.0f} pts), "
                            f"total-token reduction {tc_s} (needs ≥ {SHIP_TOKEN_CUT_PCT:.0f}%).")
        out += ["", "## Verdict (point estimates; final pass rate)", ""] + verdicts
    flagged = sum(1 for r in rows if (r.get("contamination") or "").strip())
    out += ["", "*List-price cost = tokens × the list prices in prices.json (an estimate to compare arms, not an "
                "invoice). CLI cost = the CLI's own total_cost_usd; n/a where a runner doesn't report it.",
            f"Flagged = task-runs whose tool calls touched the source repository or its git history ({flagged}); "
            "tests edited = the worker changed a test file (restored before every check)."]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="tier bench report")
    ap.add_argument("--tasks-csv", nargs="+", required=True)
    ap.add_argument("--md", help="also write the report to this file")
    ap.add_argument("--baseline", default="leader")
    ap.add_argument("--runner", help="only rows from this runner (cli or subagent)")
    ap.add_argument("--exclude-flagged", action="store_true", help="drop task-runs with a contamination flag")
    ap.add_argument("--draws", type=int, default=DRAWS)
    ap.add_argument("--seed", type=int, default=SEED)
    a = ap.parse_args(argv)
    rows = load(a.tasks_csv)
    if a.runner:
        rows = [r for r in rows if (r.get("runner") or "cli") == a.runner]
    if a.exclude_flagged:
        rows = [r for r in rows if not (r.get("contamination") or "").strip()]
    if not rows:
        print("no task-runs yet", file=sys.stderr)
        return 1
    text = render(rows, [Path(p).name for p in a.tasks_csv], a.baseline, a.draws, a.seed)
    if a.md:
        Path(a.md).write_text(text, encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
