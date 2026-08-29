# Deterministic local assessment

The repository-sanctioned local assessment entry point is:

```text
.github/ci/assessment.py
```

It is a non-mutating host-evidence runner. It does not fetch refs, install dependencies, modify GitHub state, approve a pull request, merge, close an issue, or make a gate decision.

## Authority inputs

The runner binds assessment to:

- an explicit exact 40-character `--base-sha`;
- an explicit exact 40-character `--expected-head-sha`;
- the current Git branch and clean worktree;
- `.python-version`;
- the repository-local `.venv`;
- `.github/ci/toolchain.txt`;
- `uv.lock`;
- `.github/ci/changed-python.sh`.

The local `.venv` must already contain the exact static/test tools declared by `.github/ci/toolchain.txt`. Environment construction remains outside the runner so an assessment never mutates the candidate merely to obtain green output.

## Target kinds

### `implementation`

Use before opening or updating a PR while the exact candidate commit is checked out on a named non-`main` working branch.

Required authority:

```text
--base-sha=<exact authoritative main/base SHA>
--expected-head-sha=<exact current implementation HEAD>
```

### `pr`

Use after a PR exists while its exact canonical head commit is checked out on the working branch.

Required authority:

```text
--base-sha=<exact canonical PR base SHA>
--expected-head-sha=<exact canonical PR head SHA>
--pr-number=<PR number>
```

The runner does not discover or trust PR identity from the local branch name. Central/GitHub authority supplies the exact PR identity and SHAs.

### `gate`

Use only after the candidate has been merged and the exact merged `main` commit is checked out.

Required authority:

```text
--base-sha=<exact prior trusted/main control SHA>
--expected-head-sha=<exact merged main SHA under gate assessment>
```

Gate mode refuses to run from a branch other than `main`.

## Plan and run

Use `plan` first:

```bash
.github/ci/assessment.py plan implementation \
  --base-sha "$BASE_SHA" \
  --expected-head-sha "$HEAD_SHA"
```

`plan` performs admission/preflight, verifies repository and toolchain identity, and emits the exact changed-Python membership. It does not execute validation gates and reports:

```text
HOST_EVIDENCE_RESULT=NOT_RUN
GATE_DECISION=NOT_EVALUATED
```

Then execute the same identity with `run`:

```bash
.github/ci/assessment.py run implementation \
  --base-sha "$BASE_SHA" \
  --expected-head-sha "$HEAD_SHA" \
  --output /tmp/mcp-mux-implementation-evidence.json
```

Evidence output must be outside the repository so evidence generation cannot dirty the assessed worktree.

## Validation plan

For a valid admitted target, `run` executes:

1. `git diff --check <base> <head>`;
2. Ruff on every ACMR Python file selected between exact base and head;
3. Pyrefly on the same exact changed-Python membership;
4. full-project Pytest.

The static selector is the same `.github/ci/changed-python.sh` used by project CI. This is a ratchet over existing static debt: any Python file changed by a candidate must satisfy current Ruff/Pyrefly policy, while full-project Pytest remains cumulative.

## Typed results

The runner emits JSON to stdout and, when `--output` is supplied, writes the same JSON to that external path.

`host_evidence_result` is one of:

- `PASS` — admitted target and all executed checks passed;
- `FAIL` — candidate validation failed or timed out;
- `BLOCKED` — target identity/branch/worktree admission failed;
- `STALE` — current HEAD does not match, or changes away from, the expected exact head;
- `INFRA_ERROR` — required repository/toolchain infrastructure is missing or incompatible;
- `ISOLATION_BREACH` — the worktree changes during assessment;
- `NOT_RUN` — valid `plan` result before checks execute.

The runner always emits:

```text
GATE_DECISION=NOT_EVALUATED
```

A local `PASS` is host evidence only. Central review, exact-head GitHub CI, formal PR disposition, merge, issue closure, and gate closure remain separate authorities.

## Non-negotiable failure behavior

Do not respond to runner failures by:

- changing the base/head identity;
- switching to a different `.venv`;
- installing floating tool versions;
- weakening Ruff/Pyrefly configuration;
- adding suppressions merely to satisfy the runner;
- skipping or marking failing tests expected;
- running a manual substitute lifecycle to obtain a different result.

Return the emitted evidence and direct failure output to Central for diagnosis.
