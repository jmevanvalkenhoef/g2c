FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project || uv sync --no-dev
COPY app.py cln.py payout.py nip44.py nip04.py bip340.py nwc.py node.py lnd.py lab.py lnd_admin.py ./
CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
