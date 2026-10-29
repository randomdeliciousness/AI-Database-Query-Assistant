# Third-party licenses

AI Database Query Assistant itself is MIT-licensed (see `LICENSE`). This file lists the third-party software the Docker image and CI install, with the license each one states. Recorded 2026-10-27 from the installed package metadata of the hash-locked versions (and the license file shipped in each wheel where the metadata field is not an SPDX expression). All packages are used unmodified.

MPL-2.0 packages (certifi, orjson, tqdm, pathspec) are used unmodified, as published on PyPI; their source is available at the URLs below.

## Base image

| name | version | SPDX | URL | date |
|---|---|---|---|---|
| `python` Docker Official Image `3.13.5-slim`, pinned at digest `sha256:4c2cf9917bd1cbacc5e9b07320025bdb7cdf2df7b0ceaccb55e9dd7e30987419` | CPython 3.13.5 | PSF-2.0 (CPython); Debian 12 (bookworm) base packages: see the Debian packages read by hand table below | https://hub.docker.com/_/python | 2026-10-27 |

- Digest: `sha256:4c2cf9917bd1cbacc5e9b07320025bdb7cdf2df7b0ceaccb55e9dd7e30987419`.
- `/etc/os-release`: Debian GNU/Linux 12 (bookworm).
- Debian package license inventory (`/usr/share/doc/*/copyright`): 97 copyright files; 85 carry machine-readable `License:` lines; no AGPL (the one hit, ca-certificates, matches text inside the MPL-2.0 license, not an AGPL grant); the other 12 were read by hand and are listed below.
- pip and setuptools bundled in the image: covered by this row; versions recorded after first CI build.

### Debian packages read by hand

These 12 copyright files in the image have no machine-readable `License:` line, so they were read by hand on 2026-10-27 from the image at the digest above. Versions are those shipped in that image.

| package | license (from `/usr/share/doc/<package>/copyright`) | date |
|---|---|---|
| base-files | GPL-2+ | 2026-10-27 |
| debian-archive-keyring | Keyrings not copyrightable; remaining files GPL-2+ | 2026-10-27 |
| gcc-12-base | GPL-3+ with the GCC Runtime Library Exception | 2026-10-27 |
| libc-bin | LGPL-2.1+ (library), GPL-2+ (utilities) | 2026-10-27 |
| libc6 | LGPL-2.1+ (library), GPL-2+ (utilities) | 2026-10-27 |
| libcrypt1 | LGPL-2.1+, with some BSD-3-Clause, 0BSD and public-domain files | 2026-10-27 |
| libgcc-s1 | Same as gcc-12-base (copyright file is a symlink to it) | 2026-10-27 |
| libselinux1 | Public domain, with GPL-2 and LGPL-2.1 parts | 2026-10-27 |
| libsemanage-common | LGPL-2.1+, Debian packaging GPL-2 | 2026-10-27 |
| libsemanage2 | LGPL-2.1+, Debian packaging GPL-2 | 2026-10-27 |
| libstdc++6 | Same as gcc-12-base (copyright file is a symlink to it) | 2026-10-27 |
| libtasn1-6 | LGPL-2.1+ (library), GPL-3+ (tools) | 2026-10-27 |

## Runtime packages (`requirements.lock`)

Installed in the runtime image and in CI.

| name | version | SPDX | URL | date |
|---|---|---|---|---|
| aiohappyeyeballs | 2.7.1 | PSF-2.0 | https://pypi.org/project/aiohappyeyeballs/2.7.1/ | 2026-10-27 |
| aiohttp | 3.14.5 | Apache-2.0 AND MIT | https://pypi.org/project/aiohttp/3.14.5/ | 2026-10-27 |
| aiohttp-retry | 2.9.1 | MIT | https://pypi.org/project/aiohttp-retry/2.9.1/ | 2026-10-27 |
| aiosignal | 1.4.0 | Apache-2.0 | https://pypi.org/project/aiosignal/1.4.0/ | 2026-10-27 |
| altair | 6.3.0 | BSD-3-Clause | https://pypi.org/project/altair/6.3.0/ | 2026-10-27 |
| annotated-doc | 0.0.5 | MIT | https://pypi.org/project/annotated-doc/0.0.5/ | 2026-10-27 |
| annotated-types | 0.8.0 | MIT | https://pypi.org/project/annotated-types/0.8.0/ | 2026-10-27 |
| anyio | 4.15.1 | MIT | https://pypi.org/project/anyio/4.15.1/ | 2026-10-27 |
| attrs | 26.1.0 | MIT | https://pypi.org/project/attrs/26.1.0/ | 2026-10-27 |
| bcrypt | 5.0.0 | Apache-2.0 | https://pypi.org/project/bcrypt/5.0.0/ | 2026-10-27 |
| build | 1.6.1 | MIT | https://pypi.org/project/build/1.6.1/ | 2026-10-27 |
| certifi | 2026.10.23 | MPL-2.0 (used unmodified) | https://pypi.org/project/certifi/2026.10.23/ | 2026-10-27 |
| charset-normalizer | 3.5.2 | MIT | https://pypi.org/project/charset-normalizer/3.5.2/ | 2026-10-27 |
| chromadb | 1.5.9 | Apache-2.0 | https://pypi.org/project/chromadb/1.5.9/ | 2026-10-27 |
| click | 8.5.0 | BSD-3-Clause | https://pypi.org/project/click/8.5.0/ | 2026-10-27 |
| durationpy | 0.11 | MIT | https://pypi.org/project/durationpy/0.11/ | 2026-10-27 |
| fastapi | 0.162.0 | MIT | https://pypi.org/project/fastapi/0.162.0/ | 2026-10-27 |
| faker | 40.44.0 | MIT | https://pypi.org/project/Faker/40.44.0/ | 2026-10-29 |
| filelock | 4.2.4 | MIT | https://pypi.org/project/filelock/4.2.4/ | 2026-10-27 |
| flatbuffers | 25.12.19 | Apache-2.0 | https://pypi.org/project/flatbuffers/25.12.19/ | 2026-10-27 |
| frozenlist | 1.8.0 | Apache-2.0 | https://pypi.org/project/frozenlist/1.8.0/ | 2026-10-27 |
| fsspec | 2026.9.0 | BSD-3-Clause | https://pypi.org/project/fsspec/2026.9.0/ | 2026-10-27 |
| googleapis-common-protos | 1.76.0 | Apache-2.0 | https://pypi.org/project/googleapis-common-protos/1.76.0/ | 2026-10-27 |
| grpcio | 1.85.0 | Apache-2.0 | https://pypi.org/project/grpcio/1.85.0/ | 2026-10-27 |
| h11 | 0.16.0 | MIT | https://pypi.org/project/h11/0.16.0/ | 2026-10-27 |
| hf-xet | 1.7.0 | Apache-2.0 | https://pypi.org/project/hf-xet/1.7.0/ | 2026-10-27 |
| httpcore | 1.0.9 | BSD-3-Clause | https://pypi.org/project/httpcore/1.0.9/ | 2026-10-27 |
| httpcore2 | 2.16.0 | BSD-3-Clause | https://pypi.org/project/httpcore2/2.16.0/ | 2026-10-27 |
| httptools | 0.9.0 | MIT | https://pypi.org/project/httptools/0.9.0/ | 2026-10-27 |
| httpx | 0.28.1 | BSD-3-Clause | https://pypi.org/project/httpx/0.28.1/ | 2026-10-27 |
| httpx2 | 2.16.0 | BSD-3-Clause | https://pypi.org/project/httpx2/2.16.0/ | 2026-10-27 |
| huggingface-hub | 2.3.0 | Apache-2.0 | https://pypi.org/project/huggingface-hub/2.3.0/ | 2026-10-27 |
| idna | 3.20 | BSD-3-Clause | https://pypi.org/project/idna/3.20/ | 2026-10-27 |
| importlib-resources | 7.1.0 | Apache-2.0 | https://pypi.org/project/importlib-resources/7.1.0/ | 2026-10-27 |
| iniconfig | 2.3.1 | MIT | https://pypi.org/project/iniconfig/2.3.1/ | 2026-10-27 |
| itsdangerous | 2.2.0 | BSD-3-Clause | https://pypi.org/project/itsdangerous/2.2.0/ | 2026-10-27 |
| jinja2 | 3.1.6 | BSD-3-Clause | https://pypi.org/project/jinja2/3.1.6/ | 2026-10-27 |
| jiter | 0.18.0 | MIT | https://pypi.org/project/jiter/0.18.0/ | 2026-10-27 |
| jsonschema | 4.26.0 | MIT | https://pypi.org/project/jsonschema/4.26.0/ | 2026-10-27 |
| jsonschema-specifications | 2025.9.1 | MIT | https://pypi.org/project/jsonschema-specifications/2025.9.1/ | 2026-10-27 |
| kubernetes | 37.0.1 | Apache-2.0 | https://pypi.org/project/kubernetes/37.0.1/ | 2026-10-27 |
| markdown-it-py | 4.2.0 | MIT | https://pypi.org/project/markdown-it-py/4.2.0/ | 2026-10-27 |
| markupsafe | 3.0.4 | BSD-3-Clause | https://pypi.org/project/markupsafe/3.0.4/ | 2026-10-27 |
| mdurl | 0.1.2 | MIT | https://pypi.org/project/mdurl/0.1.2/ | 2026-10-27 |
| mmh3 | 5.3.1 | MIT | https://pypi.org/project/mmh3/5.3.1/ | 2026-10-27 |
| multidict | 7.2.1 | Apache-2.0 | https://pypi.org/project/multidict/7.2.1/ | 2026-10-27 |
| narwhals | 2.28.0 | MIT | https://pypi.org/project/narwhals/2.28.0/ | 2026-10-27 |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | https://pypi.org/project/numpy/2.5.4/ | 2026-10-27 |
| oauthlib | 4.0.0 | BSD-3-Clause | https://pypi.org/project/oauthlib/4.0.0/ | 2026-10-27 |
| onnxruntime | 1.31.1 | MIT | https://pypi.org/project/onnxruntime/1.31.1/ | 2026-10-27 |
| openai | 3.40.0 | Apache-2.0 | https://pypi.org/project/openai/3.40.0/ | 2026-10-27 |
| opentelemetry-api | 1.45.1 | Apache-2.0 | https://pypi.org/project/opentelemetry-api/1.45.1/ | 2026-10-27 |
| opentelemetry-exporter-otlp-common | 0.66b1 | Apache-2.0 | https://pypi.org/project/opentelemetry-exporter-otlp-common/0.66b1/ | 2026-10-27 |
| opentelemetry-exporter-otlp-proto-common | 1.45.1 | Apache-2.0 | https://pypi.org/project/opentelemetry-exporter-otlp-proto-common/1.45.1/ | 2026-10-27 |
| opentelemetry-exporter-otlp-proto-grpc | 1.45.1 | Apache-2.0 | https://pypi.org/project/opentelemetry-exporter-otlp-proto-grpc/1.45.1/ | 2026-10-27 |
| opentelemetry-proto | 1.45.1 | Apache-2.0 | https://pypi.org/project/opentelemetry-proto/1.45.1/ | 2026-10-27 |
| opentelemetry-sdk | 1.45.1 | Apache-2.0 | https://pypi.org/project/opentelemetry-sdk/1.45.1/ | 2026-10-27 |
| opentelemetry-semantic-conventions | 0.66b1 | Apache-2.0 | https://pypi.org/project/opentelemetry-semantic-conventions/0.66b1/ | 2026-10-27 |
| orjson | 3.13.0 | MPL-2.0 AND (Apache-2.0 OR MIT) (used unmodified) | https://pypi.org/project/orjson/3.13.0/ | 2026-10-27 |
| overrides | 7.7.0 | Apache-2.0 | https://pypi.org/project/overrides/7.7.0/ | 2026-10-27 |
| packaging | 25.0 | Apache-2.0 OR BSD-2-Clause | https://pypi.org/project/packaging/25.0/ | 2026-10-28 |
| pandas | 3.1.0 | BSD-3-Clause | https://pypi.org/project/pandas/3.1.0/ | 2026-10-27 |
| pillow | 13.0.0 | MIT-CMU | https://pypi.org/project/pillow/13.0.0/ | 2026-10-27 |
| plotly | 7.1.0 | MIT | https://pypi.org/project/plotly/7.1.0/ | 2026-10-27 |
| pluggy | 1.7.0 | MIT | https://pypi.org/project/pluggy/1.7.0/ | 2026-10-27 |
| propcache | 0.5.4 | Apache-2.0 | https://pypi.org/project/propcache/0.5.4/ | 2026-10-27 |
| protobuf | 7.36.2 | BSD-3-Clause | https://pypi.org/project/protobuf/7.36.2/ | 2026-10-27 |
| pyarrow | 26.0.0 | Apache-2.0 | https://pypi.org/project/pyarrow/26.0.0/ | 2026-10-27 |
| pybase64 | 1.5.1 | BSD-2-Clause | https://pypi.org/project/pybase64/1.5.1/ | 2026-10-27 |
| pydantic | 2.14.1 | MIT | https://pypi.org/project/pydantic/2.14.1/ | 2026-10-27 |
| pydantic-core | 2.50.1 | MIT | https://pypi.org/project/pydantic-core/2.50.1/ | 2026-10-27 |
| pydantic-settings | 2.16.1 | MIT | https://pypi.org/project/pydantic-settings/2.16.1/ | 2026-10-27 |
| pydeck | 0.9.3 | Apache-2.0 | https://pypi.org/project/pydeck/0.9.3/ | 2026-10-27 |
| pygments | 2.21.0 | BSD-2-Clause | https://pypi.org/project/pygments/2.21.0/ | 2026-10-27 |
| pypika | 0.51.1 | Apache-2.0 | https://pypi.org/project/pypika/0.51.1/ | 2026-10-27 |
| pyproject-hooks | 1.3.3 | MIT | https://pypi.org/project/pyproject-hooks/1.3.3/ | 2026-10-27 |
| pytest | 9.1.1 | MIT | https://pypi.org/project/pytest/9.1.1/ | 2026-10-27 |
| python-dateutil | 3.0.0 | Apache-2.0 AND BSD-3-Clause | https://pypi.org/project/python-dateutil/3.0.0/ | 2026-10-27 |
| python-dotenv | 1.2.4 | BSD-3-Clause | https://pypi.org/project/python-dotenv/1.2.4/ | 2026-10-27 |
| python-multipart | 0.0.32 | Apache-2.0 | https://pypi.org/project/python-multipart/0.0.32/ | 2026-10-27 |
| pyyaml | 6.0.3 | MIT | https://pypi.org/project/pyyaml/6.0.3/ | 2026-10-27 |
| referencing | 0.37.0 | MIT | https://pypi.org/project/referencing/0.37.0/ | 2026-10-27 |
| requests | 2.34.2 | Apache-2.0 | https://pypi.org/project/requests/2.34.2/ | 2026-10-27 |
| requests-oauthlib | 2.0.0 | ISC | https://pypi.org/project/requests-oauthlib/2.0.0/ | 2026-10-27 |
| rich | 15.0.0 | MIT | https://pypi.org/project/rich/15.0.0/ | 2026-10-27 |
| rpds-py | 2026.9.1 | MIT | https://pypi.org/project/rpds-py/2026.9.1/ | 2026-10-27 |
| shellingham | 1.5.4 | ISC | https://pypi.org/project/shellingham/1.5.4/ | 2026-10-27 |
| six | 1.17.0 | MIT | https://pypi.org/project/six/1.17.0/ | 2026-10-27 |
| sniffio | 1.3.1 | MIT OR Apache-2.0 | https://pypi.org/project/sniffio/1.3.1/ | 2026-10-27 |
| starlette | 1.10.0 | BSD-3-Clause | https://pypi.org/project/starlette/1.10.0/ | 2026-10-27 |
| streamlit | 1.66.1 | Apache-2.0 | https://pypi.org/project/streamlit/1.66.1/ | 2026-10-27 |
| tenacity | 9.2.1 | Apache-2.0 | https://pypi.org/project/tenacity/9.2.1/ | 2026-10-27 |
| tokenizers | 0.23.3 | Apache-2.0 (upstream LICENSE) | https://github.com/huggingface/tokenizers/blob/main/LICENSE | 2026-10-27 |
| tqdm | 4.70.1 | MPL-2.0 AND MIT (used unmodified) | https://pypi.org/project/tqdm/4.70.1/ | 2026-10-27 |
| truststore | 0.10.5 | MIT | https://pypi.org/project/truststore/0.10.5/ | 2026-10-27 |
| typer | 0.27.3 | MIT | https://pypi.org/project/typer/0.27.3/ | 2026-10-27 |
| typing-extensions | 4.16.0 | PSF-2.0 | https://pypi.org/project/typing-extensions/4.16.0/ | 2026-10-27 |
| typing-inspection | 0.4.4 | MIT | https://pypi.org/project/typing-inspection/0.4.4/ | 2026-10-27 |
| tzdata | 2026.5 | Apache-2.0 | https://pypi.org/project/tzdata/2026.5/ | 2026-10-27 |
| urllib3 | 2.8.0 | MIT | https://pypi.org/project/urllib3/2.8.0/ | 2026-10-27 |
| uvicorn[standard] | 0.54.0 | BSD-3-Clause | https://pypi.org/project/uvicorn/0.54.0/ | 2026-10-27 |
| uvloop | 0.23.0 | MIT | https://pypi.org/project/uvloop/0.23.0/ | 2026-10-27 |
| watchdog | 6.0.0 | Apache-2.0 | https://pypi.org/project/watchdog/6.0.0/ | 2026-10-27 |
| watchfiles | 1.3.0 | MIT | https://pypi.org/project/watchfiles/1.3.0/ | 2026-10-27 |
| websocket-client | 1.9.2 | Apache-2.0 | https://pypi.org/project/websocket-client/1.9.2/ | 2026-10-27 |
| websockets | 17.3 | BSD-3-Clause | https://pypi.org/project/websockets/17.3/ | 2026-10-27 |
| xai-sdk | 1.21.0 | Apache-2.0 | https://pypi.org/project/xai-sdk/1.21.0/ | 2026-10-28 |
| yarl | 1.25.1 | Apache-2.0 | https://pypi.org/project/yarl/1.25.1/ | 2026-10-27 |

## Test and lint tools (`requirements-ci.lock`)

Installed in the test image and in CI only; not in the runtime image.

| name | version | SPDX | URL | date |
|---|---|---|---|---|
| black | 26.10.1 | MIT | https://pypi.org/project/black/26.10.1/ | 2026-10-27 |
| coverage | 7.17.0 | Apache-2.0 | https://pypi.org/project/coverage/7.17.0/ | 2026-10-27 |
| mypy-extensions | 1.0.0 | MIT | https://pypi.org/project/mypy-extensions/1.0.0/ | 2026-10-27 |
| pathspec | 1.1.1 | MPL-2.0 (used unmodified) | https://pypi.org/project/pathspec/1.1.1/ | 2026-10-27 |
| platformdirs | 4.13.6 | MIT | https://pypi.org/project/platformdirs/4.13.6/ | 2026-10-27 |
| pytest-asyncio | 1.4.1 | Apache-2.0 | https://pypi.org/project/pytest-asyncio/1.4.1/ | 2026-10-27 |
| pytest-cov | 7.1.0 | MIT | https://pypi.org/project/pytest-cov/7.1.0/ | 2026-10-27 |
| pytokens | 0.4.1 | MIT | https://pypi.org/project/pytokens/0.4.1/ | 2026-10-27 |
| ruff | 0.17.3 | MIT | https://pypi.org/project/ruff/0.17.3/ | 2026-10-27 |

## Notes

- python-dateutil: metadata says "Dual License"; its LICENSE file covers the code under Apache-2.0 and BSD-3-Clause.
- tokenizers: the wheel's metadata carries only the Apache classifier and no license file; the license is taken from the upstream repository's LICENSE (Apache-2.0).
- uvloop: metadata field says MIT; the wheel also ships an Apache-2.0 license file.
