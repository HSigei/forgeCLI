# Forge task suite

Real tasks, run end to end through Forge, checked by a script that tests whether the result actually
works for a user. Use it to judge every change to Forge: a change helps when the suite does better
overall, not when one task happens to finish.

```
python evals/run.py --list                 # what's in the suite
python evals/run.py                        # every task, 2 at a time (free tiers rate-limit)
python evals/run.py fix_bug kv_server      # just these
python evals/run.py --compare              # show the change against the previous run
python evals/run.py -p groq                # one provider only (also --model provider:model)
```

Each run is headless (`forge -y --json`, no window, no browser) in a fresh copy of the task's
`seed/`. Results, logs and workspaces land in `evals/.runs/<time>/` (git-ignored): `report.md` and
`results.json`.

## What's measured

| column | meaning |
|---|---|
| result | `check.py` passed **and** no protected file (tests, input data) was changed |
| forge said | whether Forge ended the turn as done; ⚠ = said done but the result doesn't work |
| steps | tool calls in the session |
| errors | tool results that were errors |
| nudges | times Forge told the model it was looping / stuck |
| bounces | times the claim check sent an answer back as unproven |
| test edits | writes to protected files (weakening tests) |

## The tasks

| task | what it covers |
|---|---|
| kv_server | TCP server + protocol; must actually serve (the kvlite case) |
| rest_api | HTTP JSON API with status codes |
| landing_page | static site with required parts, responsive |
| js_todo_app | browser app with pure logic module, run under Node |
| node_cli | a different toolchain (Node, no packages) |
| word_count_cli | Python CLI with flags, stderr, exit codes |
| fix_bug | existing project, failing tests, tests must not change |
| no_weaken_tests | spec forbids loosening tests; needs real features |
| refactor | change structure, keep exact behaviour |
| add_feature | extend existing CLI without breaking old output |
| csv_clean | messy data script |
| codebase_question | answer about call chains without editing code |

## Adding a task

`evals/tasks/<name>/` with:

- `task.yaml`: `prompt: |` block, optional `timeout:` (seconds, default 900), `protect:` list of
  globs that must stay byte-identical, `needs: [node]` for tools that must be on PATH.
- `seed/`: optional starting files.
- `check.py`: runs in the finished workspace; exit 0 = it works. Check behaviour (start the
  server and talk to it, run the CLI), not the presence of words in files.

Add tasks from real failures: when a user reports Forge getting stuck, turn that project into a
task, so the fix is measured from then on.
