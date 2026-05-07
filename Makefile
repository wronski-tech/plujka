# Local checks (default test suite does not require PostgreSQL).
.PHONY: test test-integration compile

PYTHON ?= python3

test:
	PYTHONPATH=. $(PYTHON) -m unittest discover -s tests -p 'test*.py' -v

test-integration:
	PLUJKA_RUN_DB_TESTS=1 PYTHONPATH=. $(PYTHON) -m unittest discover -s tests -p 'test*.py' -v

compile:
	$(PYTHON) -m compileall -q api scripts streamlit_app tests
