FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# node + npm are only needed by the scanner (npm audit --package-lock-only)
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY sentinel ./sentinel
COPY fake_devin ./fake_devin
RUN pip install .
RUN mkdir -p /data
EXPOSE 8080
CMD ["uvicorn", "sentinel.main:build_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
