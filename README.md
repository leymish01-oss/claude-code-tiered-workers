# Tiered workers for Claude Code

Three subagents on three model tiers, a skill that tells the leader which one to use, and a hook that logs what each
worker run used. MIT licensed. The leader stays on whatever model you chose; the workers take bounded subtasks on a
cheaper model, and an executable check (a test, a build) decides whether the result stands.

It is a routing rule written as agent files, not a service. Nothing runs outside Claude Code.

## The tiers
| Tier | Agent | Model | Effort | Tools | Use for |
|---|---|---|---|---|---|
| 1 | `haiku-scout` | haiku | medium | Read, Grep, Glob, Bash (read-only use) | locate files or symbols, summarise code, run a test or build command and parse the failures. Handoff 4k tokens max. Stops and reports before about 60k tokens of context. Never edits. |
| 2 | `sonnet-builder` | sonnet | default | Read, Write, Edit, Glob, Grep, Bash | the default for a bounded code change plus its tests, reviews, docs. |
| 3 | `opus-architect` | opus | default | Read, Write, Edit, Glob, Grep, Bash | planning, cross-module debugging, ambiguous specs, security, more than 100k tokens of context, anything that already failed once. |

Haiku is billed about 5x per token once a single request's context passes 100k tokens, which is why the scout stops
early. The prices the logger uses are in `prices.json`. They are list prices that you edit; the cost column is an
estimate, not your invoice.

## Escalation rule
Run the cheaper tier, then run the executable check (tests, typecheck, build). If it fails, escalate exactly one tier and
hand over the failure log (last 80 lines) and the diff so far. If tier 3 fails, the leader takes over. Never an LLM
judge: no model grades another model's answer.

A cheap worker cannot switch models mid-task, and a cache belongs to one model, so a handoff starts a fresh context.
That is why the handoff is a strict template (goal, paths, exact error text, acceptance check, constraints, output
format) and not a story. The `delegate` skill carries the template and the rule, including: if a subtask needs more than
30k tokens of the leader's context, fork or do it in the leader instead.

## Install
1. Copy `agents/*.md` into your project's `.claude/agents/` (or `~/.claude/agents/` for all projects).
2. Copy `skills/delegate/` into `.claude/skills/delegate/`.
3. Copy `hooks/log_usage.py` to `.claude/hooks/log_usage.py`. Optionally copy `prices.json` next to it or one folder
   up; without it the logger uses a built-in copy of the same list prices.
4. Merge `hooks/settings.example.json` into your `.claude/settings.json`. It adds a `SubagentStop` hook that runs
   `log_usage.py --hook`; the hook never fails and prints nothing, so it cannot block a run.
5. Start a new Claude Code session (agent files and hooks are read when a session starts), then check that
   `haiku-scout`, `sonnet-builder` and `opus-architect` appear in `/agents`.

Before you route real work by tier, run the bench in `bench/` on a handful of your own tasks: in our run the tiers
halved list-price cost but did not cut total tokens (see "Measured results" below).

## Usage log
Each worker run adds one row to `agent_tiers.csv` in the project root: tokens (input, cache creation, cache read,
output), cache hit, the largest single request's context, and a list-price cost. A streaming transcript repeats a
message id on several lines, so each id is counted once, with its last usage.

```
python .claude/hooks/log_usage.py --scan <session folder>        # backfill runs the hook did not see
python .claude/hooks/log_usage.py --outcome <agent_id> pass --check "<your test command>"
python .claude/hooks/log_usage.py --summary [--since YYYY-MM-DD]
```

Definitions: total tokens = input + cache creation + cache read + output. Uncached tokens = input + cache creation +
output. Cache hit = cache read / (input + cache creation + cache read).

## Measured results
How it was measured: see `bench/README.md` (harness, task format, report and the SHIP rule).

One run, 2 Nov 2026: 20 of the authors' own past code tasks (10 easy, 10 medium; Python, each with its own tests),
2 repeats per setup, workers run as subagents of one session, list prices from `prices.json`. The rule was written
first: SHIP only if the pass rate stays within 3 points of the all-leader baseline AND total tokens fall by 25% or more.

| Setup | Pass rate | Total tokens vs leader (95% CI) | List-price cost vs leader |
|---|---|---|---|
| Leader (top model does everything) | 40/40 | baseline | 100 |
| Tiered (scout on medium tasks, builder, escalate on failed tests) | 40/40 | +26.7% (+10.3 to +40.9) | 46 |
| Tiered, no scout | 40/40 | -13.3% (-5.3 to -21.5) | 44 |

Verdict: NO-SHIP for both tiered setups on the token rule; cost per task roughly halves. No task needed escalation.
Why: each worker starts with the same large context (about 85k tokens in that setup) and re-reads it every turn, so
total tokens track the number of steps, not the model; a second worker (the scout) pays for that context again, cache
is per model, and the small model's 100k-context price step sits close to the starting size. Your numbers will differ:
run the bench on your own tasks before you route by tier.

## License
MIT. Copyright (c) 2026 LeyMish Labs. See `LICENSE`.
