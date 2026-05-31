from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
import time

from src.database import get_tables
from src.metrics import REQUEST_COUNT, REQUEST_LATENCY, ACTIVE_REQUESTS

app = FastAPI(root_path="/fastapi-app",title="k8s FastAPI", version="1.0.0")


@app.middleware("http")
async def prometheus_middleware(request, call_next):
    """Middleware to track request metrics"""
    method = request.method
    path = request.url.path
    
    ACTIVE_REQUESTS.inc()
    start_time = time.time()
    
    try:
        response = await call_next(request)
        REQUEST_COUNT.labels(
            method=method,
            endpoint=path,
            status_code=response.status_code
        ).inc()
        return response
    finally:
        duration = time.time() - start_time
        REQUEST_LATENCY.labels(method=method, endpoint=path).observe(duration)
        ACTIVE_REQUESTS.dec()


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    """Prometheus metrics endpoint"""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/db")
def list_tables():
    try:
        tables = get_tables()
        return {"database": "TestDb", "tables": tables}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
