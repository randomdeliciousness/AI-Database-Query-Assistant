# Base image pinned by digest (recorded by CI in ADQ-1b-R.1).
FROM python:3.13.5-slim@sha256:4c2cf9917bd1cbacc5e9b07320025bdb7cdf2df7b0ceaccb55e9dd7e30987419 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ANONYMIZED_TELEMETRY=False

WORKDIR /app

# Every package is pinned by version and sha256 in requirements.lock; wheels only.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes --only-binary :all: -r requirements.lock \
    && pip check

# App files only (no tests): listed explicitly so the runtime image stays lean.
COPY LICENSE THIRD_PARTY_LICENSES.md README.md ./
COPY audit_log.py config.py enhanced_assistant.py mcp_client_example.py \
     mcp_server.py spend_meter.py sql_guard.py streamlit_app.py chinook.db \
     clinic_schema.py clinic_clock.py clinic_snapshot.py clinic_generator.py ./
COPY .streamlit/secrets.example.toml .streamlit/

EXPOSE 8501 8000
# App process. This command is the image default and it does not write the clinic DB.
# One-shot generator (own process, WAL writer):
#   docker run --rm --entrypoint python <image> -m clinic_generator --seed 11 --db /data/clinic.db
# Live demo (named volume, generator has no network, app on 127.0.0.1:8501):
#   docker compose up
CMD ["sh", "-c", "streamlit run streamlit_app.py & uvicorn mcp_server:app --host 0.0.0.0 --port 8000"]

# Test image: the runtime image plus the CI test tools and tests/. CI runs
# pytest in it with --network none and no keys set.
FROM runtime AS test

COPY requirements-ci.lock .
RUN pip install --no-cache-dir --require-hashes --only-binary :all: \
        -r requirements.lock -r requirements-ci.lock \
    && pip check
# Repo-hygiene tests (tests/test_claims.py) read these files.
COPY .gitignore .dockerignore pyproject.toml ./
COPY tests/ tests/
