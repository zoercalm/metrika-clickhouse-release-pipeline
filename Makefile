.PHONY: up install demo test down
PY := .venv/bin/python

up:            ## start a local ClickHouse
	docker compose up -d --wait

install:       ## create a virtualenv and install the package
	python3 -m venv .venv
	$(PY) -m pip install -q -e ".[dev]"

demo: up       ## publish, reconcile, check health
	.venv/bin/rp release
	.venv/bin/rp reconcile
	.venv/bin/rp health

test: up       ## run the test-suite against the local ClickHouse
	$(PY) -m pytest -q

down:
	docker compose down -v
