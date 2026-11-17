# Security policy

## Reporting a vulnerability

Please open an issue with the **Security problem** form: *Issues*, *New issue*, *Security
problem*. Security problems are handled in the open, like any other bug, so that everyone who
runs Opsmith can see what is known and when it is fixed.

If you would rather not post the details publicly, email the maintainer listed in
[`pyproject.toml`](pyproject.toml) instead.

Include what you did, what happened and what you expected, and leave out secrets, tokens and
real data. You will get an acknowledgement, and credit in the [changelog](CHANGELOG.md) if
you want it.

## Supported versions

Fixes go into the next release. Only the latest release is supported, so upgrade to it to
receive them.

## What is guaranteed

[The security guide](docs/guide/security.md) lists every guarantee Opsmith makes, how each is
enforced and which test proves it, and what is not protected. The findings of the last
security audit, including those still open, are in [docs/SECURITY-AUDIT.md](docs/SECURITY-AUDIT.md).
