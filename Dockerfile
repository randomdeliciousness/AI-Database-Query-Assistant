# Base image pinned by digest (recorded by CI in ADQ-1b-R.1).
FROM python:3.13.5-slim@sha256:4c2cf9917bd1cbacc5e9b07320025bdb7cdf2df7b0ceaccb55e9dd7e30987419 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ANONYMIZED_TELEMETRY=False \
    PYTHONPATH=/app

WORKDIR /app

# Every package is pinned by version and sha256 in requirements.lock; wheels only.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes --only-binary :all: -r requirements.lock \
    && pip check

# Pinned ONNX embedding. One RUN: print the pin, download, check the archive
# sha256 and size BEFORE extracting, extract, delete the tarball, print one
# line per file (archive path, sha256, bytes).
# Normal build: every file must match model_pins/ by path, sha256 and bytes
# (a placeholder fails) and the manifest is never rewritten.
# ADQ_MODEL_BOOTSTRAP=1 prints the tarball line and the file lines, then exits
# 1, so a bootstrap image never completes and can never run the tune.
# The archive ships no LICENSE, so the pinned Apache-2.0 text is copied in.
ARG ADQ_MODEL_BOOTSTRAP=0
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    ADQ_ONNX_MODEL_DIR=/opt/adq/models/all-MiniLM-L6-v2
COPY model_pins/all-MiniLM-L6-v2.sha256.json /tmp/adq-pins/manifest.json
COPY licenses/all-MiniLM-L6-v2.LICENSE /tmp/adq-pins/LICENSE
RUN ADQ_MODEL_BOOTSTRAP="${ADQ_MODEL_BOOTSTRAP}" python - <<'PY'
import json
import os
import hashlib
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

url = "https://chroma-onnx-models.s3.amazonaws.com/all-MiniLM-L6-v2/onnx.tar.gz"
expected_sha256 = "913d7300ceae3b2dbc2c50d1de4baacab4be7b9380491c27fab7418616a16ec3"
expected_size = 83178821
bootstrap = os.environ.get("ADQ_MODEL_BOOTSTRAP") == "1"
print(f"source URL: {url}", flush=True)
print(f"expected sha256: {expected_sha256}", flush=True)
print(f"expected size: {expected_size}", flush=True)
dest = Path("/tmp/onnx.tar.gz")
urllib.request.urlretrieve(url, dest)
data = dest.read_bytes()
digest = hashlib.sha256(data).hexdigest()
# Verified before anything is extracted.
if digest != expected_sha256:
    print(f"sha256 mismatch: {digest}", file=sys.stderr)
    sys.exit(1)
if len(data) != expected_size:
    print(f"size mismatch: {len(data)}", file=sys.stderr)
    sys.exit(1)
print(f"tarball: onnx.tar.gz sha256={digest} bytes={len(data)}", flush=True)
del data
extract = Path("/tmp/onnx-extract")
extract.mkdir(parents=True)
with tarfile.open(dest, "r:gz") as archive:
    archive.extractall(extract, filter="data")
dest.unlink()
matches = list(extract.rglob("model.onnx"))
if len(matches) != 1:
    print(f"model.onnx count: {len(matches)}", file=sys.stderr)
    sys.exit(1)
model_dir = matches[0].parent
files = (
    "config.json",
    "model.onnx",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
)
actual = {}
for name in files:
    path = model_dir / name
    if not path.is_file():
        print(f"{name} missing after extract", file=sys.stderr)
        sys.exit(1)
    rel = path.relative_to(extract).as_posix()
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    size = path.stat().st_size
    actual[name] = (rel, sha, size)
    print(f"file: {rel} sha256={sha} bytes={size}", flush=True)
if bootstrap:
    print("bootstrap build: hashes printed; exiting 1 by design", flush=True)
    sys.exit(1)
manifest = json.loads(Path("/tmp/adq-pins/manifest.json").read_text())
if manifest.get("status") != "pinned":
    print("model manifest is a placeholder", file=sys.stderr)
    sys.exit(1)
for name in files:
    rel, sha, size = actual[name]
    entry = manifest["files"].get(name) or {}
    if entry.get("path") != rel:
        print(f"manifest path mismatch: {name}", file=sys.stderr)
        sys.exit(1)
    if entry.get("sha256") != sha:
        print(f"manifest sha256 mismatch: {name}", file=sys.stderr)
        sys.exit(1)
    if entry.get("bytes") != size:
        print(f"manifest bytes mismatch: {name}", file=sys.stderr)
        sys.exit(1)
print("manifest check: all files match", flush=True)
target = Path("/opt/adq/models/all-MiniLM-L6-v2")
target.parent.mkdir(parents=True, exist_ok=True)
if model_dir.resolve() == extract.resolve():
    extract.rename(target)
else:
    shutil.move(str(model_dir), str(target))
    shutil.rmtree(extract, ignore_errors=True)
license_sha256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
license_src = Path("/tmp/adq-pins/LICENSE")
if hashlib.sha256(license_src.read_bytes()).hexdigest() != license_sha256:
    print("LICENSE sha256 mismatch", file=sys.stderr)
    sys.exit(1)
shutil.copyfile(license_src, target / "LICENSE")
print(f"LICENSE sha256: {license_sha256}", flush=True)
shutil.rmtree("/tmp/adq-pins")
PY

# App files only (no tests): listed explicitly so the runtime image stays lean.
COPY LICENSE THIRD_PARTY_LICENSES.md README.md ./
COPY audit_log.py config.py enhanced_assistant.py mcp_client_example.py \
     mcp_server.py spend_meter.py sql_guard.py streamlit_app.py chinook.db \
     clinic_schema.py clinic_clock.py clinic_snapshot.py clinic_generator.py \
     retrieval.py ./
COPY model_pins/all-MiniLM-L6-v2.sha256.json model_pins/
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
