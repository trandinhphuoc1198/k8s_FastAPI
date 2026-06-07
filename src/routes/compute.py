from fastapi import APIRouter
import time
from src.logging import get_logger

router = APIRouter()
logger = get_logger(__name__)

# ================================
# Compute endpoint
# ================================
@router.get("/compute/{n}")
def compute(n: int):
    start = time.time()

    logger.info("compute started", extra={"input": n})

    try:
        result = count_primes(n)
    except Exception as e:
        logger.error(
            "compute failed",
            extra={
                "input": n,
                "error.message": str(e),
                "error.type": type(e).__name__,
            },
            exc_info=True,
        )
        raise

    duration = time.time() - start

    logger.info(
        "compute finished",
        extra={
            "input": n,
            "result": result,
            "duration_ms": round(duration * 1000, 2),
        },
    )

    return {
        "input": n,
        "result": result,
        "duration_ms": round(duration * 1000, 2),
    }


# ================================
# CPU-heavy function
# ================================
def count_primes(n: int) -> int:
    count = 0
    for num in range(2, n):
        is_prime = True
        for i in range(2, int(num ** 0.5) + 1):
            if num % i == 0:
                is_prime = False
                break
        if is_prime:
            count += 1
    return count



