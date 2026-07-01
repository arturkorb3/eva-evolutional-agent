# syntax=docker/dockerfile:1

# EVA (Evolvable Virtual Agent) - secure sandbox image.
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

# A JavaScript runtime so the agent can execute/test JS it writes (node only,
# no npm, to keep the image small). The agent itself still cannot install
# software - the rootfs stays read-only at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /eva

# Bake ONLY the kernel and the initial genome (seed). The evolving organism
# never enters the image; releases, state and workspace are bind-mounted.
COPY --chown=eva:eva organism.py /eva/organism.py
COPY --chown=eva:eva seed /eva/seed

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ORGANISM_ROOT=/eva

USER eva

ENTRYPOINT ["python", "/eva/organism.py"]
CMD ["status"]
