FROM python:3.12-slim

ENV TMPDIR=/tmp

WORKDIR /app

RUN mkdir -p /tmp /var/tmp /usr/tmp && chmod 1777 /tmp /var/tmp /usr/tmp

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

EXPOSE 8000

# Run Gunicorn with Uvicorn workers
CMD ["gunicorn", "src.main:app", "--workers", "3", "--worker-class", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000"]