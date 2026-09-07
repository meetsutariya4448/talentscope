# Multi-stage. The build stage carries the toolchain needed to compile
# psycopg2 (gcc, libpq-dev); the runtime stage carries neither, only the
# libpq runtime library the compiled extension links against. Previously
# this was single-stage and shipped gcc to production for no reason.
FROM python:3.11-slim AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# --prefix so the whole install tree can be copied out in one layer below.
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Bake the sentence-transformers model into the image instead of downloading
# it from HuggingFace on first use. Three reasons, all of them things that
# actually bit: a cold container otherwise makes a network call on its first
# vector search; that call is unbounded, so warmup time was a function of
# HuggingFace's availability rather than of this image; and every replica
# paid it separately (the k8s worker has no model-cache volume at all, unlike
# compose's hf_cache). ~90 MB against a ~1.9 GB image, and it makes warmup
# deterministic enough for /ready to gate on.
ENV PYTHONPATH=/install/lib/python3.11/site-packages
ENV HF_HOME=/opt/hf
RUN python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('all-MiniLM-L6-v2')"


FROM python:3.11-slim AS runtime

# libpq5 only — the runtime shared library, not the -dev headers or gcc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 && rm -rf /var/lib/apt/lists/*

COPY --from=build /install /usr/local
COPY --from=build /opt/hf /opt/hf

WORKDIR /app
COPY . .

# Run as a non-root user. The image previously ran as root with no USER at
# all, which is the default nobody chooses on purpose.
# The two runtime-writable paths are created here, owned by the app user,
# *before* the volumes mount over them: Docker seeds a named volume from the
# image's directory (contents and ownership included), so a path that doesn't
# exist in the image becomes a root-owned volume the non-root user cannot
# write to. Both are written at runtime — prometheus_multiproc by every
# forked Celery child, /var/lib/celery by beat's PersistentScheduler.
#
# Only those two are chowned. /app and /opt/hf are deliberately left owned by
# root and merely world-readable, because `chown -R` rewrites every file it
# touches into a fresh layer: doing it across /app and /opt/hf added a second
# full copy of both to the image. The app never writes to either
# (PYTHONDONTWRITEBYTECODE=1 keeps .pyc files out of /app).
RUN useradd --create-home --uid 10001 talentscope \
    && mkdir -p /tmp/prometheus_multiproc /var/lib/celery \
    && chown talentscope:talentscope /tmp/prometheus_multiproc /var/lib/celery
USER talentscope

# HF_HUB_OFFLINE=1: the model is already present under HF_HOME, so refuse
# network calls to HuggingFace outright. A cache miss should be a loud
# failure, not a silent runtime download that reintroduces the cold start
# this image exists to remove.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/hf \
    HF_HUB_OFFLINE=1

EXPOSE 8000

# No CMD by design — compose and k8s each supply the command, since the same
# image runs as api, worker, and beat.
