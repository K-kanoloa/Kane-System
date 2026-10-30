# syntax=docker/dockerfile:1
# Build context is packages/kanaloa-tools. Reuse its unchanged worker and lockfile.
# Explicit modes avoid inheriting host umask 077 into a non-root container.
FROM python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 IPYTHONDIR=/tmp/ipython HOME=/tmp
COPY --chmod=0444 requirements.lock /opt/kanaloa/requirements.lock
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir --require-hashes -r /opt/kanaloa/requirements.lock
COPY --chmod=0444 worker.py /opt/kanaloa/worker.py
RUN chmod 0755 /opt/kanaloa
USER 1000:1000
WORKDIR /tmp
ENTRYPOINT ["python", "-I", "/opt/kanaloa/worker.py"]
