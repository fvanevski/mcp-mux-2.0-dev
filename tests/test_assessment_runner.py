from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

RUNNER = Path(__file__).parents[1] / ".github" / "ci" / "assessment.py"
CANONICAL_KEYS = {
    "schema_version",
    "assessment_id",
    "action",
    "target",
    "pr_number",
    "repo_root",
    "branch",
    "base_sha",
    "expected_head_sha",
    "observed_head_sha",
    "python_executable",
    "python_policy",
    "python_version",
    "toolchain_manifest",
    "toolchain_manifest_sha256",
    "uv_lock_sha256",
    "runner_sha256",
    "changed_python",
    "checks",
    "host_evidence_result",
    "gate_decision",
    "warnings",
}


def _run_runner(*args: str) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    proc = subprocess.run(
        [sys.executable, str(RUNNER), *args],
        cwd=RUNNER.parents[2],
        check=False,
        capture_output=True,
        text=True,
    )
    return proc, json.loads(proc.stdout)


def _assert_canonical_evidence(evidence: dict[str, object]) -> None:
    assert CANONICAL_KEYS <= evidence.keys()
    assert evidence["schema_version"] == 1
    assert evidence["gate_decision"] == "NOT_EVALUATED"
    assert isinstance(evidence["warnings"], list)


def test_runner_invalid_sha_uses_canonical_blocked_evidence():
    proc, evidence = _run_runner(
        "plan",
        "implementation",
        "--base-sha",
        "not-a-sha",
        "--expected-head-sha",
        "0" * 40,
    )

    assert proc.returncode == 2
    _assert_canonical_evidence(evidence)
    assert evidence["host_evidence_result"] == "BLOCKED"
    assert evidence["assessment_id"] is None
    assert evidence["base_sha"] == "not-a-sha"


def test_runner_non_positive_timeout_uses_canonical_blocked_evidence():
    proc, evidence = _run_runner(
        "plan",
        "implementation",
        "--base-sha",
        "0" * 40,
        "--expected-head-sha",
        "1" * 40,
        "--timeout",
        "0",
    )

    assert proc.returncode == 2
    _assert_canonical_evidence(evidence)
    assert evidence["host_evidence_result"] == "BLOCKED"
    assert isinstance(evidence["assessment_id"], str)


def test_runner_rejects_repository_internal_output_with_canonical_evidence():
    repo = RUNNER.parents[2]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    output = repo / ".assessment-should-not-exist.json"
    output.unlink(missing_ok=True)

    proc, evidence = _run_runner(
        "plan",
        "implementation",
        "--base-sha",
        head,
        "--expected-head-sha",
        head,
        "--output",
        str(output),
    )

    assert proc.returncode == 2
    _assert_canonical_evidence(evidence)
    assert evidence["host_evidence_result"] == "BLOCKED"
    assert evidence["repo_root"] == str(repo.resolve())
    assert not output.exists()


def test_runner_outside_git_worktree_uses_canonical_infra_error():
    with tempfile.TemporaryDirectory() as temp_dir:
        proc = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "plan",
                "implementation",
                "--base-sha",
                "0" * 40,
                "--expected-head-sha",
                "1" * 40,
            ],
            cwd=temp_dir,
            check=False,
            capture_output=True,
            text=True,
        )
    evidence = json.loads(proc.stdout)

    assert proc.returncode == 4
    _assert_canonical_evidence(evidence)
    assert evidence["host_evidence_result"] == "INFRA_ERROR"
    assert evidence["repo_root"] is None
