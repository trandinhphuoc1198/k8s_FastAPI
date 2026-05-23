from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)


# ── /health ──────────────────────────────────────────────────────────────────


def test_health_returns_200():
    response = client.get("/health")
    assert response.status_code == 200


def test_health_returns_ok_status():
    response = client.get("/health")
    assert response.json() == {"status": "ok"}


# ── /db ───────────────────────────────────────────────────────────────────────


def test_db_returns_200_with_tables():
    with patch("src.main.get_tables", return_value=["users", "products"]):
        response = client.get("/db")
    assert response.status_code == 200
    assert response.json() == {"database": "TestDb", "tables": ["users", "products"]}


def test_db_returns_200_with_empty_tables():
    with patch("src.main.get_tables", return_value=[]):
        response = client.get("/db")
    assert response.status_code == 200
    assert response.json() == {"database": "TestDb", "tables": []}


def test_db_returns_500_on_connection_error():
    with patch("src.main.get_tables", side_effect=Exception("could not connect to server")):
        response = client.get("/db")
    assert response.status_code == 500
    assert "could not connect to server" in response.json()["detail"]
