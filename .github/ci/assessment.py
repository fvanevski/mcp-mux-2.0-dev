#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
EXIT = {
    "PASS": 0,
    "FAIL": 1,
    "BLOCKED": 2,
    "STALE": 3,
    "INFRA_ERROR": 4,
    "ISOLATION_BREACH": 5,
    "NOT_RUN": 0,
}


class Stop(RuntimeError):
    def __init__(self, result: str, message: str) -> None:
        super().__init__(message)
        self.result = result


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def exact_sha(value: str, label: str) -> str:
    value = value.lower()
    if not SHA_RE.fullmatch(value):
        raise Stop("BLOCKED", f"{label} must be an exact 40-character SHA")
    return value


def command(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    print(f"+ {' '.join(argv)}", file=sys.stderr)
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise Stop("INFRA_ERROR", f"executable not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise Stop("FAIL", f"command timed out after {timeout}s: {' '.join(argv)}") from exc


def git(repo: Path, *args: str) -> str:
    proc = command(["git", *args], cwd=repo, timeout=60, capture=True)
    if proc.returncode:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise Stop("INFRA_ERROR", f"git {' '.join(args)} failed: {detail}")
    return proc.stdout.strip()


def root() -> Path:
    proc = command(["git", "rev-parse", "--show-toplevel"], cwd=Path.cwd(), timeout=60, capture=True)
    if proc.returncode:
        raise Stop("INFRA_ERROR", "runner must execute inside a Git worktree")
    return Path(proc.stdout.strip()).resolve()


def manifest_versions(path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            raise Stop("INFRA_ERROR", f"unbound toolchain entry: {line}")
        name, version = line.split("==", 1)
        versions[name] = version
    if set(versions) != {"ruff", "pyrefly", "pytest"}:
        raise Stop("INFRA_ERROR", "toolchain must contain exactly ruff, pyrefly, and pytest")
    return versions


def check_version(
    executable: Path,
    expected: str,
    *,
    repo: Path,
    timeout: int,
) -> str:
    if not executable.is_file():
        raise Stop("INFRA_ERROR", f"missing required executable: {executable}")
    proc = command([os.fspath(executable), "--version"], cwd=repo, timeout=timeout, capture=True)
    if proc.returncode:
        raise Stop("INFRA_ERROR", f"failed to query version: {executable}")
    observed = (proc.stdout or proc.stderr).strip()
    if expected not in observed:
        raise Stop("INFRA_ERROR", f"version mismatch: expected {expected!r}, observed {observed!r}")
    print(observed, file=sys.stderr)
    return observed


def changed_python(repo: Path, base: str, head: str, timeout: int) -> list[str]:
    selector = repo / ".github" / "ci" / "changed-python.sh"
    if not selector.is_file():
        raise Stop("INFRA_ERROR", f"missing selector: {selector}")
    proc = command(
        [os.fspath(selector), base, head],
        cwd=repo,
        timeout=timeout,
        capture=True,
    )
    if proc.returncode:
        raise Stop("INFRA_ERROR", "changed-Python selector failed")
    files = [line for line in proc.stdout.splitlines() if line]
    if files:
        print("\n".join(files), file=sys.stderr)
    else:
        print("No changed Python files.", file=sys.stderr)
    return files


def safe_output(path: Path | None, repo: Path) -> Path | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(repo)
    except ValueError:
        return resolved
    raise Stop("BLOCKED", "--output must be outside the repository")


def preflight(args: argparse.Namespace, repo: Path, base: str, head: str) -> dict[str, object]:
    branch = git(repo, "branch", "--show-current") or None
    observed = exact_sha(git(repo, "rev-parse", "HEAD"), "observed HEAD")
    if observed != head:
        raise Stop("STALE", f"HEAD mismatch: expected {head}, observed {observed}")

    if args.target == "gate":
        if branch != "main":
            raise Stop("BLOCKED", f"gate assessment requires main, observed {branch!r}")
    elif branch in {None, "main"}:
        raise Stop("BLOCKED", f"{args.target} assessment requires a named non-main branch")

    if args.target == "pr" and args.pr_number is None:
        raise Stop("BLOCKED", "pr assessment requires --pr-number")
    if args.target != "pr" and args.pr_number is not None:
        raise Stop("BLOCKED", "--pr-number is valid only for pr assessment")

    if git(repo, "status", "--porcelain=v1", "--untracked-files=normal"):
        raise Stop("BLOCKED", "worktree must be clean before assessment")

    git(repo, "cat-file", "-e", f"{base}^{{commit}}")
    if exact_sha(git(repo, "merge-base", base, head), "merge base") != base:
        raise Stop("BLOCKED", f"base {base} is not an ancestor of head {head}")

    manifest = repo / ".github" / "ci" / "toolchain.txt"
    lock = repo / "uv.lock"
    if not manifest.is_file() or not lock.is_file():
        raise Stop("INFRA_ERROR", "toolchain manifest and uv.lock are required")
    versions = manifest_versions(manifest)

    venv = (repo / args.venv).resolve() if not args.venv.is_absolute() else args.venv.resolve()
    python = venv / "bin" / "python"
    if not python.is_file():
        raise Stop("INFRA_ERROR", f"missing venv Python: {python}")

    py = command(
        [os.fspath(python), "-c", "import platform; print(platform.python_version())"],
        cwd=repo,
        timeout=args.timeout,
        capture=True,
    )
    if py.returncode:
        raise Stop("INFRA_ERROR", "failed to query venv Python")
    python_version = py.stdout.strip()
    policy_path = repo / ".python-version"
    policy = policy_path.read_text(encoding="utf-8").strip() if policy_path.is_file() else None
    if policy and not (python_version == policy or python_version.startswith(f"{policy}.")):
        raise Stop("INFRA_ERROR", f"venv Python {python_version} violates .python-version {policy}")
    print(f"python {python_version}", file=sys.stderr)

    check_version(venv / "bin" / "ruff", f"ruff {versions['ruff']}", repo=repo, timeout=args.timeout)
    check_version(
        venv / "bin" / "pyrefly",
        f"pyrefly {versions['pyrefly']}",
        repo=repo,
        timeout=args.timeout,
    )
    check_version(
        venv / "bin" / "pytest",
        f"pytest {versions['pytest']}",
        repo=repo,
        timeout=args.timeout,
    )

    files = changed_python(repo, base, head, args.timeout)
    return {
        "branch": branch,
        "observed_head_sha": observed,
        "venv": os.fspath(venv),
        "python_executable": os.fspath(python),
        "python_version": python_version,
        "python_policy": policy,
        "changed_python": files,
        "toolchain_sha256": sha256(manifest),
        "uv_lock_sha256": sha256(lock),
    }


def run_checks(
    args: argparse.Namespace,
    repo: Path,
    base: str,
    head: str,
    state: dict[str, object],
) -> tuple[str, list[dict[str, object]], list[str]]:
    venv = Path(str(state["venv"]))
    changed = state["changed_python"]
    if not isinstance(changed, list):
        raise Stop("INFRA_ERROR", "internal changed-Python state is invalid")
    files = [str(item) for item in changed]
    checks: list[dict[str, object]] = []

    planned = [
        ("git-diff-check", ["git", "diff", "--check", base, head]),
        ("ruff", [os.fspath(venv / "bin" / "ruff"), "check", *files]),
        ("pyrefly", [os.fspath(venv / "bin" / "pyrefly"), "check", *files]),
        ("pytest", [os.fspath(venv / "bin" / "pytest"), "-q"]),
    ]
    for name, argv in planned:
        if name in {"ruff", "pyrefly"} and not files:
            checks.append({"name": name, "status": "SKIPPED", "reason": "no changed Python files"})
            continue
        proc = command(argv, cwd=repo, timeout=args.timeout)
        checks.append({"name": name, "status": "PASS" if proc.returncode == 0 else "FAIL"})
    result = "FAIL" if any(check["status"] == "FAIL" for check in checks) else "PASS"

    warnings: list[str] = []
    final_head = exact_sha(git(repo, "rev-parse", "HEAD"), "final HEAD")
    if final_head != head:
        result = "STALE"
        warnings.append(f"HEAD moved during assessment: {head} -> {final_head}")
    final_status = git(repo, "status", "--porcelain=v1", "--untracked-files=normal")
    if final_status:
        result = "ISOLATION_BREACH"
        warnings.append(f"worktree changed during assessment:\n{final_status}")
    return result, checks, warnings


def identity(
    args: argparse.Namespace,
    base: str,
    head: str,
    runner_sha: str,
    state: dict[str, object],
) -> str:
    payload = json.dumps(
        {
            "schema_version": 1,
            "action": args.action,
            "target": args.target,
            "base_sha": base,
            "head_sha": head,
            "pr_number": args.pr_number,
            "runner_sha256": runner_sha,
            "toolchain_sha256": state["toolchain_sha256"],
            "uv_lock_sha256": state["uv_lock_sha256"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic local assessment for implementation, PR, and merged-main gates."
    )
    parser.add_argument("action", choices=("plan", "run"))
    parser.add_argument("target", choices=("implementation", "pr", "gate"))
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--expected-head-sha", required=True)
    parser.add_argument("--pr-number", type=int)
    parser.add_argument("--venv", type=Path, default=Path(".venv"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    runner_sha = sha256(Path(__file__).resolve())
    requested_base = args.base_sha.lower()
    requested_head = args.expected_head_sha.lower()

    repo: Path | None = None
    base: str | None = None
    head: str | None = None
    output: Path | None = None
    output_allowed = False
    state: dict[str, object] = {
        "branch": None,
        "observed_head_sha": None,
        "venv": None,
        "python_executable": None,
        "python_version": None,
        "python_policy": None,
        "changed_python": [],
        "toolchain_sha256": None,
        "uv_lock_sha256": None,
    }
    result = "NOT_RUN" if args.action == "plan" else "PASS"
    checks: list[dict[str, object]] = []
    warnings: list[str] = []

    try:
        base = exact_sha(requested_base, "base SHA")
        head = exact_sha(requested_head, "expected head SHA")
        if args.timeout < 1:
            raise Stop("BLOCKED", "--timeout must be positive")

        repo = root()
        venv = (repo / args.venv).resolve() if not args.venv.is_absolute() else args.venv.resolve()
        state["venv"] = os.fspath(venv)
        state["python_executable"] = os.fspath(venv / "bin" / "python")
        output = safe_output(args.output, repo)
        output_allowed = output is not None

        state.update(preflight(args, repo, base, head))
        if args.action == "run":
            result, checks, run_warnings = run_checks(args, repo, base, head, state)
            warnings.extend(run_warnings)
    except Stop as exc:
        result = exc.result
        warnings.append(str(exc))
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        result = "INFRA_ERROR"
        warnings.append(f"unexpected runner error: {type(exc).__name__}: {exc}")

    assessment_id = (
        identity(args, base, head, runner_sha, state)
        if base is not None and head is not None
        else None
    )
    evidence = {
        "schema_version": 1,
        "assessment_id": assessment_id,
        "action": args.action,
        "target": args.target,
        "pr_number": args.pr_number,
        "repo_root": os.fspath(repo) if repo is not None else None,
        "branch": state["branch"],
        "base_sha": base if base is not None else requested_base,
        "expected_head_sha": head if head is not None else requested_head,
        "observed_head_sha": state["observed_head_sha"],
        "python_executable": state["python_executable"],
        "python_policy": state["python_policy"],
        "python_version": state["python_version"],
        "toolchain_manifest": ".github/ci/toolchain.txt",
        "toolchain_manifest_sha256": state["toolchain_sha256"],
        "uv_lock_sha256": state["uv_lock_sha256"],
        "runner_sha256": runner_sha,
        "changed_python": state["changed_python"],
        "checks": checks,
        "host_evidence_result": result,
        "gate_decision": "NOT_EVALUATED",
        "warnings": warnings,
    }

    if output_allowed and output is not None:
        payload = json.dumps(evidence, sort_keys=True, indent=2) + "\n"
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(payload, encoding="utf-8")
            print(f"evidence_path={output}", file=sys.stderr)
            print(f"evidence_sha256={hashlib.sha256(payload.encode()).hexdigest()}", file=sys.stderr)
        except (OSError, UnicodeError) as exc:
            result = "INFRA_ERROR"
            warnings.append(f"failed to write evidence output: {type(exc).__name__}: {exc}")
            evidence["host_evidence_result"] = result
            evidence["warnings"] = warnings

    payload = json.dumps(evidence, sort_keys=True, indent=2) + "\n"
    print(payload, end="")
    return EXIT[result]


if __name__ == "__main__":
    raise SystemExit(main())
