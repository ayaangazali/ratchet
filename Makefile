.PHONY: dev test lint fmt demo build research pipeline redteam evals bench run run-offline run-graph proof docs console dashboard mascot replay audit tree clean image fixture

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

# The verifier's own eval. Eleven known cheating patterns, two controls that must pass.
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

# The whole shape of the product in a minute: harness routes it, the verifier
# rejects one attempt and accepts another, a human clears the gate, Qodo reviews
# the pull request, every finding becomes work, it merges.
# A goal, a repo or an issue in; a reviewed pull request out. The one to show.
build:
	python -m ratchet.cli build "https://github.com/acme/api/issues/42"

# A paper in, a working implementation out — and it has to reproduce the number.
research:
	python -m ratchet.cli build research "https://arxiv.org/abs/2510.20270"

pipeline:
	python -m ratchet.cli pipeline

# Exercise every claim offline and leave the evidence in .ratchet-proof/<ts>/.
proof:
	bash scripts/prove.sh

# An objective graph run: each node fulfilled only by its tests; a node that
# exhausts its attempts escalates to the tree search. No model, no network.
run-graph:
	python -m ratchet.cli graph --file objectives/demo-graph.yaml --repo demo-repo --scripted demo-repo/patches/scripted_graph.json

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

# Fetch upstream docs through Bright Data (needs BRIGHTDATA_API_KEY in .env).
docs:
	python -m ratchet.cli docs httpx --topic changelog

audit:
	python -m ratchet.cli audit --repo demo-repo

tree:
	python -m ratchet.cli tree --repo demo-repo

image:
	docker build -t ratchet-task:latest -f Dockerfile.task .

clean:
	rm -rf .ratchet demo-repo .pytest_cache .ruff_cache .mypy_cache .ratchet-wt-*
	git worktree prune
