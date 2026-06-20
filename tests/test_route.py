from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient

from src.database import DB_NAME
from src.main import app

client = TestClient(app)


# ── /health ───────────────────────────────────────────────────────────────────


def test_health_returns_200():
    response = client.get("/health")
    assert response.status_code == 200


def test_health_returns_ok_status():
    response = client.get("/health")
    assert response.json() == {"status": "ok"}


# ── GET /db/tables ────────────────────────────────────────────────────────────


def test_db_tables_returns_200_with_tables():
    with patch("src.routes.db.get_tables", return_value=["users", "products"]):
        response = client.get("/db/tables")
    assert response.status_code == 200
    assert response.json() == {"database": DB_NAME, "tables": ["users", "products"]}


def test_db_tables_returns_200_with_empty_tables():
    with patch("src.routes.db.get_tables", return_value=[]):
        response = client.get("/db/tables")
    assert response.status_code == 200
    assert response.json() == {"database": DB_NAME, "tables": []}


def test_db_tables_returns_500_on_connection_error():
    with patch("src.routes.db.get_tables", side_effect=Exception("could not connect to server")):
        response = client.get("/db/tables")
    assert response.status_code == 500
    assert "could not connect to server" in response.json()["detail"]


# ── POST /db/test/setup ───────────────────────────────────────────────────────


def test_setup_test_table_returns_201():
    with patch("src.routes.db.create_test_table") as mock_setup:
        response = client.get("/db/test/setup")
    assert response.status_code == 201
    mock_setup.assert_called_once()


def test_setup_test_table_returns_expected_body():
    with patch("src.routes.db.create_test_table"):
        response = client.get("/db/test/setup")
    assert response.json() == {"status": "ok", "message": "Test table is ready."}


def test_setup_test_table_returns_500_on_error():
    with patch(
        "src.routes.db.create_test_table",
        side_effect=Exception("permission denied"),
    ):
        response = client.get("/db/test/setup")
    assert response.status_code == 500
    assert "permission denied" in response.json()["detail"]


# ── POST /db/test/seed ────────────────────────────────────────────────────────

_SAMPLE_ROWS = [
    {"id": "abc-1", "name": "foo", "value": 42, "created_at": "2024-01-01T00:00:00+00:00"},
    {"id": "abc-2", "name": "bar", "value": 99, "created_at": "2024-01-01T00:00:01+00:00"},
]


def test_seed_test_data_returns_201():
    with patch("src.routes.db.insert_test_data", return_value=_SAMPLE_ROWS):
        response = client.get("/db/test/seed")
    assert response.status_code == 201


def test_seed_test_data_default_n():
    with patch("src.routes.db.insert_test_data", return_value=_SAMPLE_ROWS) as mock_insert:
        client.get("/db/test/seed")
    mock_insert.assert_called_once_with(n=10)


def test_seed_test_data_custom_n():
    with patch("src.routes.db.insert_test_data", return_value=_SAMPLE_ROWS) as mock_insert:
        client.get("/db/test/seed?n=25")
    mock_insert.assert_called_once_with(n=25)


def test_seed_test_data_returns_expected_body():
    with patch("src.routes.db.insert_test_data", return_value=_SAMPLE_ROWS):
        response = client.get("/db/test/seed")
    body = response.json()
    assert body["status"] == "ok"
    assert body["inserted"] == len(_SAMPLE_ROWS)
    assert body["rows"] == _SAMPLE_ROWS


def test_seed_test_data_rejects_n_below_minimum():
    response = client.get("/db/test/seed?n=0")
    assert response.status_code == 422


def test_seed_test_data_rejects_n_above_maximum():
    response = client.get("/db/test/seed?n=1001")
    assert response.status_code == 422


def test_seed_test_data_returns_500_on_error():
    with patch(
        "src.routes.db.insert_test_data",
        side_effect=Exception("relation does not exist"),
    ):
        response = client.get("/db/test/seed")
    assert response.status_code == 500
    assert "relation does not exist" in response.json()["detail"]


# ── GET /db/test/records ──────────────────────────────────────────────────────


def test_get_test_records_returns_200():
    with patch("src.routes.db.get_test_records", return_value=_SAMPLE_ROWS):
        response = client.get("/db/test/records")
    assert response.status_code == 200


def test_get_test_records_returns_expected_body():
    with patch("src.routes.db.get_test_records", return_value=_SAMPLE_ROWS):
        response = client.get("/db/test/records")
    body = response.json()
    assert body["count"] == len(_SAMPLE_ROWS)
    assert body["records"] == _SAMPLE_ROWS


def test_get_test_records_returns_empty_list():
    with patch("src.routes.db.get_test_records", return_value=[]):
        response = client.get("/db/test/records")
    body = response.json()
    assert body["count"] == 0
    assert body["records"] == []


def test_get_test_records_returns_500_on_error():
    with patch(
        "src.routes.db.get_test_records",
        side_effect=Exception("connection timeout"),
    ):
        response = client.get("/db/test/records")
    assert response.status_code == 500
    assert "connection timeout" in response.json()["detail"]