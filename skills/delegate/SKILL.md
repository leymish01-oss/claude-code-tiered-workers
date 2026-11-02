---
name: delegate
description: Tier table and handoff discipline for routing a bounded subtask to haiku-scout, sonnet-builder or opus-architect with an executable check and one-tier escalation. Measured 2 Nov 2026 - halves list-price cost but did not cut total tokens by 25%, so it is NOT the default; use only when asked to route by cost.
---

# delegate: cheapest tier that passes the check

> **Measured (2 Nov 2026, 20 tasks x 2 repeats):** pass rate unchanged (100%), list-price cost about half, total
> tokens +27% with a scout and -13% without one. Our rule needed -25% tokens, so tiering is not our default; only the
> usage log stays on. Use this skill when someone asks to route work by cost.

You (the leader) keep the model the user chose. Workers do bounded subtasks on cheaper models. The only judge of a
worker's result is an executable check.

## Tier table
| Tier | Agent | Model | Effort | Tools | Use for |
|---|---|---|---|---|---|
| 1 | `haiku-scout` | haiku | medium | Read, Grep, Glob, Bash (read-only use) | locate files or symbols, summarise code, run a test or build command and parse the failures. Handoff 4k tokens max. Stops and reports before about 60k tokens of context. Never edits. |
| 2 | `sonnet-builder` | sonnet | default | Read, Write, Edit, Glob, Grep, Bash | the default for a bounded code change plus its tests, reviews, docs. |
| 3 | `opus-architect` | opus | default | Read, Write, Edit, Glob, Grep, Bash | planning, cross-module debugging, ambiguous specs, security, more than 100k tokens of context, anything that already failed once. |

Pick the lowest tier whose row fits. If two rows fit, take the lower one. If none fits, do it yourself.

## Before you delegate
- If the subtask needs more than 30k tokens of your own context (it depends on a long conversation, many files you
  have already read, or decisions you made earlier), fork yourself or do it in the leader. A cold worker would
  re-read everything, and a long narrative handoff loses the details.
- One or two tool calls is not worth a handoff. Do it yourself.
- Haiku price cliff: Haiku is billed about 5x per token once a single request's context passes 100k tokens (see
  `prices.json`). A scout stops before about 60k. If the search needs more, split it by directory or use tier 2.

## Handoff template
Paths, exact error text, and an acceptance check. Never narrative. Fill every field; write `none` if a field is empty.

```
Goal: <one sentence: what must be true when you are done>
Paths: <files and directories to read or edit, one per line>
Exact error text: <paste the failing output verbatim, or none>
Acceptance check: <one command that exits 0 on success, e.g. a test, typecheck, or build command>
Constraints: <what not to touch, limits, no git state changes unless allowed>
Output format: <what the report must contain; default is the worker's own report format>
```

A scout's acceptance check is the command that proves its answer, or `none` for a pure lookup. A builder or an
architect must have a real command. If you cannot name one, write it first or do the work yourself.

## Run, check, escalate
1. Pick the tier and delegate with the template.
2. When the worker returns, run the acceptance check yourself and read the exit code. A worker's "done" is not a check.
3. Exit 0: accept the work.
4. Non-zero, or the worker stopped with `ESCALATE:`: escalate exactly one tier (scout to builder, builder to
   architect). Hand the next tier the original handoff, the failure log (last 80 lines), and the diff so far.
5. The architect fails: take over in the leader, or report the blocker. There is no tier 4.

Executable checks only: tests, typecheck, build, lint, a script's exit code. Never an LLM judge. Do not ask a model
whether another model's answer looks right, and do not accept "looks good" as a pass.

## Logging
A SubagentStop hook logs every worker run to a CSV: one row with tokens, cache hit, and list-price cost. After you run
the acceptance check, record the result against the worker's agent id (the Agent tool result prints it):

```
python <logger> --outcome <agent_id> pass --check "<the acceptance command>"
python <logger> --outcome <agent_id> fail --check "<the acceptance command>"
python <logger> --summary
```

`<logger>` is `hooks/log_usage.py` from this package, or `scripts/agent_tier_log.py` if the project keeps it there.
