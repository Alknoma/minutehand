# A Linux container that runs an agent through Minutehand inside the patched gVisor sandbox (`sandbox/README.md`).
# Privileged, with the repository at /work and the built sandbox (`sandbox/build.sh`) at /opt/gv.
FROM python:3.12-slim
RUN apt-get -qq update && apt-get -qq install -y --no-install-recommends iproute2 iptables procps curl ca-certificates git >/dev/null && rm -rf /var/lib/apt/lists/*
RUN pip install -q uv slack_sdk
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_CACHE_DIR=/opt/uv-cache UV_LINK_MODE=copy PATH=/opt/gv:$PATH
WORKDIR /work
