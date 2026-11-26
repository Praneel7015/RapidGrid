# One image: Vite build, then FastAPI serving that build and the API.
FROM node:22.23.5-bookworm-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
# onnxruntime-node (pulled in by the speech model) downloads GPU binaries at
# install time; the app never uses them (speech runs in the browser), and the
# download now 404s. Skip it so installs cannot fail on it.
ENV npm_config_onnxruntime_node_install_cuda=skip
RUN npm ci --legacy-peer-deps --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.11.18-slim-bookworm
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ backend/
COPY --from=frontend /src/frontend/dist frontend/dist
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 RAPIDGRID_DATA_DIR=/var/data
RUN mkdir -p /var/data
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health/live' % os.getenv('PORT', '10000'), timeout=4)"
# One process: in-memory state (fleet, chat, sessions) is not shared across workers.
# The start check only warns (stderr); it never blocks boot.
CMD ["sh", "-c", "python backend/scripts/preflight.py --startup; exec python -m uvicorn main:app --app-dir backend --host 0.0.0.0 --port ${PORT:-10000}"]
