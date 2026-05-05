# Security policy

## Supported versions

Security fixes are applied to the default branch (`main`) when practical. Use the latest commit for deployments.

## Reporting a vulnerability

Please report security issues **privately** so they can be addressed before public disclosure.

1. **Preferred:** Open a **GitHub Security Advisory** on this repository (if enabled), or email the repository maintainers with subject line `[SECURITY] Plujka`.
2. Include enough detail to reproduce (affected component, steps, impact). Do not attach exploit code in the first message if unnecessary.

Maintainers will acknowledge receipt when possible and coordinate disclosure after a fix.

## Scope

In scope: this application code, Docker images defined in this repo, and documented configuration.

Out of scope: third-party services (OpenAI, PKW websites), misconfiguration of secrets on your infrastructure, or issues in dependencies unless they affect this repo directly.

## Safe defaults

- Do not commit API keys or `.env` files (see `.gitignore`).
- Restrict exposure of OpenSearch and PostgreSQL ports in production; the Compose file maps them for local development.
