# g2c game backend + PWA. Build from the game/ directory:
#   docker build -f Dockerfile -t g2c-game .   (context = game/)
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY game-backend/pyproject.toml game-backend/uv.lock* ./
RUN uv sync --no-dev --no-install-project || uv sync --no-dev
COPY game-backend/app.py ./
COPY game-web /app/game-web
ENV WEB_DIR=/app/game-web
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
