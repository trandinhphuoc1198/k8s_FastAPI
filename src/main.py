import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from prometheus_fastapi_instrumentator import Instrumentator

from src.routes import compute, db
from src.logging import configure_root_logging, get_logger, request_id_var

# ── Logging setup ──────────────────────────────────────────────────────────────

configure_root_logging(level="INFO")
logger = get_logger(__name__)

# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(root_path="/fastapi-app", title="k8s FastAPI", version="1.0.0")
Instrumentator().instrument(app).expose(app)

# ── Middlewares ────────────────────────────────────────────────────────────────

@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    """Assign a request ID and emit structured access logs."""
    req_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    token = request_id_var.set(req_id)

    start = time.perf_counter()
    logger.info(
        "request started",
        extra={
            "http.method": request.method,
            "http.path": request.url.path,
            "http.query": str(request.query_params),
            "http.client": request.client.host if request.client else None,
        },
    )

    try:
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.info(
            "request finished",
            extra={
                "http.method": request.method,
                "http.path": request.url.path,
                "http.status_code": response.status_code,
                "http.duration_ms": duration_ms,
            },
        )
        response.headers["X-Request-ID"] = req_id
        return response
    except Exception:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.exception(
            "request failed with unhandled exception",
            extra={
                "http.method": request.method,
                "http.path": request.url.path,
                "http.duration_ms": duration_ms,
            },
        )
        raise
    finally:
        request_id_var.reset(token)


# ── Routes ─────────────────────────────────────────────────────────────────────

app.include_router(compute.router)
app.include_router(db.router)


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/fail")
def fail():
    try:
        raise ValueError("This is an intentional error for testing purposes.")
    except Exception as exc:
        logger.error("intentional failure triggered", extra={"error": str(exc)}, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))