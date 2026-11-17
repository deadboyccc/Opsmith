# Opsmith documentation

Two sets of pages, for two kinds of reader. They are also published, with search, as the
documentation site at <https://deadboyccc.github.io/Opsmith/>.

## Using Opsmith

For people who run it: evaluating it, configuring projects, operating a deployment.

| Page | Read it to |
|---|---|
| [Getting started](guide/getting-started.md) | Go from a fresh clone to your own project's first pull request, on your machine |
| [UI tour](guide/ui-tour.md) | See what it looks like, with screenshots of every page |
| [Concepts](guide/concepts.md) | Understand the terms, the pipeline and the lifecycles |
| [Configuration](guide/configuration.md) | Describe projects: repositories, services, connectors, budgets, protected paths, notifications |
| [Models](guide/models.md) | Choose a model provider (offline rules, any OpenAI-compatible endpoint, Claude) and set models per stage |
| [Deployment](guide/deployment.md) | Install it for real: Compose or processes, the execution sandbox, GitHub, environment variables |
| [Operations](guide/operations.md) | Monitor it, upgrade it, troubleshoot it |
| [API](guide/api.md) | Authenticate, understand roles, call the REST API |
| [Security](guide/security.md) | Know what is guaranteed, how, which test proves it, and what is not protected |
| [Security audit](SECURITY-AUDIT.md) | See what the last audit looked for, what it found and fixed, and what is still open |
| [Compatibility](guide/compatibility.md) | See which versions of Loki, Prometheus, GitHub, models, Python, PostgreSQL and Docker are verified, and how |

## Developing Opsmith

For people who change it.

| Page | Read it to |
|---|---|
| [CONTRIBUTING.md](../CONTRIBUTING.md) | Set up, make a change, open a pull request |
| [AGENTS.md](../AGENTS.md) | Learn the invariants and conventions (for people and for AI coding agents) |
| [Architecture](development/architecture.md) | Understand the internals: processes, the job pipeline, the model layer, the execution path, the code map, plugins |
| [Testing](development/testing.md) | Run and write tests, including on PostgreSQL, with real containers and against a live model |
| [Architecture decisions](../ARCHITECTURE_DECISIONS.md) | See why each design choice was made, and the known limitations |
| [Decisions](DECISIONS.md) | See how individual fixes and tooling choices were decided, and what was left open on purpose |
| [Changelog](../CHANGELOG.md) | See what changed |
| [Repository About text](development/repository-about.md) | Copy-paste the description, website and topics for the GitHub About section |

## Background

| Page | What it is |
|---|---|
| [Specification](development/specification.md) | The original product specification |
| [Independent project review](development/independent-project-review.md) | An outside assessment of the project |
| [Review response](development/review-response.md) | What was done about each point in that review |
| [Work reports](../reports/) | Summaries of larger pieces of work: what was done, how it was verified, what is still open |
