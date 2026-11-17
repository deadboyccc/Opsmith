# Compatibility

What Opsmith talks to, which versions it has been verified against, and how. "Live" means a
real server of that version, started in CI on every push and pull request. "Mocked" means
tests against recorded or scripted HTTP responses only: the code is tested, the pairing with
the real service is not.

- [Signal connectors](#signal-connectors)
- [Source control](#source-control)
- [Models](#models)
- [Runtime](#runtime)
- [Adding a version](#adding-a-version)

## Signal connectors

| Connector | Talks to | Verified against | How |
|---|---|---|---|
| `loki` | Grafana Loki, `GET /loki/api/v1/query_range` | **Loki 2.9.17, 3.7.10** | Live, every push |
| `prometheus` | Prometheus, `GET /api/v1/query_range` | **Prometheus v2.53.5, v3.5.5 (LTS), v3.16.0** | Live, every push |
| `victorialogs` | VictoriaLogs, `GET /select/logsql/query` | **VictoriaLogs v1.53.0** | A scripted server in every push; run by hand against a real v1.53.0 server. Not live in CI yet |
| `file_logs`, `file_metrics` | JSONL and plain-text files under `OPSMITH_LOCAL_ROOT` | Formats in [Configuration](configuration.md#connectors) | Unit tests on real files |
| `push_logs`, `push_metrics` | Your collector, calling Opsmith's API | [API](api.md) | API tests |

What the live tests check (`tests/test_connectors_live.py`):

- **Loki:** every entry in a window is read exactly once while paging through it, including
  several entries on the same nanosecond at a page boundary; JSON lines are unwrapped into
  message, level and attributes; the service and environment come from the stream labels;
  the window's start is included and its end excluded; an invalid LogQL query is reported
  as an error.
- **Prometheus:** scraped series arrive with their labels and the service label mapped; a
  constant has a point at every step of the baseline and the window, both ends included;
  steps with no value are dropped, not stored; an invalid PromQL query is reported as an
  error.

Not covered by the live tests:

- **Authentication.** Both connectors send `credentials_ref` as a Bearer token. The test
  servers run without authentication, so this is tested against mocked HTTP only.
- **Services that offer the same API.** Grafana Cloud Logs, Grafana Mimir, Thanos,
  VictoriaMetrics and other Prometheus- or Loki-compatible services expose the same
  `query_range` endpoints and should work. None of them is tested.
- **Loki multi-tenancy** (`X-Scope-OrgID`) is not supported.

## Source control

| Provider | Talks to | Verified against | How |
|---|---|---|---|
| `github` | GitHub REST API at `api.github.com`: pull requests, check runs, commit statuses; git over HTTPS for clone and push | github.com | Mocked HTTP. Pushing and opening pull requests have not been run against GitHub in CI |
| `local` | A git repository on the worker's disk | git as installed | Real git, in every test that executes a task |

GitHub Enterprise Server (a different API URL) is not supported.

## Models

| Provider | Verified against | How |
|---|---|---|
| `heuristic` (offline rules) | Built in | Drives the test suite and the demo |
| `openai` (any OpenAI-compatible endpoint) | A scripted endpoint; **Ollama with `qwen2.5:1.5b`** | The whole acceptance scenario through the HTTP path, every push (`tests/test_provider_parity.py`); live against Ollama weekly (`live-models.yml`) |
| `anthropic` (Claude) | The `anthropic` SDK 2.8, over a mocked transport | The SDK's own request building and response parsing run for the structured stages and the coding agent, every push; not run against the Claude API in CI |

See [Models](models.md) for configuration.

## Runtime

| Component | Verified against | How |
|---|---|---|
| Python | 3.12 | Every push. Other versions are untested |
| FastAPI | 0.163 (the lock file); `pyproject.toml` allows 0.121 or newer, which is untested | Every push |
| PostgreSQL | 18.6 | The whole suite and the migrations, every push. Older versions are untested |
| SQLite | The version bundled with Python 3.12 | The whole suite, every push |
| Docker sandbox | Docker Engine on GitHub's `ubuntu-latest` runners, and 29.9 | Real containers, every push (`pytest -m docker`) |
| Notifications | Slack-compatible incoming webhooks | Mocked HTTP |

## Adding a version

To verify a new Loki or Prometheus version, add it to the `connectors` matrix in
`.github/workflows/ci.yml` and to the table above in the same pull request. To try one
locally first:

```bash
docker run -d --name loki -p 127.0.0.1:3100:3100 grafana/loki:3.7.10
OPSMITH_TEST_LOKI_URL=http://127.0.0.1:3100 uv run pytest tests/test_connectors_live.py -k loki
```
