.PHONY: venv test lint up sim scan report clean

venv:            ## create local virtualenv with dev deps
	python3.12 -m venv .venv && .venv/bin/pip install -q -e ".[dev]"

test:            ## run the unit/integration tests (fake Devin, in-process)
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check sentinel fake_devin tests

up:              ## real mode: needs .env with DEVIN_API_KEY + GITHUB_TOKEN
	docker compose up --build

sim:             ## simulation mode: fake Devin, GitHub writes logged only
	docker compose -f docker-compose.sim.yml up --build

scan:            ## run the scanner once (files issues on the fork)
	docker compose --profile scan run --rm scanner

report:          ## print the markdown status report from the running service
	curl -s localhost:8080/report

clean:
	docker compose down -v; docker compose -f docker-compose.sim.yml down -v
