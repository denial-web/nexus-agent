"""Tests for Prometheus HTTP middleware metrics and DB pool gauges."""

from unittest.mock import MagicMock, patch

import pytest
from app.metrics import (
    DB_POOL_CHECKED_IN,
    DB_POOL_CHECKED_OUT,
    DB_POOL_OVERFLOW,
    DB_POOL_SIZE,
    HTTP_IN_FLIGHT,
    HTTP_REQUEST_LATENCY,
    HTTP_REQUESTS_TOTAL,
    is_available,
    normalize_path,
    update_db_pool_gauges,
)


class TestNormalizePath:
    def test_v1_traces_with_id(self):
        assert normalize_path("/v1/traces/abc123") == "/v1/traces/{id}"

    def test_api_traces_with_id(self):
        assert normalize_path("/api/traces/abc123") == "/api/traces/{id}"

    def test_v1_top_level_no_collapse(self):
        assert normalize_path("/v1/traces") == "/v1/traces"

    def test_deep_nested_path(self):
        assert normalize_path("/v1/critic/registry/node1/detail") == "/v1/critic/{id}/{id}/{id}"

    def test_non_api_path_unchanged(self):
        assert normalize_path("/health") == "/health"

    def test_dashboard_path_unchanged(self):
        assert normalize_path("/dashboard/traces") == "/dashboard/traces"

    def test_root_path(self):
        assert normalize_path("/") == "/"

    def test_empty_string(self):
        assert normalize_path("") == ""

    def test_v1_skills_with_id(self):
        assert normalize_path("/v1/skills/some-skill-id") == "/v1/skills/{id}"

    def test_api_governance_nested(self):
        assert normalize_path("/api/governance/policies/p1") == "/api/governance/{id}/{id}"


class TestUpdateDbPoolGauges:
    @pytest.mark.skipif(not is_available(), reason="prometheus_client not installed")
    def test_updates_gauges_for_pooled_engine(self):
        mock_pool = MagicMock()
        mock_pool.size.return_value = 5
        mock_pool.checkedin.return_value = 3
        mock_pool.checkedout.return_value = 2
        mock_pool.overflow.return_value = 0

        mock_engine = MagicMock()
        mock_engine.pool = mock_pool

        with (
            patch("app.metrics.DB_POOL_SIZE") as m_size,
            patch("app.metrics.DB_POOL_CHECKED_IN") as m_in,
            patch("app.metrics.DB_POOL_CHECKED_OUT") as m_out,
            patch("app.metrics.DB_POOL_OVERFLOW") as m_ov,
            patch("app.db.engine", mock_engine),
        ):
            import app.metrics

            orig = app.metrics._HAS_PROMETHEUS
            app.metrics._HAS_PROMETHEUS = True
            try:
                app.metrics.update_db_pool_gauges()
            finally:
                app.metrics._HAS_PROMETHEUS = orig

            m_size.set.assert_called_once_with(5)
            m_in.set.assert_called_once_with(3)
            m_out.set.assert_called_once_with(2)
            m_ov.set.assert_called_once_with(0)

    def test_noop_when_prometheus_absent(self):
        with patch("app.metrics._HAS_PROMETHEUS", False):
            update_db_pool_gauges()

    def test_silent_on_exception(self):
        import app.metrics

        orig = app.metrics._HAS_PROMETHEUS
        app.metrics._HAS_PROMETHEUS = True
        try:
            mock_engine = MagicMock()
            mock_engine.pool.size.side_effect = Exception("no pool")
            with patch("app.db.engine", mock_engine):
                update_db_pool_gauges()
        finally:
            app.metrics._HAS_PROMETHEUS = orig


class TestHttpMetricsExist:
    @pytest.mark.skipif(not is_available(), reason="prometheus_client not installed")
    def test_http_latency_is_histogram(self):
        from prometheus_client import Histogram

        assert isinstance(HTTP_REQUEST_LATENCY, Histogram)

    @pytest.mark.skipif(not is_available(), reason="prometheus_client not installed")
    def test_http_requests_total_is_counter(self):
        from prometheus_client import Counter

        assert isinstance(HTTP_REQUESTS_TOTAL, Counter)

    @pytest.mark.skipif(not is_available(), reason="prometheus_client not installed")
    def test_http_in_flight_is_gauge(self):
        from prometheus_client import Gauge

        assert isinstance(HTTP_IN_FLIGHT, Gauge)

    @pytest.mark.skipif(not is_available(), reason="prometheus_client not installed")
    def test_db_pool_gauges_are_gauge(self):
        from prometheus_client import Gauge

        assert isinstance(DB_POOL_SIZE, Gauge)
        assert isinstance(DB_POOL_CHECKED_IN, Gauge)
        assert isinstance(DB_POOL_CHECKED_OUT, Gauge)
        assert isinstance(DB_POOL_OVERFLOW, Gauge)


class TestMetricsMiddlewareIntegration:
    def test_metrics_endpoint_includes_http_metrics(self, client):
        client.get("/health")
        resp = client.get("/metrics")
        assert resp.status_code == 200
        body = resp.text
        assert "nexus_http_request_duration_seconds" in body
        assert "nexus_http_requests_total" in body

    def test_latency_recorded_for_health(self, client):
        client.get("/health")
        resp = client.get("/metrics")
        body = resp.text
        assert 'path_template="/health"' in body

    def test_status_class_label(self, client):
        client.get("/nonexistent-path-that-404s")
        resp = client.get("/metrics")
        body = resp.text
        assert 'status_class="4xx"' in body

    def test_method_label(self, client):
        client.get("/health")
        resp = client.get("/metrics")
        body = resp.text
        assert 'method="GET"' in body

    def test_in_flight_gauge_present(self, client):
        resp = client.get("/metrics")
        body = resp.text
        assert "nexus_http_in_flight_requests" in body

    def test_db_pool_gauges_present(self, client):
        client.get("/health")
        resp = client.get("/metrics")
        body = resp.text
        assert "nexus_db_pool_size" in body or "nexus_db_pool_checked_in" in body

    def test_path_id_collapsed_in_metrics(self, client):
        client.get("/v1/traces/some-fake-id-12345")
        resp = client.get("/metrics")
        body = resp.text
        assert "some-fake-id-12345" not in body
        assert 'path_template="/v1/traces/{id}"' in body

    def test_post_request_counted(self, client):
        client.post("/v1/agent/run", json={"prompt": "hello"})
        resp = client.get("/metrics")
        body = resp.text
        assert 'method="POST"' in body

    def test_2xx_counted(self, client):
        client.get("/health")
        resp = client.get("/metrics")
        body = resp.text
        assert 'status_class="2xx"' in body

    def test_multiple_requests_accumulate(self, client):
        for _ in range(3):
            client.get("/health")
        resp = client.get("/metrics")
        body = resp.text
        assert "nexus_http_requests_total" in body
