"""Memory subsystem (Phase 12) — governed, bitemporal, Beta-confidence beliefs.

Every module here MUST check `settings.MEMORY_ENABLED` before doing anything
observable. The whole subsystem is no-op when the flag is off; the regression
tripwire in tests/test_memory_regression.py verifies that.
"""
