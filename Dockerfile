FROM python:3.10-slim
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev
COPY src src
COPY ui ui
EXPOSE 8000
CMD [".venv/bin/uvicorn", "--factory", "web:create_app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]
