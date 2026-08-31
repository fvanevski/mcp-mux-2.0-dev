# Deterministic local assessment

The repository-sanctioned local assessment entry point is:

```text
.github/ci/assessment.py
```

It is a non-mutating host-evidence runner. It does not fetch refs, install dependencies, modify GitHub state, approve a pull request, merge, close an issue, or make a gate decision.

Phase 0 introduced this runner and the initial CI ratchet as an **intentional validation-control transition** authorized on Issue #3. Phase 6 (#9) retains that exact-SHA host-evidence contract and adds the release-hardening CI authority described below; CI does not replace the local assessment runner, and the runner does not replace CI.

## Phase 6 pull-request CI

`.github/workflows/ci.yml` defines the merge-visible release-hardening checks:

- `Quality (Python 3.13)` and `Quality (Python 3.14)`: pinned Ruff plus Pyrefly;
- `Unit tests (Python 3.13)` and `Unit tests (Python 3.14)`: the non-integration regression corpus;
- `Integration tests (Python 3.13)` and `Integration tests (Python 3.14)`: compatibility, deterministic malformed/HTTP/transport upstream-failure fixtures, legacy lifecycle, and observability integration coverage;
- `Dependency audit`: locked runtime dependencies audited with `pip-audit 2.10.1`;
- `MCP conformance (2026-07-28)`: official `@modelcontextprotocol/conformance@0.2.0-alpha.11` requirements run through the mux against the Python MCP SDK 2.1.1 Everything server pinned at commit `0921d94a74db900dccd2d534842aa7b6160542d2`. The only expected-failure baseline is `server-stateless:sep-2575-server-unsupported-version-error`, where the mux correctly terminates the unsupported version at its gateway boundary but the referee additionally expects upstream fixture-internal metrics that cannot exist for an intercepted request.

The supported Python MCP SDK line is current stable v2; the v0.2.0 manifest requires `mcp>=2.1.1,<3`, with the release lock resolving `2.1.1`.

All named CI checks are expected to complete successfully on the exact PR head before merge disposition. Repository/branch protection configuration is a GitHub governance control; workflow success alone does not grant merge authorization. Issue #9 additionally requires the quality, unit, integration, and dependency-audit checks to be configured as required checks on `main`; that policy must be verified independently from workflow success before Phase 6 can be considered complete.

## Authority inputs

The runner binds assessment to:

- explicit exact 40-character `--base-sha`;
- explicit exact 40-character `--expected-head-sha`;
- the current Git branch and clean worktree;
- `.python-version`;
- a pre-existing canonical `.venv` selected by `--venv` (default: `<assessment-worktree>/.venv`);
- `.github/ci/toolchain.txt`;
- `uv.lock`;
- `.github/ci/changed-python.sh`.

The selected `.venv` must already contain the exact static/test tools declared by `.github/ci/toolchain.txt`; version comparison is exact, not prefix/substr matching. Environment construction remains outside the runner so assessment never mutates the candidate merely to obtain green output.

For an isolated assessment worktree, a worktree-local `.venv` will normally be absent. In that case Central may supply the absolute path of the already-established canonical host `.venv` and the assessment must pass it explicitly with `--venv`. Do not create or repair a new environment inside the assessment worktree merely to make the run pass.

## Target kinds

### `implementation`

Use before PR handoff while the exact implementation commit is checked out on a named non-`main` branch:

```text
--base-sha=<exact authoritative base SHA>
--expected-head-sha=<exact implementation HEAD>
```

### `pr`

Use after a PR exists while its exact canonical head is checked out on a named non-`main` branch:

```text
--base-sha=<exact canonical PR base SHA>
--expected-head-sha=<exact canonical PR head SHA>
--pr-number=<PR number>
```

The runner does not discover PR identity from the branch name. Central/GitHub authority supplies the PR identity and exact SHAs.

### `gate`

Use only after merge while exact merged `main` is checked out:

```text
--base-sha=<exact prior trusted/main SHA>
--expected-head-sha=<exact merged-main SHA>
```

Gate mode refuses a branch other than `main`.

## Plan and run

Use `plan` first:

```bash
.github/ci/assessment.py plan pr \
  --base-sha "$BASE_SHA" \
  --expected-head-sha "$HEAD_SHA" \
  --pr-number "$PR_NUMBER" \
  --venv "$ASSESSMENT_VENV"
```

`plan` performs admission/preflight, verifies repository/toolchain identity, and emits the changed-Python membership. It does not execute validation checks and reports:

```text
HOST_EVIDENCE_RESULT=NOT_RUN
GATE_DECISION=NOT_EVALUATED
```

Then execute the same identity with `run`:

```bash
.github/ci/assessment.py run pr \
  --base-sha "$BASE_SHA" \
  --expected-head-sha "$HEAD_SHA" \
  --pr-number "$PR_NUMBER" \
  --venv "$ASSESSMENT_VENV" \
  --output /tmp/mcp-mux-pr-evidence.json
```

Evidence output must be outside the repository.

## Validation plan

For an admitted target, `run` executes:

1. `git diff --check <base> <head>`;
2. Ruff on every ACMR Python file selected between exact base/head;
3. Pyrefly on that same membership;
4. full-project Pytest.

The selector is the same `.github/ci/changed-python.sh` used by project CI. This is a ratchet over pre-existing static debt: every changed Python file must satisfy current Ruff/Pyrefly policy while Pytest remains cumulative.

## Typed result contract

For every syntactically valid runner invocation, the runner emits one canonical JSON evidence envelope to stdout. The envelope always includes:

```text
schema_version
assessment_id
action
target
pr_number
repo_root
branch
base_sha
expected_head_sha
observed_head_sha
python_executable
python_policy
python_version
toolchain_manifest
toolchain_manifest_sha256
uv_lock_sha256
runner_sha256
changed_python
checks
host_evidence_result
gate_decision
warnings
```

Fields whose authority could not yet be established are `null` or retain the requested selector when appropriate. `assessment_id` is `null` when exact base/head identity could not be parsed.

This same canonical envelope is used for early admission failures, including:

- invalid/non-exact SHAs;
- non-positive timeout;
- execution outside a Git worktree;
- unsafe repository-internal `--output`;
- preflight branch/worktree/toolchain failures.

Argument-parser syntax/usage errors (for example, omitting a required CLI argument so `argparse` exits before the runner is invoked) are CLI usage errors and are outside the typed evidence contract.

`host_evidence_result` is one of:

- `PASS` — admitted target and all executed checks passed;
- `FAIL` — candidate validation failed or timed out;
- `BLOCKED` — identity/branch/worktree/admission precondition failed;
- `STALE` — HEAD does not match or moves away from the expected exact head;
- `INFRA_ERROR` — required repository/toolchain infrastructure is missing or the runner itself cannot execute correctly;
- `ISOLATION_BREACH` — worktree content changes during assessment;
- `NOT_RUN` — valid `plan` result before validation checks.

The runner always emits:

```text
GATE_DECISION=NOT_EVALUATED
```

A local `PASS` is host evidence only. Central review, exact-head GitHub CI, formal disposition, merge, issue closure, and gate closure remain separate authorities.

## Regression coverage for the evidence contract

`tests/test_assessment_runner.py` executes the runner as a subprocess and verifies canonical evidence for representative early failures:

- malformed SHA → `BLOCKED`;
- non-positive timeout → `BLOCKED`;
- repository-internal output path → `BLOCKED` with no evidence file created;
- execution outside a Git worktree → `INFRA_ERROR`.

These tests protect the machine-readable failure schema as part of the Phase 0 validation-control contract.

## Non-negotiable failure behavior

Do not respond to a runner failure by:

- changing the requested base/head identity;
- switching to a different ungoverned environment;
- installing floating tool versions;
- weakening Ruff/Pyrefly configuration;
- adding suppressions merely to satisfy the runner;
- skipping or xfail-marking a real regression;
- manually reconstructing the runner-owned lifecycle merely to obtain a different result.

Return the emitted evidence and direct failure output to Central for diagnosis.
