# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Nexus Agent, **please do not open a public issue**.

Instead, report it privately:

1. Email: Open a [GitHub Security Advisory](https://github.com/denial-web/nexus-agent/security/advisories/new) (preferred)
2. Include a clear description of the vulnerability, steps to reproduce, and potential impact

We will acknowledge your report within 48 hours and aim to release a fix within 7 days for critical issues.

## Supported Versions

| Version | Supported |
|---------|-----------|
| latest `master` | Yes |
| older commits | Best-effort |

## Security Design

Nexus Agent is built with security as a core principle:

- **Default-deny governance**: Unknown actions are always blocked
- **Input scanning**: 11-language prompt injection detection with semantic memory
- **Output scanning**: Blocks leaked secrets and sensitive data in responses
- **Hash-chained traces**: Tamper-evident audit log with cryptographic integrity
- **ECDSA capability tokens**: Cryptographically signed authorization proofs
- **Timing-safe auth**: API key comparison uses SHA-256 digest comparison to prevent timing attacks
- **Rate limiting**: All expensive endpoints are rate-limited per IP
- **CSRF protection**: Optional CSRF tokens for dashboard forms
- **Session security**: Signed sessions with configurable secrets

## Scope

The following are in scope for security reports:

- Prompt injection bypasses (especially for non-English languages)
- Governance policy bypass or escalation
- Authentication or authorization flaws
- Hash chain integrity breaks
- Information leakage through error messages or timing
- Dependency vulnerabilities

The following are out of scope:

- Issues in mock mode (development-only, no real LLM calls)
- Denial of service via legitimate API usage (covered by rate limiting)
- Social engineering
