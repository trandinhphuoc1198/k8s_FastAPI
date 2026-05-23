from fastapi import FastAPI, HTTPException

from src.database import get_tables

app = FastAPI(title="k8s FastAPI", version="1.0.0")


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/db")
def list_tables():
    try:
        tables = get_tables()
        return {"database": "TestDb", "tables": tables}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
