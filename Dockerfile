# ChangeGuard: change impact analysis with a local web app.
#   docker run --rm -p 127.0.0.1:8000:8000 -v changeguard-data:/data ghcr.io/artham-bhardwaj/changeguard
# See the "Docker" section of README.md for mounting your projects and running CLI commands.

FROM python:3.12-slim-bookworm AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY changeguard ./changeguard
COPY evaluation ./evaluation
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.title="ChangeGuard" \
      org.opencontainers.image.description="See what a code change affects, how risky it is, and what to test." \
      org.opencontainers.image.source="https://github.com/artham-bhardwaj/changeguard"

# git reads repositories; bubblewrap + Node.js run sandboxed tests (--verify) for Python and JS/TS.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git bubblewrap nodejs npm ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 # Mounted repositories belong to the host user; let git read them anyway.
 && git config --system --add safe.directory '*'

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

RUN useradd --create-home --uid 1000 changeguard && mkdir -p /data && chown changeguard:changeguard /data
ENV CHANGEGUARD_CONTAINER=1 \
    XDG_DATA_HOME=/data \
    XDG_CONFIG_HOME=/data/config \
    CHANGEGUARD_USE_GH_TOKEN=0 \
    PYTHONUNBUFFERED=1
USER changeguard
WORKDIR /data
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/status', timeout=4)" || exit 1

ENTRYPOINT ["changeguard"]
CMD ["ui"]
