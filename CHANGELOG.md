# Changelog

All notable changes to Nexus Agent are documented here.

## [Unreleased]

### Added
- Apache 2.0 license
- Comprehensive README with full API reference and architecture guide
- Test coverage reporting (`pytest-cov`) in CI and `make test-cov`
- Pinned dependency lockfile (`requirements.lock`)
- Community files: CONTRIBUTING.md, SECURITY.md, CODE_OF_CONDUCT.md
- GitHub issue/PR templates
- Example scripts for quick demonstration
- ClawGuard beta7 compatibility hardening: canonical `/v1/agent/run` coverage, safer default tool policies, approval action-hash binding, reviewer identity validation, and tampered-resume rejection.

### Changed
- Agent `file_write` and general `shell_exec` seeded policies now require approval by default; destructive shell patterns remain high-risk approval or deny paths.
- Beta/production startup now requires `APPROVAL_REVIEWERS` so approval votes are constrained to configured reviewer identities.

## [0.1.0] - 2025-06-01

Initial release with all 7 phases complete.

### Core Pipeline
- 7-step zero-trust pipeline: input scan, A-S-FLC analysis, LLM generation, critic evaluation, governance check, output scan, trace persistence
- Multi-provider LLM support: Gemini, OpenAI, DeepSeek, local HuggingFace, mock fallback
- SSE streaming via `POST /api/agent/stream`
- Multi-model comparison via `POST /api/agent/compare`

### Security (Agent-Immune)
- 11-language prompt injection detection (English, Chinese, Russian, Arabic, Hindi, Japanese, Korean, Spanish, French, German, Portuguese)
- Semantic Memory Bank for fuzzy-matching known attack patterns
- Session escalation tracking for repeated suspicious prompts
- Prompt hardening (injection fragment stripping) for flagged inputs
- Output scanning for leaked secrets and sensitive data

### Governance (Covernor)
- Default-deny policy engine with pattern-matching rules
- K-of-N human approval workflow with quorum
- ECDSA capability tokens as cryptographic proof of authorization
- Approval expiration and concurrency-safe voting

### Critic System (Arbiter)
- Pluggable critic tree with DB-backed node registry
- Four built-in nodes: Reasoning, Injection, Safety, Quality
- Hot-swappable prompt templates and LoRA adapters
- Streaming critic evaluation with `[UNC]` uncertainty markers
- Re-evaluation endpoint for drift detection

### Training Flywheel
- Automatic failure-to-labeling-queue pipeline
- Human labeling API and dashboard UI
- Training data export with evidential uncertainty enrichment
- Doctrine Lab integration (dataset import, fine-tuning, adapter promotion)
- ECE calibration tracking with persistence
- Background scheduler for automated exports and retention

### Decision Engine (A-S-FLC)
- LLM-powered path decomposition for complex prompts
- Asymmetric risk evaluation (negative outcomes penalized harder)
- Convergence-based iteration with configurable thresholds
- System hint generation for guided LLM generation

### Infrastructure
- FastAPI with Pydantic v2 and SQLAlchemy 2.0
- Alembic migrations (SQLite + PostgreSQL)
- API key authentication with timing-safe comparison
- Rate limiting on all expensive endpoints
- Browser dashboard: trace explorer, labeling queue, approval console, calibration chart
- Prometheus metrics (opt-in)
- Structured JSON logging with request ID correlation
- Hash-chained tamper-evident audit traces
- Docker support (SQLite and PostgreSQL profiles)
- GitHub Actions CI: lint + type check + SQLite tests + PostgreSQL tests
- Configurable data retention with scheduled purge
- 384 tests across 20 test files
