.PHONY: setup test lint check

setup:
	pip install -r requirements.txt
	pip install ruff
	npm install
	npm install --prefix frontend

test:
	pytest tests/ --ignore=tests/test_websocket.py -q

lint:
	ruff check app/ tests/

check: lint test
