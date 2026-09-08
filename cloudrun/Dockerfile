# Build only the reviewed live source context. Engine/codegen/probe bytes are
# SHA-pinned runtime inputs, never a clone of an arbitrary remote branch.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
ARG LIVE_COMMIT
RUN printf '%s' "$LIVE_COMMIT" | python -c "import re,sys; assert re.fullmatch('[0-9a-f]{40}',sys.stdin.read()), 'LIVE_COMMIT must be a full commit hash'"
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates cmake g++ git libeigen3-dev ninja-build \
    && rm -rf /var/lib/apt/lists/*
# Match the campaign's generated-strategy Eigen include search contract.
RUN ln -s /usr/include/eigen3/Eigen /usr/include/Eigen \
    && ln -s /usr/include/eigen3/unsupported /usr/include/unsupported \
    && useradd --create-home --uid 10001 runner \
    && mkdir -p /app /workspace \
    && chown runner:runner /app /workspace
WORKDIR /app
COPY --chown=runner:runner pineforge_live /app/pineforge_live
COPY --chown=runner:runner LICENSE pyproject.toml /app/
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PINEFORGE_LIVE_BUILD_COMMIT=${LIVE_COMMIT} \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    GIT_NO_REPLACE_OBJECTS=1 GIT_TERMINAL_PROMPT=0
LABEL org.opencontainers.image.revision=${LIVE_COMMIT}
USER runner
# Import-only build check. No strategy execution or grading during image build.
RUN python -c "import sys, ctypes, sqlite3, ssl; assert sys.version_info[:2] == (3, 12); import pineforge_live.verification.cloud_io"
ENTRYPOINT ["python", "-m", "pineforge_live.verification.campaign_worker"]
