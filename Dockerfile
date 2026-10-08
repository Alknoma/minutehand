# Minutehand as one image: `docker run <image>` serves (`minutehand serve`); `docker run <image> run
# scenario.yaml --agent agent.yaml -- <agent command>` plays a scenario.
#
# The package is installed from the wheel this file builds, never from the source tree, with the
# dependency versions in uv.lock. It lives in its own environment under /opt/minutehand; `python` on
# PATH is the base image's interpreter, which Minutehand does not use, so an agent started inside the
# container runs in an environment of its own, as it would outside one.
#
#   docker build -t minutehand .                     the image a user runs
#   docker build --target example -t example .      the same, plus slack_sdk for examples/follow_up/agent.py

ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3

FROM ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded AS uv

# -- build: the wheel, and an environment holding it and its locked dependencies ----------------------------
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE.md ./
COPY src ./src
RUN uv build --out-dir /dist \
 && uv export --frozen --no-dev --no-emit-project --no-hashes --output-file /dist/requirements.txt \
 && uv venv /opt/minutehand \
 && uv pip install --python /opt/minutehand/bin/python --requirement /dist/requirements.txt \
 && uv pip install --python /opt/minutehand/bin/python --no-deps /dist/*.whl

# -- base: the runtime every target below shares -------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS base
RUN useradd --system --uid 10001 --create-home --home-dir /home/minutehand minutehand \
 && mkdir -p /var/lib/minutehand/ca \
 && chown -R minutehand:minutehand /var/lib/minutehand
COPY --from=build /opt/minutehand /opt/minutehand
RUN ln -s /opt/minutehand/bin/minutehand /usr/local/bin/minutehand
# Runs, the world of each, and the proxy's CA are kept here.
ENV MINUTEHAND_STATE=/var/lib/minutehand PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
VOLUME /var/lib/minutehand
USER minutehand
WORKDIR /home/minutehand
# The standing mode a stack's services use (docs/serve.md): proxy 8080, control API 8081, OTLP 4318.
# `docker run <image> run scenario.yaml ...` still plays a scenario instead.
EXPOSE 8080 8081 4318
ENTRYPOINT ["minutehand"]
CMD ["serve", "--host", "0.0.0.0"]

# -- example: the base image plus the one library examples/follow_up/agent.py needs -----------------------
FROM base AS example
USER root
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore slack_sdk==3.45.0
USER minutehand

# -- runtime: the image a user runs. Last, so a plain `docker build` builds it ------------------------------
FROM base AS runtime
