"""Tests for unified provider health overview."""

from unittest.mock import patch

from app.core.llm.circuit_breaker import get_registry, reset_registry
from app.services.provider_health import _compute_overall, get_provider_health

# ---------------------------------------------------------------------------
# _compute_overall logic
# ---------------------------------------------------------------------------


class TestComputeOverall:
    def test_unconfigured(self):
        assert _compute_overall(False, None, None) == "unconfigured"

    def test_healthy_no_signals(self):
        assert _compute_overall(True, None, None) == "healthy"

    def test_healthy_with_closed_cb(self):
        cb = {"state": "closed", "recent_failures": 0}
        assert _compute_overall(True, cb, None) == "healthy"

    def test_down_when_cb_open(self):
        cb = {"state": "open", "recent_failures": 5}
        assert _compute_overall(True, cb, None) == "down"

    def test_degraded_when_cb_half_open(self):
        cb = {"state": "half_open", "recent_failures": 1}
        assert _compute_overall(True, cb, None) == "degraded"

    def test_degraded_when_probe_unreachable(self):
        probe = {"reachable": False, "error": "timeout"}
        assert _compute_overall(True, None, probe) == "degraded"

    def test_healthy_when_probe_reachable(self):
        probe = {"reachable": True, "latency_s": 0.1}
        assert _compute_overall(True, None, probe) == "healthy"

    def test_down_overrides_probe_reachable(self):
        cb = {"state": "open", "recent_failures": 5}
        probe = {"reachable": True, "latency_s": 0.1}
        assert _compute_overall(True, cb, probe) == "down"


# ---------------------------------------------------------------------------
# get_provider_health service
# ---------------------------------------------------------------------------


class TestGetProviderHealth:
    def setup_method(self):
        reset_registry()

    def teardown_method(self):
        reset_registry()

    def test_returns_all_known_providers(self):
        providers = get_provider_health()
        names = [p["name"] for p in providers]
        assert "gemini" in names
        assert "openai" in names
        assert "deepseek" in names
        assert "ollama" in names

    def test_configured_flag_reflects_settings(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.GEMINI_API_KEY", "test-key")
        monkeypatch.setattr("app.config.settings.OPENAI_API_KEY", "")
        providers = get_provider_health()
        by_name = {p["name"]: p for p in providers}
        assert by_name["gemini"]["configured"] is True
        assert by_name["openai"]["configured"] is False

    def test_cb_state_included_when_initialized(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.GEMINI_API_KEY", "key")
        reg = get_registry()
        reg.get("gemini").record_failure()
        providers = get_provider_health()
        gemini = next(p for p in providers if p["name"] == "gemini")
        assert gemini["circuit_breaker"] is not None
        assert gemini["circuit_breaker"]["recent_failures"] >= 1

    def test_cb_none_when_not_initialized(self):
        providers = get_provider_health()
        gemini = next(p for p in providers if p["name"] == "gemini")
        assert gemini["circuit_breaker"] is None

    def test_probe_results_included_when_run(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.GEMINI_API_KEY", "key")
        mock_result = {"gemini": {"reachable": True, "latency_s": 0.05}}
        with patch(
            "app.services.health_probe.probe_providers",
            return_value=mock_result,
        ):
            providers = get_provider_health(run_probes=True)
        gemini = next(p for p in providers if p["name"] == "gemini")
        assert gemini["probe"] is not None
        assert gemini["probe"]["reachable"] is True

    def test_probe_not_included_when_not_run(self):
        providers = get_provider_health(run_probes=False)
        for p in providers:
            assert p["probe"] is None

    def test_overall_status_computed(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.GEMINI_API_KEY", "")
        providers = get_provider_health()
        gemini = next(p for p in providers if p["name"] == "gemini")
        assert gemini["overall_status"] == "unconfigured"

    def test_display_name_present(self):
        providers = get_provider_health()
        gemini = next(p for p in providers if p["name"] == "gemini")
        assert gemini["display_name"] == "Google Gemini"


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------


class TestProvidersDashboard:
    def setup_method(self):
        reset_registry()

    def teardown_method(self):
        reset_registry()

    def test_page_loads(self, client):
        resp = client.get("/dashboard/providers")
        assert resp.status_code == 200
        assert "Provider Health" in resp.text

    def test_shows_provider_names(self, client):
        resp = client.get("/dashboard/providers")
        assert "Google Gemini" in resp.text
        assert "OpenAI" in resp.text
        assert "DeepSeek" in resp.text

    def test_shows_unconfigured_badge(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.GEMINI_API_KEY", "")
        monkeypatch.setattr("app.config.settings.OPENAI_API_KEY", "")
        monkeypatch.setattr("app.config.settings.DEEPSEEK_API_KEY", "")
        resp = client.get("/dashboard/providers")
        assert "unconfigured" in resp.text.lower()

    def test_probe_button_present(self, client):
        resp = client.get("/dashboard/providers")
        assert "Run Live Probes" in resp.text

    def test_probed_badge_shown(self, client):
        with patch(
            "app.services.health_probe.probe_providers",
            return_value={},
        ):
            resp = client.get("/dashboard/providers?probe=true")
        assert resp.status_code == 200
        assert "Live probes ran" in resp.text

    def test_nav_link_present(self, client):
        resp = client.get("/dashboard/providers")
        assert 'href="/dashboard/providers"' in resp.text

    def test_cb_state_shown_for_open_provider(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.GEMINI_API_KEY", "key")
        reg = get_registry()
        cb = reg.get("gemini")
        for _ in range(reg._default_config.failure_threshold):
            cb.record_failure()
        resp = client.get("/dashboard/providers")
        assert "open" in resp.text.lower()
        assert "Reset Circuit" in resp.text

    def test_reset_circuit_redirects_back(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.GEMINI_API_KEY", "key")
        reg = get_registry()
        cb = reg.get("gemini")
        for _ in range(reg._default_config.failure_threshold):
            cb.record_failure()
        resp = client.post(
            "/dashboard/circuit-breakers/gemini/reset",
            follow_redirects=False,
        )
        assert resp.status_code == 303


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


class TestProviderHealthAPI:
    def setup_method(self):
        reset_registry()

    def teardown_method(self):
        reset_registry()

    def test_api_returns_all_providers(self, client):
        resp = client.get("/v1/agent/providers/health")
        assert resp.status_code == 200
        data = resp.json()
        names = [p["name"] for p in data["providers"]]
        assert "gemini" in names
        assert "openai" in names

    def test_api_includes_overall_status(self, client):
        resp = client.get("/v1/agent/providers/health")
        data = resp.json()
        for p in data["providers"]:
            assert "overall_status" in p

    def test_api_probe_false_by_default(self, client):
        resp = client.get("/v1/agent/providers/health")
        data = resp.json()
        for p in data["providers"]:
            assert p["probe"] is None

    def test_api_probe_true_runs_probes(self, client):
        with patch(
            "app.services.health_probe.probe_providers",
            return_value={"gemini": {"reachable": True, "latency_s": 0.1}},
        ):
            resp = client.get("/v1/agent/providers/health?probe=true")
        data = resp.json()
        gemini = next(p for p in data["providers"] if p["name"] == "gemini")
        assert gemini["probe"] is not None

    def test_legacy_api_route(self, client):
        resp = client.get("/api/agent/providers/health")
        assert resp.status_code == 200
