"""Tests for circuit breaker dashboard visualization."""

from app.core.llm.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    get_registry,
    reset_registry,
)

# ---------------------------------------------------------------------------
# Enriched get_status()
# ---------------------------------------------------------------------------


class TestEnrichedStatus:
    def test_since_last_failure_none_when_no_failures(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig())
        status = cb.get_status()
        assert status["since_last_failure_seconds"] is None

    def test_since_last_failure_populated_after_failure(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=10))
        cb.record_failure()
        status = cb.get_status()
        assert status["since_last_failure_seconds"] is not None
        assert status["since_last_failure_seconds"] >= 0

    def test_recovery_remaining_none_when_closed(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig())
        status = cb.get_status()
        assert status["recovery_remaining_seconds"] is None

    def test_recovery_remaining_present_when_open(self):
        config = CircuitBreakerConfig(failure_threshold=2, recovery_timeout_seconds=60)
        cb = CircuitBreaker("test", config)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        status = cb.get_status()
        assert status["recovery_remaining_seconds"] is not None
        assert status["recovery_remaining_seconds"] > 0

    def test_rolling_window_in_status(self):
        config = CircuitBreakerConfig(rolling_window_seconds=120)
        cb = CircuitBreaker("test", config)
        status = cb.get_status()
        assert status["rolling_window_seconds"] == 120

    def test_half_open_successes_in_status(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig())
        status = cb.get_status()
        assert status["half_open_successes"] == 0


# ---------------------------------------------------------------------------
# Dashboard HTML routes
# ---------------------------------------------------------------------------


class TestCircuitBreakerDashboard:
    def setup_method(self):
        reset_registry()

    def teardown_method(self):
        reset_registry()

    def test_page_loads_empty(self, client):
        resp = client.get("/dashboard/circuit-breakers")
        assert resp.status_code == 200
        assert "Circuit Breakers" in resp.text
        assert "No circuit breakers registered" in resp.text

    def test_page_shows_provider(self, client):
        reg = get_registry()
        reg.get("gemini")
        resp = client.get("/dashboard/circuit-breakers")
        assert resp.status_code == 200
        assert "gemini" in resp.text
        assert "closed" in resp.text.lower()

    def test_page_shows_open_provider(self, client):
        reg = get_registry()
        cb = reg.get("openai")
        for _ in range(reg._default_config.failure_threshold):
            cb.record_failure()
        resp = client.get("/dashboard/circuit-breakers")
        assert resp.status_code == 200
        assert "openai" in resp.text
        assert "open" in resp.text.lower()
        assert "Reset to Closed" in resp.text

    def test_stats_counts(self, client):
        reg = get_registry()
        reg.get("gemini")
        cb = reg.get("openai")
        for _ in range(reg._default_config.failure_threshold):
            cb.record_failure()
        resp = client.get("/dashboard/circuit-breakers")
        assert resp.status_code == 200
        assert "Tracked Providers" in resp.text

    def test_reset_button_absent_for_closed(self, client):
        reg = get_registry()
        reg.get("gemini")
        resp = client.get("/dashboard/circuit-breakers")
        assert "Reset to Closed" not in resp.text

    def test_nav_link_present(self, client):
        resp = client.get("/dashboard/circuit-breakers")
        assert 'href="/dashboard/circuit-breakers"' in resp.text


# ---------------------------------------------------------------------------
# Reset endpoint
# ---------------------------------------------------------------------------


class TestCircuitBreakerReset:
    def setup_method(self):
        reset_registry()

    def teardown_method(self):
        reset_registry()

    def test_reset_open_provider(self, client):
        reg = get_registry()
        cb = reg.get("deepseek")
        for _ in range(reg._default_config.failure_threshold):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN

        resp = client.post(
            "/dashboard/circuit-breakers/deepseek/reset",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert cb.state == CircuitState.CLOSED

    def test_reset_unknown_provider_404(self, client):
        resp = client.post(
            "/dashboard/circuit-breakers/nonexistent/reset",
            follow_redirects=False,
        )
        assert resp.status_code == 404
        assert "not found" in resp.text.lower()

    def test_reset_redirects_to_dashboard(self, client):
        reg = get_registry()
        reg.get("gemini")
        resp = client.post(
            "/dashboard/circuit-breakers/gemini/reset",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/dashboard/circuit-breakers" in resp.headers.get("location", "")


# ---------------------------------------------------------------------------
# JSON API (existing endpoint, verify enriched fields)
# ---------------------------------------------------------------------------


class TestCircuitBreakerAPIEnriched:
    def setup_method(self):
        reset_registry()

    def teardown_method(self):
        reset_registry()

    def test_api_returns_enriched_fields(self, client):
        reg = get_registry()
        cb = reg.get("gemini")
        cb.record_failure()
        resp = client.get("/v1/agent/circuit-breakers")
        assert resp.status_code == 200
        data = resp.json()
        breaker = data["breakers"][0]
        assert "rolling_window_seconds" in breaker
        assert "since_last_failure_seconds" in breaker
        assert "half_open_successes" in breaker

    def test_api_recovery_remaining_when_open(self, client):
        reg = get_registry()
        cb = reg.get("openai")
        for _ in range(reg._default_config.failure_threshold):
            cb.record_failure()
        resp = client.get("/v1/agent/circuit-breakers")
        data = resp.json()
        breaker = [b for b in data["breakers"] if b["name"] == "openai"][0]
        assert breaker["state"] == "open"
        assert breaker["recovery_remaining_seconds"] is not None
        assert breaker["recovery_remaining_seconds"] > 0
