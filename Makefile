.PHONY: install check lint test
install:
	uv sync
lint:
	uv run ruff check .
	uv run ruff format --check .
test:
	uv run pytest -q
check: install lint test
