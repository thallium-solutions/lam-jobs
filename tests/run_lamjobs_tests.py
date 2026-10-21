#!/usr/bin/env python3
"""Compile and run the @lam/jobs package tests in an isolated extlibs tree.

Every ``tests/test_*.lam`` file is compiled against a temporary
``extlibs/@lam/jobs`` copy of this checkout (the installed-package boundary)
and its standard output is compared with the file's ``# expect:`` lines.

Files containing ``# requires: database`` need PostgreSQL: they run when
``LAM_JOBS_TEST_DATABASE_URL`` is set (``sh test-services.sh`` starts a
disposable server and prints the URL) and are reported as skipped otherwise.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


LIB_ROOT = Path(__file__).resolve().parents[1]
COMPILER = "lamc"
MIN_COMPILER_VERSION = (3, 0, 0)
PACKAGE_PATH = Path("@lam") / "jobs"
DATABASE_ENV = "LAM_JOBS_TEST_DATABASE_URL"
VERSION_PATTERN = re.compile(r"(?:lamc\s+)?v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?")
REQUIRES_PATTERN = re.compile(r"^\s*#\s*requires:\s*(.*)$")


def _run(
    command: list[str], *, cwd: Path, timeout: int, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def _check_compiler() -> bool:
    try:
        proc = _run([COMPILER, "version"], cwd=LIB_ROOT, timeout=10)
    except FileNotFoundError:
        print(f"Error: {COMPILER} is not installed or is not on PATH.", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print(f"Error: {COMPILER} version check timed out.", file=sys.stderr)
        return False
    except OSError as exc:
        print(f"Error: unable to run {COMPILER}: {exc}", file=sys.stderr)
        return False

    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit status {proc.returncode}"
        print(f"Error: unable to check {COMPILER} version: {detail}", file=sys.stderr)
        return False

    version_text = proc.stdout.strip()
    match = VERSION_PATTERN.fullmatch(version_text)
    if not match:
        print(f"Error: {COMPILER} returned an invalid version: {version_text!r}.", file=sys.stderr)
        return False

    version = tuple(int(part) for part in match.groups())
    if version < MIN_COMPILER_VERSION:
        required = ".".join(str(part) for part in MIN_COMPILER_VERSION)
        print(
            f"Error: {COMPILER} {version_text} is incompatible; version >= {required} is required.",
            file=sys.stderr,
        )
        return False
    return True


def _expectations(source: str) -> list[str]:
    return [
        match.group(1).strip()
        for line in source.splitlines()
        if (match := re.match(r"^\s*#\s*expect:\s*(.*)$", line))
    ]


def _requirements(source: str) -> set[str]:
    out: set[str] = set()
    for line in source.splitlines():
        match = REQUIRES_PATTERN.match(line)
        if match:
            out.update(part.strip() for part in match.group(1).split(",") if part.strip())
    return out


def _stage_package(extlibs: Path) -> None:
    destination = extlibs / PACKAGE_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        LIB_ROOT,
        destination,
        ignore=shutil.ignore_patterns(
            ".git",
            "build",
            "extlibs",
            "tests",
            "*.lamc-cache",
            "__pycache__",
            "*.pyc",
        ),
    )


def _process_failure(label: str, proc: subprocess.CompletedProcess[str]) -> str:
    details = [f"{label} ({proc.returncode})"]
    if proc.stdout.strip():
        details.append("stdout:\n" + proc.stdout.rstrip())
    if proc.stderr.strip():
        details.append("stderr:\n" + proc.stderr.rstrip())
    return "\n".join(details)


def _run_case(path: Path, run_timeout: int) -> tuple[bool, str]:
    expected = _expectations(path.read_text(encoding="utf-8"))
    if not expected:
        return False, "TEST ERROR: no '# expect:' lines found"

    with tempfile.TemporaryDirectory(prefix="lamjobs_test_") as tmp:
        root = Path(tmp)
        extlibs = root / "extlibs"
        _stage_package(extlibs)
        binary = root / "test_binary"
        command = [
            COMPILER,
            str(path),
            "--extlibs",
            str(extlibs),
            "--no-cache",
            "-o",
            str(binary),
        ]
        try:
            compile_proc = _run(command, cwd=root, timeout=240)
        except subprocess.TimeoutExpired:
            return False, "COMPILE FAIL: timed out after 240 seconds"
        except OSError as exc:
            return False, f"COMPILE FAIL: unable to run {COMPILER}: {exc}"
        if compile_proc.returncode != 0:
            return False, _process_failure("COMPILE FAIL", compile_proc)

        try:
            run_proc = _run([str(binary)], cwd=root, timeout=run_timeout, env=dict(os.environ))
        except subprocess.TimeoutExpired:
            return False, f"RUN FAIL: timed out after {run_timeout} seconds"
        except OSError as exc:
            return False, f"RUN FAIL: unable to execute test binary: {exc}"
        if run_proc.returncode != 0:
            return False, _process_failure("RUN FAIL", run_proc)

        actual = run_proc.stdout.rstrip("\n")
        expected_text = "\n".join(expected)
        if actual != expected_text:
            expected_lines = expected_text.splitlines()
            actual_lines = actual.splitlines()
            first = next(
                (
                    index
                    for index in range(max(len(expected_lines), len(actual_lines)))
                    if index >= len(expected_lines)
                    or index >= len(actual_lines)
                    or expected_lines[index] != actual_lines[index]
                ),
                0,
            )
            exp_line = expected_lines[first] if first < len(expected_lines) else "<end of output>"
            act_line = actual_lines[first] if first < len(actual_lines) else "<end of output>"
            return (
                False,
                "OUTPUT MISMATCH:\n"
                f"  first difference at line {first + 1}:\n"
                f"    expected: {exp_line!r}\n"
                f"    actual:   {act_line!r}\n"
                f"  full output:\n" + "\n".join(f"    {line}" for line in actual_lines)
                + (f"\n  stderr:\n    {run_proc.stderr.rstrip()}" if run_proc.stderr.strip() else ""),
            )
        return True, "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description="@lam/jobs package tests")
    parser.add_argument("--verbose", "-v", action="store_true", help="show full failure details")
    parser.add_argument("-k", dest="filter", default="", help="only run tests whose file name contains this text")
    parser.add_argument("--run-timeout", type=int, default=180, help="seconds each test binary may run")
    args = parser.parse_args()

    if not _check_compiler():
        return 1

    cases = sorted((LIB_ROOT / "tests").glob("test_*.lam"))
    if args.filter:
        cases = [case for case in cases if args.filter in case.name]
    if not cases:
        print("Error: no @lam/jobs test cases found.", file=sys.stderr)
        return 1

    database = os.environ.get(DATABASE_ENV, "").strip()
    failures: list[tuple[Path, str]] = []
    skipped: list[Path] = []
    print(f"Running {len(cases)} @lam/jobs test(s)...\n")
    if not database:
        print(f"  note: {DATABASE_ENV} is not set; PostgreSQL tests are skipped (sh test-services.sh)\n")
    for case in cases:
        relative = case.relative_to(LIB_ROOT)
        needs = _requirements(case.read_text(encoding="utf-8"))
        unknown = needs - {"database"}
        if unknown:
            failures.append((relative, f"TEST ERROR: unknown requirement(s) {sorted(unknown)}"))
            print(f"  FAIL  {relative}")
            continue
        if "database" in needs and not database:
            skipped.append(relative)
            print(f"  SKIP  {relative} ({DATABASE_ENV} not set)")
            continue
        started = time.monotonic()
        ok, message = _run_case(case, args.run_timeout)
        elapsed = time.monotonic() - started
        print(f"  {'PASS' if ok else 'FAIL'}  {relative} ({elapsed:.1f}s)")
        if not ok:
            failures.append((relative, message))
            if args.verbose:
                for line in message.splitlines():
                    print(f"        {line}")

    passed = len(cases) - len(failures) - len(skipped)
    print(
        f"\n@lam/jobs results: {passed} passed, {len(failures)} failed, "
        f"{len(skipped)} skipped, {len(cases)} total"
    )
    if failures and not args.verbose:
        print("Re-run with --verbose for failure details.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
