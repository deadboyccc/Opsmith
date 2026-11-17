# Security policy

## Reporting a vulnerability

Please report a security problem privately, not in a public issue, so that it can be fixed
before it is known:

- through GitHub's private vulnerability reporting: the **Security** tab of this repository,
  then **Report a vulnerability**; or
- by email to the maintainer listed in [`pyproject.toml`](pyproject.toml).

Include what you did, what happened and what you expected. You will get an acknowledgement,
and credit in the [changelog](CHANGELOG.md) if you want it.

## Supported versions

Fixes go into the next release. Only the latest release is supported, so upgrade to it to
receive them.

## What is guaranteed

[The security guide](docs/guide/security.md) lists every guarantee Opsmith makes, how each is
enforced and which test proves it, and what is not protected. The findings of the last
security audit, including those still open, are in [docs/SECURITY-AUDIT.md](docs/SECURITY-AUDIT.md).
