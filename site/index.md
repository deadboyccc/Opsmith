---
title: Opsmith
description: Production signals in, reviewed pull requests out. Self-hosted engineering ops with a human approval on every change.
hide:
  - navigation
  - toc
---

<div class="os-hero" markdown>

<div class="os-hero__text" markdown>

<img class="os-hero__mascot" src="docs/images/brand/mascot.svg" width="112" height="112" alt="Smithy, the Opsmith mascot: a friendly robot blacksmith in an orange apron, hammer raised over an anvil">

# Production signals in. Reviewed pull requests out.

Opsmith watches your logs, metrics and repositories, links what production shows to what
the code shows, and ranks the result as engineering tasks. When a person approves a task, a
sandboxed coding agent implements it and opens a pull request. After a person merges it,
Opsmith checks that the problem is actually gone.

[Get started](docs/guide/getting-started.md){ .md-button .md-button--primary }
[See it in a minute](#see-it-in-a-minute){ .md-button }

</div>

<picture class="os-hero__art">
  <source type="image/webp" srcset="assets/brand/social-preview-640.webp 640w, assets/brand/social-preview-1280.webp 1280w" sizes="(min-width: 76em) 560px, (min-width: 45em) 45vw, 100vw">
  <img src="docs/images/brand/social-preview.png" width="1280" height="640" fetchpriority="high" alt="Opsmith: production signals in, reviewed pull requests out. Smithy, a robot blacksmith, forges a glowing pull request on an anvil.">
</picture>

</div>

<div class="os-rules" markdown>

**Two rules are never broken.** Nothing changes code without a recorded human approval, and
Opsmith cannot merge. A human always merges.

</div>

## See it in a minute

You need [uv](https://docs.astral.sh/uv/) and git. No API key, no Docker, no network.

```bash
git clone https://github.com/deadboyccc/Opsmith.git && cd Opsmith
uv sync --all-extras
uv run opsmith demo --dir ./opsmith-demo   # ends with "final_status": "verified"
```

The demo builds a small repository with a real bug, feeds in logs and metrics, finds the
problem from both sides, has a reviewer approve the fix, opens a pull request, merges it as a
human would, and confirms the problem is gone. The [overview](overview.md) shows it as a
one-minute video; here it is in thirty seconds:

<div class="os-reel">
  <video controls muted playsinline preload="none" width="1080" height="1920" poster="docs/images/reel-poster.jpg" aria-label="Opsmith in 30 seconds: it reads your logs, metrics and code, a human approves every change, a sandboxed agent writes the fix and opens a pull request, it never merges, and after the merge it checks the bug is really gone.">
    <source src="docs/images/reel.mp4" type="video/mp4">
    <a href="docs/images/reel.mp4">Watch the 30-second video</a>
  </video>
</div>

## Where to go next

<div class="grid cards" markdown>

-   **New here**

    ---

    1. [Overview](overview.md): what it does and how it works
    2. [Getting started](docs/guide/getting-started.md): your own project, on your machine
    3. [Concepts](docs/guide/concepts.md): leads, findings, tasks and their lifecycles
    4. [UI tour](docs/guide/ui-tour.md): every page, with screenshots

-   **Running it**

    ---

    - [Configuration](docs/guide/configuration.md) of projects, connectors and budgets
    - [Models](docs/guide/models.md): offline rules, any OpenAI-compatible endpoint, or Claude
    - [Deployment](docs/guide/deployment.md) with Compose and the sandbox
    - [Operations](docs/guide/operations.md): monitoring, upgrades, troubleshooting
    - [API](docs/guide/api.md) reference

-   **Trusting it**

    ---

    - [Security](docs/guide/security.md): every guarantee and the test that proves it
    - [Security audit](docs/SECURITY-AUDIT.md): what was found, fixed and left open
    - [Compatibility](docs/guide/compatibility.md): what is verified, and how

-   **Changing it**

    ---

    - [Contributing](CONTRIBUTING.md) and the [guide for AI agents](AGENTS.md)
    - [Architecture](docs/development/architecture.md) and [testing](docs/development/testing.md)
    - [Architecture decisions](ARCHITECTURE_DECISIONS.md) and [decisions](docs/DECISIONS.md)
    - [Changelog](CHANGELOG.md)

</div>
