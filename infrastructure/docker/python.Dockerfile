# One image for the API, worker, migration job and fictional-bank simulators.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/apps/api:/app

# WeasyPrint (PDF reports) needs Pango; DejaVu fonts give consistent report typography.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini pyproject.toml ./
COPY apps/api apps/api
COPY workers workers
COPY decision_simulator decision_simulator
COPY infrastructure/migrations infrastructure/migrations
COPY test_suites test_suites
COPY scripts scripts

RUN useradd --system --uid 10001 --home /app modelassure \
 && mkdir -p /data/objects /data/secrets && chown -R modelassure /data
USER modelassure

EXPOSE 8000
CMD ["uvicorn", "main:app", "--app-dir", "apps/api", "--host", "0.0.0.0", "--port", "8000"]
