# Image for Azure Container Apps, scaled to zero.
#
# BUILD CONTEXT IS repos/, NOT repos/tpm-chatbot-api/.
#
#   docker build -f tpm-chatbot-api/Dockerfile -t tpm-chatbot-api .
#
# because the API imports tpm_rag, which is not published to any index and is
# installed from the sibling path (setup/2-SETUP.ps1 does the same with -e).
# A context of tpm-chatbot-api/ alone cannot see it and the build fails at the
# first import.

FROM python:3.12-slim

# PYTHONUNBUFFERED so the structured log lines - the ones carrying searched=
# and escalated= - reach the Container Apps log stream as they happen rather
# than when the buffer fills. On a scale-to-zero app the buffer may never
# fill before the container is frozen.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependency layers first, so a code edit does not reinstall torch-adjacent
# trees on every build.
COPY tpm-rag/pyproject.toml tpm-rag/README.md ./tpm-rag/
COPY tpm-chatbot-api/pyproject.toml tpm-chatbot-api/README.md ./tpm-chatbot-api/
COPY tpm-rag/src ./tpm-rag/src
# No [ingest] extra, deliberately - that is the whole point of it existing.
# PyMuPDF, pytesseract, Pillow and llama-index-core were required deps and most
# of a 919 MB image, re-pulled on every scale-from-zero cold start, for an OCR
# path this container never runs. tpm_rag/__init__.py defers the loader import
# so serving never touches them. Ingest runs on a dev machine, where
# setup/2-SETUP.ps1 installs "[ingest]".
RUN pip install ./tpm-rag

COPY tpm-chatbot-api/app ./tpm-chatbot-api/app
RUN pip install ./tpm-chatbot-api

# Non-root. Nothing here writes to disk except the escalations SQLite file,
# and that path is configurable - see ESCALATIONS_DB.
RUN useradd --create-home --uid 10001 app \
    && chown -R app:app /srv
USER app

WORKDIR /srv/tpm-chatbot-api

# Container Apps injects PORT. The default matches the local dev port in the
# README so `docker run` behaves the same as `uvicorn --port 8001`.
ENV PORT=8001
EXPOSE 8001

# No --reload, no --workers. One worker per container; Container Apps scales
# by adding replicas, and the rate limiter is in-process so extra workers
# would each keep their own counters and silently multiply the real limit.
CMD ["sh", "-c", "exec uvicorn app.api:app --host 0.0.0.0 --port ${PORT}"]
