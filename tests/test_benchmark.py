"""Tests for the security benchmark system."""

import json
import subprocess
import sys

import pytest
from app.core.immune.benchmark import (
    ATTACK_REGISTRY,
    AttackCategory,
    run_benchmark,
)


class TestBenchmarkRunner:
    def test_full_benchmark_passes(self):
        report = run_benchmark()
        assert report.total_payloads == len(ATTACK_REGISTRY)
        assert report.total_failed == 0
        assert report.composite_score == 1.0
        assert report.duration_ms > 0
        assert len(report.categories) > 0
        assert report.failures == []

    def test_all_categories_covered(self):
        report = run_benchmark()
        reported_cats = {c.category for c in report.categories}
        expected_cats = {c.value for c in AttackCategory}
        assert reported_cats == expected_cats

    def test_category_filter(self):
        report = run_benchmark(categories=["encoding_evasion"])
        assert len(report.categories) == 1
        assert report.categories[0].category == "encoding_evasion"
        assert report.total_payloads > 0
        assert report.total_payloads < len(ATTACK_REGISTRY)

    def test_multiple_category_filter(self):
        report = run_benchmark(categories=["structural_injection", "output_leak"])
        cat_names = {c.category for c in report.categories}
        assert cat_names == {"structural_injection", "output_leak"}

    def test_empty_category_filter(self):
        report = run_benchmark(categories=["nonexistent_category"])
        assert report.total_payloads == 0
        assert report.composite_score == 0.0

    def test_each_category_perfect_score(self):
        report = run_benchmark()
        for cat in report.categories:
            assert cat.detection_rate == 1.0, (
                f"Category {cat.category} has {cat.failed} failure(s): {cat.payloads_failed}"
            )

    def test_report_serialization(self):
        report = run_benchmark()
        d = report.to_dict()
        assert isinstance(d, dict)
        assert "timestamp" in d
        assert "composite_score" in d
        assert "categories" in d
        assert isinstance(d["categories"], list)
        json_str = json.dumps(d)
        parsed = json.loads(json_str)
        assert parsed["composite_score"] == 1.0

    def test_report_timestamp_format(self):
        report = run_benchmark()
        assert "T" in report.timestamp
        assert report.timestamp.endswith("+00:00")

    def test_idempotent_runs(self):
        r1 = run_benchmark()
        r2 = run_benchmark()
        assert r1.total_passed == r2.total_passed
        assert r1.total_failed == r2.total_failed
        assert r1.composite_score == r2.composite_score


class TestBenchmarkAPI:
    @pytest.fixture
    def client(self):
        from app.main import app
        from starlette.testclient import TestClient

        with TestClient(app) as c:
            yield c

    def test_benchmark_endpoint(self, client):
        resp = client.post("/api/agent/benchmark", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["composite_score"] == 1.0
        assert data["total_failed"] == 0
        assert "categories" in data

    def test_benchmark_with_category_filter(self, client):
        resp = client.post(
            "/api/agent/benchmark",
            json={"categories": ["encoding_evasion"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["categories"]) == 1
        assert data["categories"][0]["category"] == "encoding_evasion"

    def test_benchmark_gate_passed(self, client):
        resp = client.post(
            "/api/agent/benchmark",
            json={"threshold": 0.9},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["gate"] == "passed"

    def test_benchmark_gate_impossible_threshold(self, client):
        resp = client.post(
            "/api/agent/benchmark",
            json={"threshold": 1.1},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["gate"] == "failed"


class TestBenchmarkCLI:
    def test_cli_json_output(self):
        result = subprocess.run(
            [sys.executable, "-m", "app.cli", "benchmark", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["composite_score"] == 1.0
        assert data["total_failed"] == 0

    def test_cli_table_output(self):
        result = subprocess.run(
            [sys.executable, "-m", "app.cli", "benchmark"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "Security Benchmark" in result.stdout
        assert "ALL CLEAR" in result.stdout

    def test_cli_category_filter(self):
        result = subprocess.run(
            [sys.executable, "-m", "app.cli", "benchmark", "--json", "--categories", "false_positive"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert len(data["categories"]) == 1

    def test_cli_threshold_pass(self):
        result = subprocess.run(
            [sys.executable, "-m", "app.cli", "benchmark", "--threshold", "0.9"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "GATE PASSED" in result.stdout

    def test_cli_invalid_category(self):
        result = subprocess.run(
            [sys.executable, "-m", "app.cli", "benchmark", "--categories", "not_a_real_category"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 1
        assert "Unknown categories" in result.stderr


class TestAttackRegistryIntegrity:
    def test_all_payloads_have_unique_labels(self):
        labels = [p.label for p in ATTACK_REGISTRY]
        assert len(labels) == len(set(labels))

    def test_all_payloads_have_valid_categories(self):
        valid = {c.value for c in AttackCategory}
        for p in ATTACK_REGISTRY:
            assert p.category in valid, f"Invalid category on {p.label}"

    def test_minimum_payload_count(self):
        assert len(ATTACK_REGISTRY) >= 60

    def test_each_category_has_payloads(self):
        cats_with_payloads = {p.category for p in ATTACK_REGISTRY}
        for cat in AttackCategory:
            assert cat in cats_with_payloads, f"No payloads for {cat}"
