#!/usr/bin/env bash
# The proof runner: exercise every claim this repository makes, offline, and leave
# the evidence behind. No model, no key, no network. Run it with `make proof`.
#
# Everything it prints is also written to .ratchet-proof/<timestamp>/, along with
# the raw artifacts a skeptic would ask for: the JSONL bus of each run, the signed
# receipt chains, the graph state files, and a deliberately tampered copy of a
# chain that the auditor must catch.
set -uo pipefail

TS=$(date +%Y%m%d-%H%M%S)
OUT=".ratchet-proof/$TS"
mkdir -p "$OUT"
PASS=0; FAIL=0
declare -a ROWS

say()  { printf "\n\033[1m== %s ==\033[0m\n" "$*"; }
check() { # check <name> <exit_code> <expected_code>
  if [ "$2" -eq "$3" ]; then PASS=$((PASS+1)); ROWS+=("PASS  $1");
  else FAIL=$((FAIL+1)); ROWS+=("FAIL  $1 (exit $2, wanted $3)"); fi
}

say "0 · fresh baseline"
rm -rf demo-repo .ratchet-wt-*
python -m ratchet.cli demo --dir demo-repo > "$OUT/seed.log" 2>&1
git -C demo-repo config user.email proof@ratchet && git -C demo-repo config user.name proof
check "demo repo seeded" $? 0

say "1 · the test suite"
python -m pytest -q > "$OUT/suite.log" 2>&1
check "pytest suite" $? 0
tail -2 "$OUT/suite.log"

say "2 · the red team: eleven known attacks, two controls"
python -m ratchet.cli redteam --repo demo-repo > "$OUT/redteam.log" 2>&1
check "redteam: whole battery caught, zero false positives" $? 0
grep -E "caught|false positives" "$OUT/redteam.log"

say "3 · three standalone verdicts"
python -m ratchet.cli verify --task tasks/demo-001-slugify/task.yaml --repo demo-repo \
  --diff demo-repo/patches/honest.diff > "$OUT/verify-honest.log" 2>&1
check "honest fix -> GREEN" $? 0
python -m ratchet.cli verify --task tasks/demo-001-slugify/task.yaml --repo demo-repo \
  --diff demo-repo/patches/cheat.diff > "$OUT/verify-cheat.log" 2>&1
check "cheating patch -> blocked before executing" $? 1
python -m ratchet.cli verify --task tasks/canary-impossible/task.yaml --repo demo-repo \
  --diff demo-repo/patches/canary_hack.diff > "$OUT/verify-canary.log" 2>&1
check "canary hack -> caught with zero static findings" $? 1
head -1 "$OUT/verify-honest.log"; head -1 "$OUT/verify-cheat.log"; head -1 "$OUT/verify-canary.log"

say "4 · a complete search, with the approval gate exercised live"
python -m ratchet.cli run --repo demo-repo \
  --scripted demo-repo/patches/scripted.json --run-id proof-search > "$OUT/search.log" 2>&1 &
RUNPID=$!
APPROVED=""
for _ in $(seq 1 120); do
  REQ=$(ls demo-repo/.ratchet/approvals/*.request.json 2>/dev/null | head -1) || true
  if [ -n "${REQ:-}" ]; then
    ID=$(basename "$REQ" .request.json)
    echo '{"allow": true, "reason": "approved by the proof runner"}' \
      > "demo-repo/.ratchet/approvals/$ID.json"
    APPROVED="$ID"; break
  fi
  sleep 1
done
wait $RUNPID; RC=$?
check "scripted search reached green and the human gate approved" $RC 0
[ -n "$APPROVED" ] && ROWS+=("PASS  approval gate held until a decision file appeared ($APPROVED)") && PASS=$((PASS+1))
grep -E "winner|approval" "$OUT/search.log" | tail -2
python -m ratchet.cli tree --repo demo-repo --run proof-search > "$OUT/search-tree.log" 2>&1
cat "$OUT/search-tree.log"

say "5 · reseed, then the objective graph: nodes fulfilled only by their tests"
rm -rf demo-repo .ratchet-wt-*
python -m ratchet.cli demo --dir demo-repo >/dev/null 2>&1
git -C demo-repo config user.email proof@ratchet && git -C demo-repo config user.name proof
python -m ratchet.cli graph --file objectives/demo-graph.yaml --repo demo-repo \
  --scripted demo-repo/patches/scripted_graph.json --run-id proof-graph > "$OUT/graph.log" 2>&1
check "objective graph: both nodes fulfilled, one candidate rejected" $? 0
cat "$OUT/graph.log"

say "6 · reseed, then the escalation path: 3 failures hand the node to the tree search"
rm -rf demo-repo .ratchet-wt-*
python -m ratchet.cli demo --dir demo-repo >/dev/null 2>&1
git -C demo-repo config user.email proof@ratchet && git -C demo-repo config user.name proof
python -m ratchet.cli graph --file objectives/demo-graph.yaml --repo demo-repo \
  --scripted demo-repo/patches/scripted_graph_escalation.json --run-id proof-esc > "$OUT/graph-escalation.log" 2>&1
check "exhausted node escalated and the search reached green" $? 0
cat "$OUT/graph-escalation.log"
python -m ratchet.cli tree --repo demo-repo --run proof-esc-truncation > "$OUT/escalation-tree.log" 2>&1
cat "$OUT/escalation-tree.log"

say "7 · every receipt chain verifies"
for run in proof-esc proof-esc-truncation; do
  python -m ratchet.cli audit --repo demo-repo --run "$run" > "$OUT/audit-$run.log" 2>&1
  check "receipt chain intact: $run" $? 0
done

say "8 · and a tampered chain is caught at the exact receipt"
cp demo-repo/.ratchet/proof-esc.receipts.jsonl "$OUT/tampered.receipts.jsonl"
cp demo-repo/.ratchet/proof-esc.receipts.key "$OUT/tampered.receipts.key"
python3 - "$OUT/tampered.receipts.jsonl" <<'PY'
import json, sys
p = sys.argv[1]
lines = open(p).read().splitlines()
r = json.loads(lines[0])
r["score"] = round(r.get("score", 0) + 0.5, 6)   # any change to a signed field must break the chain
r["outcome"] = "green"; r["green"] = True
lines[0] = json.dumps(r, separators=(",", ":"))
open(p, "w").write("\n".join(lines) + "\n")
PY
python -m ratchet.cli audit --receipts "$OUT/tampered.receipts.jsonl" > "$OUT/audit-tampered.log" 2>&1
check "forged 'green' verdict detected -> CHAIN BROKEN" $? 1
grep -E "BROKEN|signature|chain broken" "$OUT/audit-tampered.log" | head -3

say "9 · copy the raw artifacts"
cp -r demo-repo/.ratchet "$OUT/artifacts" 2>/dev/null || true

say "verdict"
printf "%s\n" "${ROWS[@]}" | tee "$OUT/SUMMARY.txt"
echo
echo "checks passed: $PASS · failed: $FAIL"
echo "evidence bundle: $OUT/"
ls -1 "$OUT" | sed 's/^/  /'
[ "$FAIL" -eq 0 ] && echo "every claim demonstrated. this shit actually works." || echo "A CHECK FAILED — read the log above."
exit $FAIL
