"""Capture 116 benchmark records for two immutable revisions in ABBA order."""

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import tomllib

EXPECTED_FIXTURES = 29
ORDER = ("anchor", "candidate", "candidate", "anchor")
FIXTURES = Path("tools/benchmark-harness/fixtures")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_count(records: list[dict], expected: int = EXPECTED_FIXTURES) -> None:
    require(len(records) == expected, f"expected {expected} records, got {len(records)}")
    require(len({row["fixture"] for row in records}) == expected, "duplicate fixture identities")


def self_test() -> None:
    records = [{"fixture": str(index)} for index in range(EXPECTED_FIXTURES)]
    validate_count(records)
    for invalid in (records[:-1], [records[0]] * EXPECTED_FIXTURES):
        try:
            validate_count(invalid)
        except ValueError:
            continue
        raise RuntimeError("record-count or identity negative control did not fire")
    test_campaign()
    print("Negative controls rejected invalid records, missing captures, changed provenance and wrong SHA", flush=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(arguments: list[str], cwd: Path, log: Path, env: dict | None = None) -> None:
    with log.open("a") as output:
        output.write(f"cwd={cwd}\ncommand={arguments!r}\n")
        output.flush()
        subprocess.run(arguments, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, check=True)


def inventory(source: Path) -> dict:
    entries = tomllib.loads((source / FIXTURES / "groups.toml").read_text())["fixtures"]
    validate_count([{"fixture": entry["path"]} for entry in entries])
    return {
        entry["path"]: {
            "group": entry["group"],
            "bytes": (source / FIXTURES / entry["path"]).stat().st_size,
            "sha256": sha256(source / FIXTURES / entry["path"]),
        }
        for entry in entries
    }


def prepare(repository: Path, workspace: Path, artifacts: Path, revisions: dict) -> dict:
    manifest = {}
    for name, revision in revisions.items():
        source = workspace / name
        log = artifacts / f"{name}-build.log"
        command(["git", "fetch", "origin", revision], repository, log)
        command(["git", "worktree", "add", "--detach", str(source), revision], repository, log)
        command(["rustc", "-Vv"], source, log)
        command(["cargo", "-V"], source, log)
        target = workspace / f"target-{name}"
        environment = dict(os.environ, CARGO_TARGET_DIR=str(target))
        command(
            ["cargo", "build", "--locked", "--release", "-p", "html-to-markdown-bench", "--bin", "htmbench"],
            source,
            log,
            environment,
        )
        binary = workspace / f"htmbench-{name}"
        shutil.copy2(target / "release/htmbench", binary)
        command(["git", "diff", "--exit-code"], source, log)
        manifest[name] = {
            "sha": revision,
            "binary_sha256": sha256(binary),
            "lock_sha256": sha256(source / "Cargo.lock"),
            "toolchain_sha256": sha256(source / "rust-toolchain.toml"),
            "fixtures": inventory(source),
        }
    for name, metadata in manifest.items():
        require(manifest["anchor"]["fixtures"] == metadata["fixtures"], f"fixture corpus changed: {name}")
        require(
            manifest["anchor"]["toolchain_sha256"] == metadata["toolchain_sha256"],
            f"toolchain contract changed: {name}",
        )
    (artifacts / "binary-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def capture(workspace: Path, artifacts: Path, manifest: dict, revisions: dict) -> list[dict]:
    captures = []
    for index, name in enumerate(ORDER, 1):
        binary = workspace / f"htmbench-{name}"
        require(sha256(binary) == manifest[name]["binary_sha256"], "preserved binary changed")
        output = artifacts / f"{index}-{name}.json"
        command(
            [str(binary), "run", "--output", str(output)],
            workspace / name,
            artifacts / f"{index}-{name}.log",
            dict(os.environ, HTMBENCH_LOG="htmbench=info,html_to_markdown=error"),
        )
        data = json.loads(output.read_text())
        validate_count(data["runs"])
        require(data["schema"] == 2 and data["sha"] == revisions[name], "capture provenance mismatch")
        for row in data["runs"]:
            expected = manifest[name]["fixtures"][row["fixture"]]
            require(row["group"] == expected["group"] and row["bytes"] == expected["bytes"], "input mismatch")
        captures.append(data)
        print(f"Capture {index} {name}: {len(data['runs'])} records", flush=True)
    return captures


def validate_campaign(captures: list[dict], revisions: dict) -> int:
    expected_records = EXPECTED_FIXTURES * len(ORDER)
    actual_records = sum(len(data["runs"]) for data in captures)
    require(len(captures) == len(ORDER) and actual_records == expected_records, "campaign count mismatch")
    first = captures[0]
    for name, data in zip(ORDER, captures, strict=True):
        validate_count(data["runs"])
        require(data["schema"] == 2 and data["sha"] == revisions[name], "capture provenance mismatch")
        require(data["provenance"] == first["provenance"], "measurement provenance changed")
        require(data["hostname"] == first["hostname"], "measurement host changed")
    return actual_records


def test_campaign() -> None:
    revisions = {"anchor": "a" * 40, "candidate": "b" * 40}
    captures = [
        {
            "schema": 2,
            "sha": revisions[name],
            "hostname": "test-host",
            "provenance": {"profile": "release"},
            "runs": [{"fixture": str(index)} for index in range(EXPECTED_FIXTURES)],
        }
        for name in ORDER
    ]
    require(
        validate_campaign(captures, revisions) == EXPECTED_FIXTURES * len(ORDER), "positive campaign control failed"
    )
    changed_provenance = copy.deepcopy(captures)
    changed_provenance[-1]["provenance"]["profile"] = "debug"
    wrong_sha = copy.deepcopy(captures)
    wrong_sha[-1]["sha"] = revisions["candidate"]
    cases = (
        (captures[:-1], "campaign count mismatch"),
        (changed_provenance, "measurement provenance changed"),
        (wrong_sha, "capture provenance mismatch"),
    )
    for invalid, expected_error in cases:
        try:
            validate_campaign(invalid, revisions)
        except ValueError as error:
            require(str(error) == expected_error, f"negative control failed for wrong reason: {error}")
            continue
        raise RuntimeError(f"campaign negative control did not fire: {expected_error}")


def summarize(captures: list[dict], artifacts: Path, revisions: dict) -> None:
    actual_records = validate_campaign(captures, revisions)
    outputs = {}
    for data in captures:
        for row in data["runs"]:
            outputs.setdefault(row["fixture"], []).append(row["output_bytes"])
    for fixture, values in outputs.items():
        require(values == list(reversed(values)), f"unstable output size: {fixture}")
    differences = {fixture: values for fixture, values in outputs.items() if len(set(values)) > 1}
    summary = {
        "capture_order": ORDER,
        "expected_records": EXPECTED_FIXTURES * len(ORDER),
        "actual_records": actual_records,
        "output_bytes": differences,
    }
    (artifacts / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary), flush=True)


def compare_guardrails(workspace: Path, artifacts: Path) -> None:
    results = {}
    for index, name in enumerate(ORDER, 1):
        arguments = [
            str(workspace / "htmbench-candidate"),
            "compare",
            "--results",
            str(artifacts / f"{index}-{name}.json"),
            "--baseline",
            str(workspace / "anchor/tools/benchmark-harness/baselines/baseline.json"),
            "--guardrails",
            str(workspace / "anchor/tools/benchmark-harness/guardrails.json"),
        ]
        with (artifacts / f"{index}-{name}-guardrails.log").open("w") as output:
            result = subprocess.run(
                arguments, cwd=workspace / "candidate", stdout=output, stderr=subprocess.STDOUT, check=False
            )
        results[f"{index}-{name}"] = {"exit_code": result.returncode, "passed": result.returncode == 0}
    (artifacts / "guardrail-results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(f"Strict existing guardrail results: {json.dumps(results)}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--base-sha", default=os.environ.get("BASE_SHA"))
    parser.add_argument("--candidate-sha", default=os.environ.get("CANDIDATE_SHA"))
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--workspace", type=Path)
    arguments = parser.parse_args()
    self_test()
    if arguments.self_test:
        return
    require(arguments.artifacts is not None and arguments.workspace is not None, "both directories are required")
    revisions = {"anchor": arguments.base_sha, "candidate": arguments.candidate_sha}
    for name, revision in revisions.items():
        require(isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision), f"invalid full SHA: {name}")
    require(revisions["anchor"] != revisions["candidate"], "revisions must differ")
    artifacts = arguments.artifacts.resolve()
    workspace = arguments.workspace.resolve()
    require(not artifacts.exists() and not workspace.exists(), "diagnostic directories must be new")
    artifacts.mkdir(parents=True)
    workspace.mkdir(parents=True)
    command(["uname", "-a"], Path.cwd(), artifacts / "host.log")
    command(["lscpu"], Path.cwd(), artifacts / "host.log")
    manifest = prepare(Path.cwd(), workspace, artifacts, revisions)
    summarize(capture(workspace, artifacts, manifest, revisions), artifacts, revisions)
    compare_guardrails(workspace, artifacts)


if __name__ == "__main__":
    main()
