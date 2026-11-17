# Deployment

How to install Opsmith for real use: the processes, the two ways to run them, the execution
sandbox, connecting GitHub, and every environment variable. For a first look on your own
machine, start with [Getting started](getting-started.md) instead.

- [What you are deploying](#what-you-are-deploying)
- [Option 1: Docker Compose](#option-1-docker-compose)
- [Option 2: Processes on a host](#option-2-processes-on-a-host)
- [Execution sandbox](#execution-sandbox)
- [Connecting a GitHub repository](#connecting-a-github-repository)
- [Environment variables](#environment-variables)

Related pages: [Models](models.md) for choosing a model provider, and
[Operations](operations.md) for running, upgrading and troubleshooting.

## What you are deploying

Three kinds of process share one database:

| Process | Command | What it does | How many |
|---|---|---|---|
| API and UI | `opsmith api` | REST API, review UI, `/healthz`, `/metrics` | one or more |
| Scheduler | `opsmith scheduler` | Enqueues jobs that are due | one is enough; more are safe |
| Worker | `opsmith worker` | Runs every pipeline job, including executions | one or more |

Supporting pieces:

- **PostgreSQL** holds all state, including the job queue. SQLite works for one machine and one
  worker; use PostgreSQL for anything shared or long-lived.
- **The data directory** (`OPSMITH_DATA_DIR`) holds stored signals. Every worker and the API
  need the same directory (a shared volume).
- **A sandbox** for running repository checks. This is the one part you must set up
  deliberately; see [Execution sandbox](#execution-sandbox).

`opsmith run` starts all three processes in one, for development only.

## Option 1: Docker Compose

Requirements: Docker Engine with the Compose plugin.

```bash
git clone https://github.com/deadboyccc/Opsmith.git && cd Opsmith
cp .env.example .env
```

Edit `.env` and set `POSTGRES_PASSWORD`. There is no default: the stack refuses to start
without it, so a copied deployment can never run with a known password.

```bash
echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)" >> .env   # hex is safe inside the database URL
docker compose up -d --build
docker compose exec api opsmith init --tenant acme --admin "Your Name"
```

`init` prints an API key once. Store it; it is your admin login. Then:

1. Open <http://localhost:8000/ui> and sign in with the key.
2. Describe your projects in a TOML file and apply it
   (see [Configuration](configuration.md)):

   ```bash
   docker compose cp opsmith.toml api:/tmp/opsmith.toml
   docker compose exec api opsmith apply /tmp/opsmith.toml --tenant acme
   ```

3. Create keys for other people and for log collectors:

   ```bash
   docker compose exec api opsmith create-user --tenant acme --name "Riley" --role reviewer
   docker compose exec api opsmith create-user --tenant acme --name "collector" --role service
   ```

The API is published on `127.0.0.1:8000` only. To expose it, put a TLS-terminating reverse
proxy in front and set `OPSMITH_BASE_URL` to the public URL.

To serve Opsmith under one path of a domain that hosts other things (for example
`https://example.org/opsmith/`), set `OPSMITH_BASE_PATH=/opsmith` and route that path to it
without stripping the prefix. The API, the UI, its assets and the API reference then live
under the path, and the sign-in cookie is scoped to it. With Caddy:

```caddyfile
redir /opsmith /opsmith/ 308
handle /opsmith/* {
	reverse_proxy opsmith:8000
}
```

Put your own access control in front of the route if the domain's users should not all
reach Opsmith's sign-in page (Caddy's `forward_auth`, for example).

At this point Opsmith ingests, analyzes, audits, ranks and takes reviews. It will **not**
execute an approved task yet: the worker has no sandbox. An approval in this state returns the
task to the review queue with the reason `sandbox unavailable`. Giving it one is the
[Execution sandbox](#execution-sandbox) section below. To use a real model instead of the
offline rules, see [Models](models.md).

## Option 2: Processes on a host

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), git, and PostgreSQL.

```bash
uv sync --extra postgres --extra anthropic --no-dev
export OPSMITH_DATABASE_URL=postgresql+psycopg://opsmith:<password>@db-host:5432/opsmith
export OPSMITH_DATA_DIR=/var/lib/opsmith/data
uv run opsmith init --tenant acme --admin "Your Name"
uv run opsmith api --host 127.0.0.1 --port 8000     # one unit
uv run opsmith scheduler                            # another
uv run opsmith worker                               # another; run as a non-root user
```

Run each as its own service (systemd, a supervisor, or containers). A worker on a host that has
Docker can use the Docker sandbox directly, with no extra wiring.

## Execution sandbox

When a human approves a task, the worker checks out the repository, lets the coding agent edit
files, and runs the repository's own `build`, `lint` and `test` commands. Those commands are
code from the repository, and the agent can add tests that they execute. Treat all of it as
untrusted: it must not reach your network, your credentials or the worker itself.

`OPSMITH_SANDBOX_BACKEND` selects how those commands run:

| Backend | Isolation | Use it for |
|---|---|---|
| `docker` (default) | A new container per command: no network, read-only root filesystem, all capabilities dropped, `no-new-privileges`, non-root user, CPU/memory/process limits, only the checkout mounted | Every real repository |
| `local` | None. A subprocess on the worker with a scrubbed environment and a timeout | Development, tests, the demo |

The policy is enforced in code and fails closed:

- `local` is **refused** for any repository that is not on the `local` SCM provider. A GitHub
  repository cannot be executed without real isolation.
- If `docker` is selected but the Docker CLI or daemon is unavailable, the execution is refused
  with `sandbox unavailable`. Opsmith never falls back to a weaker sandbox.

### The sandbox image

Set `OPSMITH_SANDBOX_IMAGE` to an image that **already contains everything your checks need**:
the language runtime, the test runner and the project's dependencies. The sandbox has no
network, so `pip install`, `npm ci` and similar commands cannot work inside it. Build an image
from your project's own CI image or Dockerfile, with dependencies installed, and point Opsmith
at it. A check that tries to download something fails, and the task returns to review with the
check's output.

One image serves every repository a worker handles. If your repositories need different
toolchains, run separate deployments or a combined image for now.

### Giving a containerized worker a sandbox

A worker running inside a container needs a way to start sandbox containers. The overlay
`docker-compose.sandbox.yml` provides it:

```bash
# in .env
OPSMITH_WORK_DIR=/var/lib/opsmith/work
DOCKER_GID=<output of: stat -c %g /var/run/docker.sock>
```

```bash
sudo install -d -o 10001 -g 10001 -m 0750 /var/lib/opsmith/work
docker compose -f docker-compose.yml -f docker-compose.sandbox.yml up -d --build
```

What the overlay does:

- Mounts the host's Docker socket into the worker so it can start sandbox containers.
- Mounts `OPSMITH_WORK_DIR` at the **same path** on the host and in the worker. The host's
  Docker daemon mounts the checkout into the sandbox, so both must see it at one path.

Understand the trade-off before you use it. Access to the Docker socket is equivalent to root on
that host. The worker is trusted code and the socket is never passed into a sandbox, but the
worker container is now privileged. Put it on a dedicated host or VM, away from anything you
would not want a compromised worker to reach. A worker installed directly on a host that has
Docker ([Option 2](#option-2-processes-on-a-host)) needs no socket mount at all.

### What the platform does around the sandbox

- The agent has no shell. It can list, read and write files inside the checkout and run the
  three configured checks by name, nothing else.
- The checkout has its remote removed and holds no credentials.
- Before the agent starts, Opsmith snapshots the repository metadata (`.git`) outside the
  checkout. Every later git command (status, diff, commit, push) uses only that snapshot, with
  hooks disabled. Anything a sandboxed command writes into the checkout's own `.git` is ignored.
- After the last check has run, the resulting diff is gated: protected paths and
  credential-like values block the pull request.
- The approval is re-checked immediately before the push.

## Connecting a GitHub repository

Create a fine-grained personal access token (or a GitHub App installation token) limited to the
repositories Opsmith should work on, with these repository permissions:

| Permission | Level | Why |
|---|---|---|
| Contents | Read and write | Clone, and push branches under `opsmith/` |
| Pull requests | Read and write | Open pull requests and read their state |
| Checks, Commit statuses | Read | Track CI on the pull request |

Then:

1. Protect the default branch in GitHub so the token cannot push to it. Opsmith refuses to push
   to the default branch or to any branch outside `opsmith/`, and has no merge operation at all;
   branch protection makes that hold even if the token leaks.
2. Make the token available to the **worker** as an environment variable (for example
   `GITHUB_TOKEN` in `.env`) or as a file under `OPSMITH_SECRETS_DIR`.
3. Reference it from the repository configuration: `credentials_ref = "env:GITHUB_TOKEN"` or
   `credentials_ref = "file:/run/secrets/github_token"`. The database stores only the reference.

## Environment variables

All are optional unless marked. [`.env.example`](../../.env.example) lists the same settings.

| Variable | Default | Meaning |
|---|---|---|
| `POSTGRES_PASSWORD` | none, **required by Compose** | Database password. Use URL-safe characters |
| `OPSMITH_DATABASE_URL` | SQLite file in the data dir | `postgresql+psycopg://user:pass@host:5432/db` |
| `OPSMITH_DATA_DIR` | `./data` | Stored signals, local PR state, SQLite database |
| `OPSMITH_SANDBOX_BACKEND` | `docker` | `docker` or `local` (development only) |
| `OPSMITH_SANDBOX_IMAGE` | `python:3.12-slim` | Image for sandbox containers; must contain your toolchain and dependencies |
| `OPSMITH_WORK_DIR` | system temp dir | Where execution checkouts are created |
| `OPSMITH_LOCAL_ROOT` | unset (disabled) | The only directory file connectors and `local` repositories may read |
| `OPSMITH_SECRETS_DIR` | `/run/secrets` | `file:` credential references must be inside it |
| `OPSMITH_LLM_PROVIDER_ALLOWLIST` | `heuristic,anthropic,openai` | Providers that may receive data |
| `OPSMITH_OPENAI_BASE_URL` | `https://api.openai.com/v1` | Base URL of the OpenAI-compatible endpoint used by the `openai` provider |
| `OPSMITH_OPENAI_API_KEY_REF` | `env:OPENAI_API_KEY` | Where that endpoint's key comes from. A local server needs none |
| `OPSMITH_OPENAI_JSON_MODE` | `json_schema` | `json_object` for endpoints that only support plain JSON mode |
| `OPSMITH_ANTHROPIC_API_KEY_REF` | `env:ANTHROPIC_API_KEY` | Where the Claude API key comes from |
| `OPSMITH_ANTHROPIC_FALLBACKS` | `default` | Server-side refusal fallbacks for Claude; `off` disables |
| `OPSMITH_BASE_URL` | unset | Public URL of the UI; makes pull requests link to their task |
| `OPSMITH_BASE_PATH` | unset | Serve the API and UI under this path (`/opsmith`) instead of the root, behind a proxy that routes that path here |
| `OPSMITH_WORKER_LEASE_SECONDS` | `900` | How long after a worker dies its job is retried |
| `OPSMITH_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`, in either case. Anything else stops the process at start |
| `OPSMITH_LOG_JSON` | `true` | JSON logs; `false` for plain text |
| `OPSMITH_BOT_NAME`, `OPSMITH_BOT_EMAIL` | `Opsmith Bot`, a noreply address | Author of agent commits |
| `DOCKER_GID` | none | Only with the sandbox overlay: group id of the Docker socket |

An invalid value stops the process at start with a message naming the setting.

