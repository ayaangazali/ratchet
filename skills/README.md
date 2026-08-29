# skills/

Techniques distilled from papers, one per file, written by `ratchet research distill`
and adopted only by `ratchet research trial`.

This directory is empty of skills on purpose. A skill committed here is a claim that
a specific paper produced a specific instruction, and that instruction measurably
improved the agent. Seeding it with plausible-looking examples would attach real
arXiv ids to text those papers did not say — which is the exact failure mode the
whole project exists to catch, committed into the repository as a starting condition.

Fill it by running the pipeline:

```bash
ratchet research search  "reward hacking in coding agents"   # see what is out there
ratchet research distill "reward hacking in coding agents"   # papers -> proposed skills
ratchet research trial <skill> --task tasks/demo-001-slugify/task.yaml
```

## The file format

```markdown
---
name: Assume half the tests are hidden
kind: skill                 # skill | system_prompt
source: arXiv:2510.20270    # where the claim came from, so a reader can check it
title: ImpossibleBench: Measuring LLMs' Propensity of Exploiting Test Cases
url: https://arxiv.org/abs/2510.20270
applies_to: [pytest]        # frameworks; empty means any
triggers: [start, always]   # always | start | stall | regression | cheat
keywords: [overfitting, hidden, tests]
status: proposed            # proposed | adopted | rejected
trial:                      # written by `research trial`. Never edit by hand.
  baseline: 0.33
  treatment: 0.67
  trials: 3
  verdict: adopted
---
The instruction itself, addressed to the agent, under 120 words.
```

## Rules

- **`status: proposed` reaches no prompt.** Only `adopted` skills are injected, and
  a skill is adopted only by winning a paired A/B run of the real search.
- **`rejected` skills stay.** A technique that sounded good and did not work is a
  more useful record than silence: it stops the next person reading the same paper
  and proposing the same thing.
- **Break-even is a rejection.** Every skill costs context in every prompt it
  appears in, so a tie is a loss.
- **The `trial:` block is measured output**, not documentation. It is the evidence
  the skill is carrying its weight.
