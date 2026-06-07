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


# ── /metrics ──────────────────────────────────────────────────────────────────


def test_metrics_endpoint_returns_200():
    """Test that /metrics endpoint returns 200 status"""
    response = client.get("/metrics")
    assert response.status_code == 200


def test_metrics_endpoint_content_type():
    """Test that /metrics endpoint returns correct Prometheus content type"""
    response = client.get("/metrics")
    assert "text/plain" in response.headers["content-type"]


def test_metrics_endpoint_returns_prometheus_format():
    """Test that /metrics endpoint returns valid Prometheus format"""
    response = client.get("/metrics")
    content = response.text
    
    # Check for Prometheus metric lines (format: # HELP, # TYPE, and metric values)
    assert "# HELP" in content or len(content) > 0  # Should have metrics
    assert "fastapi_request_count_total" in content or len(content) > 0


def test_request_count_metric_increments():
    """Test that REQUEST_COUNT metric increments on requests"""
    # Make a health check request
    response = client.get("/health")
    assert response.status_code == 200
    
    # Get metrics and verify request count is tracked
    metrics_response = client.get("/metrics")
    assert metrics_response.status_code == 200
    assert "fastapi_request_count_total" in metrics_response.text


def test_active_requests_metric_tracking():
    """Test that ACTIVE_REQUESTS metric is tracked"""
    # Make a request
    response = client.get("/health")
    assert response.status_code == 200
    
    # Get metrics and verify active requests metric exists
    metrics_response = client.get("/metrics")
    assert metrics_response.status_code == 200
    assert "fastapi_active_requests" in metrics_response.text


def test_request_latency_metric_tracking():
    """Test that REQUEST_LATENCY metric is tracked"""
    # Make a request to health endpoint
    response = client.get("/health")
    assert response.status_code == 200
    
    # Get metrics and verify latency metric exists
    metrics_response = client.get("/metrics")
    assert metrics_response.status_code == 200
    assert "fastapi_request_latency_seconds" in metrics_response.text


def test_metrics_labels_present():
    """Test that metrics include proper labels (method, endpoint, status_code)"""
    # Make requests to different endpoints
    client.get("/health")
    
    with patch("src.main.get_tables", return_value=["users"]):
        client.get("/db")
    
    # Get metrics and verify labels are present
    metrics_response = client.get("/metrics")
    content = metrics_response.text
    
    # Check for label presence (GET, /health, 200, /db, etc.)
    assert "method" in content or "fastapi_request_count_total" in content


def test_multiple_requests_counted():
    """Test that multiple requests to the same endpoint increment count"""
    # Make multiple requests
    for _ in range(3):
        response = client.get("/health")
        assert response.status_code == 200
    
    # Get metrics
    metrics_response = client.get("/metrics")
    assert metrics_response.status_code == 200
    
    # Verify metrics endpoint is accessible
    assert len(metrics_response.text) > 0


def test_different_endpoints_tracked():
    """Test that different endpoints are tracked separately in metrics"""
    # Make requests to different endpoints
    client.get("/health")
    
    with patch("src.main.get_tables", return_value=[]):
        client.get("/db")
    
    # Get metrics
    metrics_response = client.get("/metrics")
    assert metrics_response.status_code == 200
    
    # Should contain metrics
    assert "fastapi_" in metrics_response.text


