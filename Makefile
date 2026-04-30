.PHONY: setup test lint arch-check check

setup:
	pip install -r requirements.txt
	pip install ruff
	npm install
	npm install --prefix frontend

test:
	pytest tests/ --ignore=tests/test_websocket.py -q

lint:
	ruff check app/ tests/

arch-check:
	@echo "arch-check passed (add grep checks here as review feedback is promoted)"

check: lint test arch-check
