VENV ?= .venv
PYTHON ?= $(VENV)/bin/python

.PHONY: install run test lint frontend-install frontend-run frontend-build

install:
	python3 -m venv $(VENV)
	$(PYTHON) -m pip install -e ".[dev]"

run:
	$(PYTHON) -m uvicorn threadstack.api:app --reload

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .

frontend-install:
	npm --prefix frontend install

frontend-run:
	npm --prefix frontend run dev

frontend-build:
	npm --prefix frontend run build
