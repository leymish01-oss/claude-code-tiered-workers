# tier_bench: measure tiered workers before you trust them

Tiered workers send a task to a cheap model first and escalate one tier when an executable check fails. That
can save tokens, or quietly cost you pass rate. This bench measures both on tasks from **your own git history**,
using the task's own tests as the only judge.

Standard library Python 3.10+ and git. The model calls go through the `claude` CLI (headless), or through your
own leader session's subagents (see "Without headless CLI access").

## Tasks

A task is a past commit that changed code and its tests. `tasks.json`:

```json
{
  "contamination_markers": ["name-of-your-repo-folder"],
  "tasks": [{
    "id": "t01", "difficulty": "easy",
    "commit": "<sha of the commit>", "base": "<sha of its parent>",
    "paths": ["src/parser.py"],
    "test_files": ["tests/test_parser.py"],
    "accept": "python -m pytest tests/test_parser.py -q",
    "regress": "python -m pytest tests -q",
    "prompt": "Goal: ...\nRequired behaviour: ...\nEdit: src/parser.py\nAcceptance: `python -m pytest tests/test_parser.py -q` must pass.",
    "source": "commit subject"
  }]
}
```

- `paths`: the smallest set of files/folders that lets the tests run. `test_files` always come from `commit`.
- `prompt`: write it like a ticket (goal, behaviour, the test file, the acceptance command). Never paste the
  solution; `validate` counts long solution lines that appear verbatim in the prompt (`leaks`).
- `regress` (optional) must pass too. `setup` (optional list of shell commands) runs in the fresh run dir, e.g. to
  install dependencies.
- Easy ≈ up to 80 changed lines in 1–2 files; medium ≈ 80–300 lines or 2–4 files.

## Commands

```sh
python tier_bench.py doctor                      # CLI found, logged in, agent files present (no model call)
python tier_bench.py validate --tasks tasks.json --jobs 4 --json validate.json
python tier_bench.py run --tasks tasks.json --arms leader,tiered --repeats 2 --jobs 2 --valid-from validate.json
python report.py --tasks-csv bench-results/tier_bench_tasks.csv --md report.md
```

`validate` exports `base` plus the commit's test files (the acceptance command must FAIL), then `commit` (it must
PASS). Only valid tasks belong in a measurement.

Every run gets a fresh folder (default `<temp>/tier-bench/<task>-<arm>-<repeat>`, change it with `--run-root`;
keep it outside your repository). It holds `git archive <base> -- <paths>`, the commit's test files, your current
worker agent files, and a new one-commit git repo. Your repository's history is never copied, so the answer can't
leak through `git log`.

## Arms

| arm | steps |
|---|---|
| `leader` | `--model opus` on the task. If the acceptance check fails: one retry in the same session (`--resume`) with the last 80 lines of the failure. |
| `tiered` | medium tasks: `haiku-scout` (read-only, effort medium) reports what the change needs; then `sonnet-builder` makes the change with that report. If the check fails: ONE escalation to a fresh `opus-architect` with the failure log and the builder's diff. |
| `tiered-noscout` | `tiered` without the scout step. |

Before every check the harness restores the original test files, so a worker that edits a test can't pass by it
(the run is flagged `tests_edited`). A run is also flagged when a tool call touched the source repository's path
(or any `contamination_markers`) or ran `git log`/`git show`/`git reflog`.

Each call is:

```
claude -p --output-format json --setting-sources project --strict-mcp-config --model <m>
  [--agents <run-root>/_agents.json --agent <worker>] [--effort medium] [--resume <session>]
  --permission-mode acceptEdits --permission-prompts none
  --allowedTools Read Edit Write Glob Grep Bash
  --disallowedTools Agent Task WebFetch WebSearch "Bash(git push:*)" "Bash(curl:*)" "Bash(wget:*)"
```

The prompt goes in on stdin (no command-line length limit), cwd is the run folder, and the timeout is 20
minutes. `acceptEdits` approves file edits inside the run folder only; anything else that would prompt is denied.
The worker definitions are passed explicitly with `--agents` (built from your `.claude/agents/*.md`) because a
headless session in a fresh, untrusted folder may ignore project agent files. A step whose session ran on another
model than the one it asked for gets the note `model-mismatch`.

**Auth.** Headless calls need the CLI's own login: run `claude auth login` once in a terminal, or create a token
with `claude setup-token` and export `CLAUDE_CODE_OAUTH_TOKEN` (or use `ANTHROPIC_API_KEY`). When the bench is
started from inside another Claude Code session, the variables that session exports to its children are removed
before each call, so the nested CLI uses its own login. `run` checks `claude auth status` first and stops if it
isn't logged in.

## Without headless CLI access

A leader session can run the same arms with its own subagents:

```sh
python tier_bench.py prepare t01 tiered 1 --tasks tasks.json   # run dir + the next step's handoff prompt
#   ... spawn the subagent it names with that prompt ...
python tier_bench.py record t01 tiered 1 --step scout --transcript <session>/subagents/agent-<id>.jsonl
python tier_bench.py prepare t01 tiered 1 --tasks tasks.json   # -> build (with the scout's report)
python tier_bench.py record t01 tiered 1 --step build --transcript ...
python tier_bench.py check t01 tiered 1 build --tasks tasks.json   # PASS/FAIL; on FAIL `prepare` gives escalate
```

`record` reads tokens per model from the subagent's transcript (each message id counted once) with the same
parser as `../hooks/log_usage.py`; for a continued conversation (the leader's retry) it counts only the new
messages. `check` leaves the failure log and the diff in `<run dir>/.bench/` for the escalation handoff. Rows get
`runner=subagent` and no CLI cost. Note that subagents start with the leader's own context and working folder,
so the handoff prompt pins them to the run folder; compare runners separately (`report.py --runner`).

## Output

`--out-dir` (default `./bench-results/`) gets two CSVs:

- `tier_bench_runs.csv`, one row per model call: task, difficulty, arm, repeat, runner, step
  (scout/build/escalate/leader1/leader2), agent, model(s), session, input / cache-creation / cache-read / output
  tokens, total, uncached, `cost_cli` (the CLI's `total_cost_usd`), `cost_list`, turns, duration, requests,
  max_context (largest single request's input + cache), Haiku requests above 100k context, exit, contamination.
- `tier_bench_tasks.csv`, one row per task-run: passed_first, passed_final, attempts, escalated, the token sums,
  costs, cache_hit, max_context, wall time, tests_edited, contamination.

`run` is resumable: task-runs already in the tasks CSV are skipped. Call rows from an unfinished task-run keep
their own `attempt_id` and are ignored by the report.

Token definitions: total = input + cache_creation + cache_read + output; uncached = input + cache_creation +
output; cache_hit = cache_read / (input + cache_creation + cache_read).

## Report and verdict

`report.py` prints, per arm: pass rate first and final (Wilson 95% CI), mean tokens per task-run (total and
uncached), mean cost (CLI and list price), cache hit, escalation rate and Haiku requests above 100k. Against
`leader`: deltas paired by task (repeats averaged, tasks resampled, 10,000 bootstrap draws, seed 82) with 95% CIs.

**SHIP** when the final pass-rate delta is at least −3 points AND total tokens drop by at least 25% (point
estimates); otherwise **NO-SHIP**. Both numbers are printed with the verdict.

List-price cost uses `../prices.json`: list prices you edit, an estimate for comparing arms, not an invoice.

## Limits

- One machine, the CLI version you have, and tasks from one repository: it measures your setup, not models in
  general.
- Repeats of the same task aren't independent; the bootstrap resamples tasks, not runs.
- The CLI's token totals include its own small background calls; the transcript holds the main conversation only.
