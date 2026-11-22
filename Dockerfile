# One image: Vite build, then FastAPI serving that build and the API.
FROM node:22-bookworm-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
# onnxruntime-node (pulled in by the speech model) downloads GPU binaries at
# install time; the app never uses them (speech runs in the browser), and the
# download now 404s. Skip it so installs cannot fail on it.
ENV npm_config_onnxruntime_node_install_cuda=skip
RUN npm ci --legacy-peer-deps
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ backend/
COPY --from=frontend /src/frontend/dist frontend/dist
ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "exec python -m uvicorn main:app --app-dir backend --host 0.0.0.0 --port ${PORT:-10000}"]
