# ARGUS FinDash — one-command workflow (macOS / Linux)
#   make            -> set up venv, install, doctor, ingest-if-needed, launch
#   make eval       -> run evaluation suite
#   make ingest     -> (re)build corpus
#   make doctor     -> environment preflight

VENV := .venv
PY := $(VENV)/bin/python

.PHONY: up eval ingest doctor setup

up: setup
	$(PY) run.py up

setup:
	@test -d $(VENV) || python3 -m venv $(VENV)
	@$(PY) -c "import streamlit" 2>/dev/null || $(PY) -m pip install -r requirements.txt

eval: setup
	$(PY) run.py eval

ingest: setup
	$(PY) run.py ingest

doctor: setup
	$(PY) run.py doctor
