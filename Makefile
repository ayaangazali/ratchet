.PHONY: dev test lint fmt demo serve run console clean image fixture

dev:
	pip install -e ".[dev]"

test:
	RATCHET_BACKEND=local python -m pytest -q

lint:
	ruff check src tests && mypy src/ratchet --ignore-missing-imports

fmt:
	ruff check --fix src tests

demo:
	python -m ratchet.cli demo --dir demo-repo

serve:
	RATCHET_REPO=demo-repo RATCHET_TASK=tasks/demo-001-slugify/task.yaml python -m ratchet.cli serve

run:
	RATCHET_REPO=demo-repo RATCHET_TASK=tasks/demo-001-slugify/task.yaml python -m ratchet.cli run

console:
	python -m ratchet.cli console --repo demo-repo

fixture:
	python scripts/make_fixture.py .ratchet/fixture.bus.jsonl
	@echo 'now: python -m ratchet.cli console --bus .ratchet/fixture.bus.jsonl'

image:
	docker build -t ratchet-task:latest -f Dockerfile.task .

clean:
	rm -rf .ratchet demo-repo .pytest_cache .ruff_cache .mypy_cache
	git worktree prune
