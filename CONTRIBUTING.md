# Contributing to Nexus Agent

Thanks for your interest in contributing! This guide will help you get started.

## Development Setup

```bash
git clone https://github.com/denial-web/nexus-agent.git
cd nexus-agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt
alembic upgrade head
```

Verify everything works:

```bash
pytest tests/ -v
```

All 384 tests should pass.

## Making Changes

1. **Fork the repo** and create a feature branch from `master`.
2. **Write tests** for any new functionality. Every module in `app/` has a corresponding `tests/test_*.py` file.
3. **Run the full suite** before submitting:

```bash
make test        # all tests
make lint        # ruff linting
make typecheck   # mypy type checking
```

4. **Follow the existing code style**:
   - Python 3.13, type hints on all function signatures
   - Imports at file top (no inline imports except circular dep avoidance)
   - `datetime.now(timezone.utc)` not `datetime.utcnow()`
   - Pydantic v2: `model_config = ConfigDict(...)` not `class Config`
   - Logging via `logging.getLogger(__name__)`, never `print()`
   - No unnecessary comments — code should be self-documenting

5. **Open a pull request** with a clear description of what changed and why.

## Architecture Rules

Before contributing, read [AGENTS.md](AGENTS.md) for the full architecture overview. Key constraints:

- **Default-deny governance**: The Covernor policy engine blocks unknown actions. Never change this default.
- **Pipeline order is fixed**: The 7-step pipeline in `app/agent/pipeline.py` must not be reordered.
- **Critic tree pattern**: New evaluation dimensions should be new leaf nodes registered with the Arbiter, not inline scoring logic.
- **Trace everything**: Every pipeline step must write to the Trace model. No silent failures.
- **Labeling queue**: Any critic halt or failure must push to the labeling queue.

## Database Migrations

After any model change in `app/models/`:

```bash
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

Test both directions to catch issues early.

## What to Contribute

- Bug fixes (always welcome)
- New critic node types (follow the pattern in `app/core/critic/nodes.py`)
- Injection detection patterns for additional languages
- Dashboard improvements
- Documentation and examples
- Performance improvements with benchmarks

## Reporting Issues

Use the [GitHub issue tracker](https://github.com/denial-web/nexus-agent/issues). For security vulnerabilities, see [SECURITY.md](SECURITY.md) instead.

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By participating, you agree to uphold it.
