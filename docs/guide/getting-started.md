# Getting started

This guide takes you from a fresh clone to your own project producing its first pull request,
on your own machine, in about fifteen minutes. It uses no API key, no Docker and no network:
the built-in offline rules do the analysis, and everything stays local.

When you want real models, real repositories and a real deployment, the last section points to
the pages that cover them.

- [1. Install](#1-install)
- [2. Watch the whole loop once](#2-watch-the-whole-loop-once)
- [3. Set up your own project](#3-set-up-your-own-project)
- [4. Send it some logs](#4-send-it-some-logs)
- [5. Analyze and audit](#5-analyze-and-audit)
- [6. Review and approve](#6-review-and-approve)
- [7. Look at the pull request](#7-look-at-the-pull-request)
- [Where to go next](#where-to-go-next)

## 1. Install

You need git, Python 3.12 or newer, and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/deadboyccc/Opsmith.git
cd Opsmith
uv sync --all-extras
```

## 2. Watch the whole loop once

```bash
uv run opsmith demo --dir ./opsmith-demo
```

In a few seconds the demo generates a small repository with a bug, feeds in logs and metrics,
finds the problem from both sides, has a (simulated) reviewer approve the task, fixes the code,
opens a pull request, merges it and verifies the fix. The JSON at the end of its output lists
two API keys. Use `api_keys.admin` to look around:

```bash
OPSMITH_DATA_DIR=./opsmith-demo/data uv run opsmith api
# open http://127.0.0.1:8000/ui and sign in with that key; stop the server with Ctrl+C
```

The [UI tour](ui-tour.md) shows these pages if you would rather not run it.

## 3. Set up your own project

Pick a directory for this experiment. Opsmith will only read repositories and files inside it.

```bash
export LAB=$HOME/opsmith-lab
mkdir -p $LAB/repos

export OPSMITH_LOCAL_ROOT=$LAB           # the only directory Opsmith may read local repos from
export OPSMITH_DATA_DIR=$LAB/data        # where Opsmith keeps its own data
export OPSMITH_SANDBOX_BACKEND=local     # development only: runs the test command without isolation
```

Keep these four variables set in every terminal you use below.

> `OPSMITH_SANDBOX_BACKEND=local` runs your repository's test command directly on your machine.
> That is fine for a repository you wrote. It is refused for GitHub repositories; a real
> deployment uses the Docker sandbox ([Deployment](deployment.md#execution-sandbox)).

**A repository to work on.** Use one of your own Python repositories by cloning it into
`$LAB/repos/`, or create this small one, which has two problems Opsmith can find:

```bash
mkdir -p $LAB/repos/shop/app $LAB/repos/shop/tests && cd $LAB/repos/shop && git init -q -b main
touch app/__init__.py

cat > app/orders.py <<'EOF'
"""Order handling for the shop."""

import httpx


def fetch_stock(item_id: str) -> int:
    response = httpx.get(f"https://inventory.example.com/stock/{item_id}")
    return int(response.json()["count"])


def parse_quantity(raw: str) -> int:
    try:
        return int(raw)
    except:
        return 0
EOF

cat > tests/test_orders.py <<'EOF'
import unittest

from app.orders import parse_quantity


class OrdersTest(unittest.TestCase):
    def test_parse_quantity(self) -> None:
        self.assertEqual(parse_quantity("3"), 3)
        self.assertEqual(parse_quantity("x"), 0)
EOF

git add -A && git -c user.name=You -c user.email=you@example.com commit -q -m "initial"
cd -
```

**Describe the project.** Save this as `$LAB/opsmith.toml`, replacing the path with the value
of `$LAB` on your machine:

```toml
[[projects]]
name = "shop"

[[projects.repositories]]
name = "shop"
scm_provider = "local"                                  # a git repository on this machine
url = "/home/you/opsmith-lab/repos/shop"                # must be inside OPSMITH_LOCAL_ROOT
test_command = "python3 -m unittest discover -s tests -q"

[[projects.services]]
name = "shop-api"                                       # the running thing that emits logs
repository = "shop"                                     # ...and the code it is built from
path_prefixes = ["app/"]

[[projects.connectors]]
name = "collector"
type = "push_logs"                                      # logs will be pushed to this connector
service = "shop-api"
```

**Create your account, apply the file, and start Opsmith:**

```bash
uv run opsmith init --tenant lab --admin "You"          # prints your API key once: copy it
uv run opsmith apply $LAB/opsmith.toml --tenant lab
uv run opsmith run                                      # API + scheduler + worker in one process
```

Leave that running. Open <http://127.0.0.1:8000/ui>, sign in with the key, and look at
*Configuration*: your repository, service and connector are there.

## 4. Send it some logs

In a second terminal, set your key and find the ids of what you just created:

```bash
export KEY=osk_...                                      # the key from `opsmith init`
export O=http://127.0.0.1:8000
auth="Authorization: Bearer $KEY"
id() { python3 -c "import json,sys; print(json.load(sys.stdin)[0]['id'])"; }

PROJECT=$(curl -s -H "$auth" $O/api/v1/projects | id)
CONNECTOR=$(curl -s -H "$auth" $O/api/v1/projects/$PROJECT/connectors | id)
SERVICE=$(curl -s -H "$auth" $O/api/v1/projects/$PROJECT/services | id)
REPO=$(curl -s -H "$auth" $O/api/v1/projects/$PROJECT/repositories | id)
```

Now push sixty error events, as a log shipper would. Their stack traces point at line 7 of
`app/orders.py`, the HTTP call:

```bash
python3 - > /tmp/logs.json <<'EOF'
import datetime, json
now = datetime.datetime.now(datetime.timezone.utc)
print(json.dumps([
    {
        "timestamp": (now - datetime.timedelta(minutes=120 - i)).isoformat(),
        "service": "shop-api",
        "severity": "error",
        "message": f"stock lookup timed out after {30000 + i}ms",
        "attributes": {"stack": 'Traceback:\n  File "app/orders.py", line 7, in fetch_stock'},
    }
    for i in range(60)
]))
EOF

curl -s -X POST $O/api/v1/connectors/$CONNECTOR/push \
  -H "$auth" -H 'Content-Type: application/json' --data @/tmp/logs.json
```

The answer says how many records were stored: `{"batches": [...], "records": 60}`.

## 5. Analyze and audit

Normally the scheduler runs these on their own (analysis every six hours per service, audits
nightly). To see results now, trigger both:

```bash
curl -s -X POST $O/api/v1/projects/$PROJECT/trigger -H "$auth" -H 'Content-Type: application/json' \
  -d "{\"job\": \"inference\", \"target_id\": \"$SERVICE\"}"

curl -s -X POST $O/api/v1/projects/$PROJECT/trigger -H "$auth" -H 'Content-Type: application/json' \
  -d "{\"job\": \"audit\", \"target_id\": \"$REPO\", \"mode\": \"full\"}"
```

Give it a few seconds, then open *Review queue* in the UI (or `GET
/api/v1/projects/$PROJECT/tasks`). With the sample repository you get three tasks:

| Priority | Task | Why |
|---|---|---|
| P1 | HTTP call without timeout in `app/orders.py` | The audit found the missing timeout **and** your logs show timeouts at the same line. Runtime and code evidence were merged into one task |
| P2 | Bare `except:` in `app/orders.py` | Found by the audit only |
| P3 | Repository has no LICENSE file | Found by the compliance audit |

## 6. Review and approve

Open the first task. Read the problem, the evidence and the proposed approach, then choose
*Approve* and submit. Nothing has touched your code until this moment, and nothing would
without it.

From the command line instead:

```bash
TASK=$(curl -s -H "$auth" "$O/api/v1/projects/$PROJECT/tasks?status=in_review" | id)
curl -s -X POST $O/api/v1/tasks/$TASK/review -H "$auth" -H 'Content-Type: application/json' \
  -d '{"decision": "approve"}'
```

Within a few seconds the task moves through `executing` to `pr_open`. *Executions* in the UI
shows what the agent did and that your test command passed.

## 7. Look at the pull request

With the `local` provider a "pull request" is a branch in your repository plus a record Opsmith
keeps. The change is real:

```bash
cd $LAB/repos/shop
git branch --list 'opsmith/*'
git diff main "$(git branch --list 'opsmith/*' | head -1 | tr -d ' ')"
```

```diff
-    response = httpx.get(f"https://inventory.example.com/stock/{item_id}")
+    response = httpx.get(f"https://inventory.example.com/stock/{item_id}", timeout=10)
```

Opsmith stops here. It never merges: that is your decision. With a GitHub repository this
branch would be a pull request on GitHub, carrying the evidence and a link back to the task.

Stop Opsmith with Ctrl+C when you are done. `rm -rf $LAB` removes everything this guide created.

## Where to go next

| You want to | Read |
|---|---|
| Understand what just happened | [Concepts](concepts.md) |
| Use a real model instead of the offline rules: Claude, OpenAI, or a local model | [Models](models.md) |
| Have a model run your own review instructions on a schedule ("audit for bugs and performance issues") | [Checks](checks.md) |
| Pull logs and metrics from Loki, Prometheus or files instead of pushing them | [Configuration](configuration.md#connectors) |
| Work on GitHub repositories, with isolated execution | [Deployment](deployment.md) |
| Know exactly what is guaranteed, and what is not | [Security](security.md) |
| Script it | [API](api.md) |
