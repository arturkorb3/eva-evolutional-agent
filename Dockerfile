# syntax=docker/dockerfile:1

# EVA (Evolutional Agent) - secure sandbox image.
#
# Only the tiny, non-evolving kernel (organism.py) is baked into the image.
# Everything the organism evolves - releases, state and workspace - lives in
# bind-mounted volumes (see docker-compose.yml). That keeps the kernel
# immutable from inside the container and lets you inspect what EVA does on the
# host under ./data/.

FROM python:3.12-slim

# Unprivileged, fixed UID, no login shell.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin eva \
    && mkdir -p /eva/runtime /eva/state /eva/workspace \
    && chown -R eva:eva /eva

WORKDIR /eva

# Bake ONLY the kernel. The evolving organism never enters the image.
COPY --chown=eva:eva organism.py /eva/organism.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ORGANISM_ROOT=/eva

USER eva

ENTRYPOINT ["python", "/eva/organism.py"]
CMD ["status"]
