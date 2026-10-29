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

# Pinned ONNX embedding. One RUN: print the pin, download, check sha256 and
# size, extract, delete the tarball. A mismatch fails the build.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    ADQ_ONNX_MODEL_DIR=/opt/adq/models/all-MiniLM-L6-v2
RUN python - <<'PY'
import hashlib
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

url = "https://chroma-onnx-models.s3.amazonaws.com/all-MiniLM-L6-v2/onnx.tar.gz"
expected_sha256 = "913d7300ceae3b2dbc2c50d1de4baacab4be7b9380491c27fab7418616a16ec3"
expected_size = 83178821
print(f"source URL: {url}", flush=True)
print(f"expected sha256: {expected_sha256}", flush=True)
print(f"expected size: {expected_size}", flush=True)
dest = Path("/tmp/onnx.tar.gz")
urllib.request.urlretrieve(url, dest)
data = dest.read_bytes()
digest = hashlib.sha256(data).hexdigest()
if digest != expected_sha256:
    print(f"sha256 mismatch: {digest}", file=sys.stderr)
    sys.exit(1)
if len(data) != expected_size:
    print(f"size mismatch: {len(data)}", file=sys.stderr)
    sys.exit(1)
extract = Path("/tmp/onnx-extract")
extract.mkdir(parents=True)
with tarfile.open(dest, "r:gz") as archive:
    archive.extractall(extract, filter="data")
matches = list(extract.rglob("model.onnx"))
if len(matches) != 1:
    print(f"model.onnx count: {len(matches)}", file=sys.stderr)
    sys.exit(1)
target = Path("/opt/adq/models/all-MiniLM-L6-v2")
target.parent.mkdir(parents=True, exist_ok=True)
model_dir = matches[0].parent
if model_dir.resolve() == extract.resolve():
    extract.rename(target)
else:
    shutil.move(str(model_dir), str(target))
    shutil.rmtree(extract, ignore_errors=True)
dest.unlink()
if not (target / "model.onnx").is_file():
    print("model.onnx missing after extract", file=sys.stderr)
    sys.exit(1)
PY

# App files only (no tests): listed explicitly so the runtime image stays lean.
COPY LICENSE THIRD_PARTY_LICENSES.md README.md ./
COPY audit_log.py config.py enhanced_assistant.py mcp_client_example.py \
     mcp_server.py spend_meter.py sql_guard.py streamlit_app.py chinook.db \
     clinic_schema.py clinic_clock.py clinic_snapshot.py clinic_generator.py \
     retrieval.py ./
COPY .streamlit/secrets.example.toml .streamlit/
# Seed pairs and the block list. retrieval_params.json is written by the
# in-image tune onto a mounted eval directory, so it is not copied here.
COPY eval/seed_pairs.jsonl eval/blocked.json eval/

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
COPY eval/ eval/
