# Checks

A **check** is a standing instruction for a model: what to look for in your code, in which
files, and how often. Each run reads those files and hands them to the model with your
instruction. Every problem the model reports, and can point to in the code, becomes a finding
and then a task in the review queue, like any other. Use checks for the reviews you would
otherwise run by hand: a bug hunt, a hardening pass, a security audit of one area.

A check never changes code. Its tasks need a human approval like every other task, and the
coding agent that implements one still runs in the sandbox and can only open a pull request.

!!! note "A check needs a model"
    The built-in offline rules cannot follow an instruction written in words. Without a model
    for the `check` stage, a run is recorded as **skipped**, with that reason, and a
    `check.failed` notification says so. A local model through Ollama or vLLM keeps your code on
    your own machines; see [Models](models.md).

## Writing a check

Checks are a project setting, in `opsmith.toml` or on the configuration page of the UI:

```toml
[[projects.settings.checks]]
name = "security-review"               # lower case, digits and hyphens
cron = "0 3 * * 1"                     # Mondays at 03:00 UTC
category = "security"                  # optional: file every finding under this category
paths = ["src/**"]
exclude = ["src/generated/**"]
instructions = """
Review this code for weaknesses an attacker could use: injection, missing authorization
checks, unsafe deserialization, secrets in code, and user input that reaches the file system
or a shell. Report only what you can point to in the code.
"""

[[projects.settings.checks]]
name = "bug-hunt"
cron = "30 2 * * *"                    # every night
paths = ["app/**", "lib/**"]
instructions = """
Find bugs that would show up in production: unhandled errors, wrong edge cases (empty input,
zero, None), off-by-one mistakes, resources that are never closed, and race conditions.
"""

[[projects.settings.checks]]
name = "hardening"
cron = "0 4 1 * *"                     # on the first of each month
category = "fix"
instructions = """
Find missing hardening: network calls without a timeout or retry limit, unbounded reads,
missing input validation at the edges, and logging that could leak personal data.
"""
```

| Field | Default | Meaning |
|---|---|---|
| `name` | required | Identifies the check. Lower-case letters, digits and hyphens, up to 63 characters, unique in the project |
| `instructions` | required | What to look for, in plain words. Up to 4,000 characters |
| `cron` | `0 3 * * *` | When it runs, in UTC; the same five-field format as `audit_cron` |
| `enabled` | `true` | A disabled check is not scheduled, but can still be run by hand to try it out |
| `category` | none | Every finding takes this category (`fix`, `security`, `performance`, `compliance`, `refactor`, `observability`, `dependency`). Without it, the model picks one per finding |
| `repositories` | all | Names of the project's repositories to run on |
| `paths` | `["**"]` | Globs of the files to read, relative to the repository root. `src` also covers everything below it |
| `exclude` | none | Globs of files to leave out |
| `max_files` | 200 | Files read per run, 1 to 2,000 |
| `max_chars` | 400,000 | Characters of code read per run, 1,000 to 4,000,000 |
| `max_findings` | 20 | Findings kept per run, 1 to 200; the most severe are kept |

A project can have up to 50 checks. Every value is checked when you save it: a bad cron, a
blank instruction, a glob that leaves the repository (`../`) or a duplicate name is refused
with a message.

## What a run does

1. The scheduler starts the check on its cron, on every repository it covers. You can also
   start it by hand (below).
2. The worker checks the repository out, as for an audit, and reads the files the check's
   `paths` cover, minus its `exclude`, minus protected paths marked `excluded`, in path order,
   up to its limits. Binary files, files over 1 MB and symbolic links are never read.
3. The files go to the model of the `check` stage, about 120,000 characters per call, with
   every line numbered. Your instruction goes with them in a block of its own. The code is
   framed as data, so text in a file cannot pose as an instruction.
4. Every reported problem must quote the file it names: one to five lines copied from it. The
   quote is looked up in the file, and its location becomes the finding's line. A problem
   whose quote is not in that file, or that names a file the model was not given, is
   discarded and counted on the run.
5. The findings that are kept go to aggregation, which turns them into tasks for review,
   merges them with runtime evidence that points at the same code, and suppresses problems
   you rejected or fixed recently.

## Running a check now

```bash
curl -X POST "$OPSMITH/api/v1/projects/$PROJECT/trigger" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"job": "check", "target_id": "<repository id>", "check": "security-review"}'
```

This needs an `admin` key. The run is queued, so it is retried like a scheduled one.

## Choosing the model

The `check` stage takes its model from `llm.stages.check`, or from `llm.default`:

```toml
[projects.settings.llm.stages.check]
provider = "anthropic"
model = "claude-opus-5-5"
max_tokens = 16000
```

A check reads much more text than the other stages. It is capped like every stage, by
`budgets.max_tokens_per_run`, `max_cost_per_run_usd` and `max_cost_per_day_usd`; size
`max_chars` and the budgets together. A run that reaches a cap stops, keeps what the parts
before it found, and is recorded as `budget_exceeded`.

## Where results appear

- **Tasks** in the review queue. Their findings name the check as their source
  (`check:security-review`) and carry the quoted code and the suggested fix.
- **Runs** in `GET /projects/{id}/audit-runs`, with `mode` `check` and `audit_types`
  `["check:<name>"]`. A run records the model, the files read, how many were left out by the
  limits, how many findings were discarded and why, and the model's own notes.
- **The audit log**: `check.completed`, `check.skipped` or `check.failed`.

| Run status | Meaning |
|---|---|
| `succeeded` | The check ran on every file in scope |
| `skipped` | Nothing was checked: no model can follow the instruction, or no readable file matches `paths`. The reason is in `error` |
| `failed` | A model call failed; findings from the parts before it are kept |
| `budget_exceeded` | A budget cap stopped the run; findings from the parts before it are kept |

Anything other than `succeeded` also sends a `check.failed` (or `budget.exceeded`)
notification.
