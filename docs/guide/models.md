# Models

Opsmith is not tied to one model vendor. This page explains the choices, how an operator makes
a provider available, and how a project picks models for each stage.

- [The three providers](#the-three-providers)
- [Operator: make a provider available](#operator-make-a-provider-available)
- [Project: choose models per stage](#project-choose-models-per-stage)
- [Prices and budgets](#prices-and-budgets)
- [What a model needs to be able to do](#what-a-model-needs-to-be-able-to-do)
- [Keeping data on your own machines](#keeping-data-on-your-own-machines)
- [Adding another provider](#adding-another-provider)

## The three providers

| Provider | What it is | Use it when |
|---|---|---|
| `heuristic` (default) | Deterministic rules, built in. No model, no network | Trying Opsmith, air-gapped installs, CI. Limited to the patterns it knows |
| `openai` | **Any OpenAI-compatible endpoint:** OpenAI, Azure OpenAI, Ollama, vLLM, LM Studio, llama.cpp, LiteLLM, OpenRouter, Groq, Mistral, Together and others | You have such an endpoint, hosted or local |
| `anthropic` | Claude, through the official SDK | You use Claude |

Models are used in five places, called **stages**:

| Stage | What the model does there |
|---|---|
| `inference` | Reads log and metric bundles plus open leads; reports findings and lead updates |
| `audit_triage` | Filters false positives out of audit findings and calibrates confidence |
| `check` | Carries out a [check](checks.md): reads the files in its scope with your instruction, and reports problems that quote the code. The offline rules cannot do this; a check needs a model |
| `task` | Drafts the task a human will review: title, problem, approach, acceptance criteria |
| `executor` | The coding agent: edits files and runs the checks for an approved task |

Each stage can use a different provider and model.

## Operator: make a provider available

Providers are switched on with environment variables on the **worker**
([all variables](deployment.md#environment-variables)). Nothing is needed for `heuristic`.

**A local model with [Ollama](https://ollama.com):**

```bash
ollama pull qwen2.5-coder:14b
# in .env: an address the worker can reach (http://localhost:11434/v1 when both run on one host)
OPSMITH_OPENAI_BASE_URL=http://ollama-host:11434/v1
```

**A hosted OpenAI-compatible service:**

```bash
OPSMITH_OPENAI_BASE_URL=https://api.openai.com/v1      # the default
OPENAI_API_KEY=sk-...
```

**Claude:**

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

The container image includes the Anthropic SDK. Elsewhere install the extra:
`uv sync --extra anthropic`.

Notes for the `openai` provider:

- The endpoint URL is operator configuration on purpose. A project can choose a model, never
  the URL model calls go to.
- One endpoint per deployment. To reach several backends, put a gateway such as LiteLLM in
  front and point `OPSMITH_OPENAI_BASE_URL` at it.
- For Azure OpenAI and other gateways, use the OpenAI-compatible base path your deployment
  exposes.
- If the endpoint does not support `response_format: json_schema`, Opsmith notices and falls
  back to plain JSON mode. Set `OPSMITH_OPENAI_JSON_MODE=json_object` to skip the probe.
- A local server usually needs no key; leave `OPENAI_API_KEY` unset.

`OPSMITH_LLM_PROVIDER_ALLOWLIST` (default `heuristic,anthropic,openai`) is the process-wide list
of providers that may receive data at all.

## Project: choose models per stage

In the project's settings ([Configuration](configuration.md#projects-and-settings)):

```toml
[projects.settings]
executor_agent = "openai"                  # who implements approved tasks: heuristic | claude | openai

[projects.settings.llm.default]            # used by every stage not listed under llm.stages
provider = "openai"
model = "qwen2.5-coder:14b"                # whatever your endpoint calls the model
max_tokens = 8000
input_price_per_mtok = 0                   # USD per million tokens; 0 for a local model
output_price_per_mtok = 0

[projects.settings.llm.stages.executor]    # the coding agent can use a different model
provider = "openai"
model = "qwen2.5-coder:32b"
input_price_per_mtok = 0
output_price_per_mtok = 0
```

The same with Claude:

```toml
[projects.settings]
executor_agent = "claude"

[projects.settings.llm.default]
provider = "anthropic"
model = "claude-opus-5-5"
effort = "medium"                          # low | medium | high | xhigh | max (Claude only)

[projects.settings.llm.stages.executor]
provider = "anthropic"
model = "claude-opus-5-5"
effort = "high"
```

You can mix them, for example a local model for triage and a hosted one for the coding agent:

```toml
[projects.settings.llm.stages.audit_triage]
provider = "openai"
model = "qwen2.5-coder:7b"
input_price_per_mtok = 0
output_price_per_mtok = 0
```

`executor_agent` goes with the executor stage:

| `executor_agent` | Needs |
|---|---|
| `heuristic` | Nothing. Applies a small set of deterministic fixes and gives up on everything else |
| `openai` | The `executor` stage (or `llm.default`) set to an `openai` model |
| `claude` | Uses the `executor` stage's model when it is an `anthropic` one, otherwise `claude-opus-5-5` |

To restrict one project further than the process allows:

```toml
[projects.settings.llm]
provider_allowlist = ["heuristic"]         # this project never sends data to a remote model
```

## Prices and budgets

Budgets ([Configuration](configuration.md#budgets)) cap tokens and cost per run and per
project per day, for every stage including the coding agent. For the cost caps to mean
something, Opsmith needs to know what a model costs:

- It has list prices for `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5` and
  `claude-fable-5-2`.
- For any other model, set `input_price_per_mtok` and `output_price_per_mtok` on the stage.
- A model with no price is costed at **the most expensive rate Opsmith knows**, so a budget is
  never bypassed by an unpriced model. A local model therefore appears to cost money until you
  set its prices to 0.

Calls that fail, are refused or are cut off are still recorded and counted. A reply from an
OpenAI-compatible endpoint that reports no token usage is counted by estimate (about four
characters to a token) instead of as free. Input that Claude reads from or writes to its
prompt cache counts as input.

An OpenAI-compatible endpoint that answers 429, 500, 502, 503 or 504, or refuses the
connection, is asked twice more (after 1 and 2 seconds, or after its `Retry-After`) before
the call fails. A failure of any provider reaches the stages the same way: the run is
recorded as `failed` with what it had already spent, and task drafting falls back to the
built-in rules.

## What a model needs to be able to do

- **Return JSON that matches a schema.** Every stage has one. Opsmith asks the endpoint to
  enforce it where possible and validates the result itself either way, with one corrective
  retry. A model that cannot produce valid output makes that run `failed`; nothing is guessed.
- **Tool calling, for the coding agent.** Pick a model your endpoint serves with function or
  tool calling enabled.
- **Enough context** for a bundle (a few thousand tokens) plus `max_tokens` of output.

The protocol is the same for every model; the quality of the findings and fixes is the model's.
Small local models work and find less. `effort` is passed to Claude only.

## Keeping data on your own machines

Signals are redacted before they are stored and again immediately before every model call.
Redaction removes recognizable secrets and personal data; the rest of a bundle (log templates,
metric names, code excerpts) is sent to the provider you chose.

To keep everything local, use `heuristic`, or `openai` with an endpoint you host, and set
`OPSMITH_LLM_PROVIDER_ALLOWLIST=heuristic,openai` (or just `heuristic`) so nothing else can be
selected by any project.

## Adding another provider

A provider is a Python object with a `name` and a `complete(request)` method. Package it and
register it in the `opsmith.llm_providers` entry-point group; Opsmith loads it at start. See
[development/architecture.md](../development/architecture.md#extending-opsmith).
