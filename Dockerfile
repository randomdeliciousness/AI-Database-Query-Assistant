# Base image pinned by tag only for now. The digest is pinned in the next
# stage (python:3.13.5-slim@sha256:...), after the first CI docker-build job
# records it with `docker image inspect`.
FROM python:3.13.5-slim AS runtime

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
     mcp_server.py sql_guard.py streamlit_app.py chinook.db ./
COPY .streamlit/secrets.example.toml .streamlit/

EXPOSE 8501 8000
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
