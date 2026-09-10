"""Tests for the FastAPI health endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from etl_agent import main


def test_health_returns_ok_when_dependencies_are_healthy(monkeypatch) -> None:
    """Health returns 200 only when all external dependencies are reachable."""

    def healthy() -> bool:
        return True

    monkeypatch.setattr(main, "_check_database", healthy)
    monkeypatch.setattr(main, "_check_redis", healthy)
    monkeypatch.setattr(main, "_check_seatunnel", healthy)

    response = TestClient(main.app).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["checks"] == {
        "database": "ok",
        "redis": "ok",
        "seatunnel": "ok",
    }
