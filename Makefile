.PHONY: venv install test coverage clean

PYTHON ?= python3
VENV := .venv
BIN := $(VENV)/bin

venv:
	$(PYTHON) -m venv $(VENV)

install: venv
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -r requirements-dev.txt

test:
	PYTHONPATH=. $(BIN)/python -m pytest -v

coverage:
	PYTHONPATH=. $(BIN)/python -m pytest --cov=src --cov-report=term-missing

clean:
	rm -rf $(VENV) .pytest_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
