.PHONY: dev test lint fmt demo redteam evals bench serve run console dashboard mascot replay audit clean image fixture

dev:
	pip install -e ".[dev]"

test:
	python -m pytest -q

lint:
	ruff check ratchet tests scripts && mypy ratchet --ignore-missing-imports

fmt:
	ruff check --fix ratchet tests scripts

demo:
	python -m ratchet.cli demo --dir demo-repo

# The verifier's own eval. Ten known cheating patterns, two controls that must pass.
redteam:
	python -m ratchet.cli redteam --repo demo-repo

# Linear vs search on our own seeded bugs, with error bars.
evals:
	python -m ratchet.cli evals --repo demo-repo --trials 6

# The pre-noon decision: real snapshots, or the worktree fallback.
bench:
	python -m ratchet.cli bench-snapshot --repo demo-repo

# A complete search with no model, no key and no network.
run-offline:
	python -m ratchet.cli run --repo demo-repo --scripted demo-repo/patches/scripted.json

run:
	python -m ratchet.cli run --repo demo-repo

console:
	python -m ratchet.cli console --repo demo-repo

# The same run in a browser, for anyone who would rather be handed a URL.
dashboard:
	python -m ratchet.cli dashboard --repo demo-repo

# Redraw the dolphin from its geometry.
mascot:
	python scripts/make_mascot.py

fixture:
	python scripts/make_fixture.py .ratchet/fixture.bus.jsonl
	@echo "now: python -m ratchet.cli console --bus .ratchet/fixture.bus.jsonl"

replay:
	python -m ratchet.cli replay --bus .ratchet/fixture.bus.jsonl

audit:
	python -m ratchet.cli audit --repo demo-repo

tree:
	python -m ratchet.cli tree --repo demo-repo

image:
	docker build -t ratchet-task:latest -f Dockerfile.task .

clean:
	rm -rf .ratchet demo-repo .pytest_cache .ruff_cache .mypy_cache .ratchet-wt-*
	git worktree prune
