.PHONY: install check lint test screenshots
install:
	uv sync
lint:
	uv run ruff check .
	uv run ruff format --check .
test:
	uv run pytest -q
check: install lint test
# Visual review: PNGs of every page in .forge/screenshots/ (see scripts/screenshots.py).
screenshots: install
	uv run python scripts/screenshots.py
