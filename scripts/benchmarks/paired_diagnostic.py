"""Capture a fixed ABBA benchmark diagnostic without changing calibration policy."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import tomllib

EXPECTED_FIXTURES = 29
REVISIONS = {
    "anchor": "c87f68acde9af870d9a3a3fb169c8208e0a5654c",
    "candidate": "28c92d40441913a3cb2b1d0c8307c89fb13e872d",
}
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
    print("Negative controls rejected 28 records and 29 duplicate identities", flush=True)


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


def prepare(repository: Path, workspace: Path, artifacts: Path) -> dict:
    manifest = {}
    for name, revision in REVISIONS.items():
        source = workspace / name
        log = artifacts / f"{name}-build.log"
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
    require(manifest["anchor"]["fixtures"] == manifest["candidate"]["fixtures"], "fixture corpus changed")
    require(
        manifest["anchor"]["toolchain_sha256"] == manifest["candidate"]["toolchain_sha256"],
        "toolchain contract changed",
    )
    (artifacts / "binary-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def capture(workspace: Path, artifacts: Path, manifest: dict) -> list[dict]:
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
        require(data["schema"] == 2 and data["sha"] == REVISIONS[name], "capture provenance mismatch")
        for row in data["runs"]:
            expected = manifest[name]["fixtures"][row["fixture"]]
            require(row["group"] == expected["group"] and row["bytes"] == expected["bytes"], "input mismatch")
        captures.append(data)
        print(f"Capture {index} {name}: {len(data['runs'])} records", flush=True)
    return captures


def summarize(captures: list[dict], artifacts: Path) -> None:
    expected_records = EXPECTED_FIXTURES * len(ORDER)
    actual_records = sum(len(data["runs"]) for data in captures)
    require(len(captures) == len(ORDER) and actual_records == expected_records, "campaign count mismatch")
    first = captures[0]
    for data in captures[1:]:
        require(data["provenance"] == first["provenance"], "measurement provenance changed")
        require(data["hostname"] == first["hostname"], "measurement host changed")
    outputs = {}
    for data in captures:
        for row in data["runs"]:
            outputs.setdefault(row["fixture"], []).append(row["output_bytes"])
    for fixture, values in outputs.items():
        require(values[0] == values[3] and values[1] == values[2], f"unstable output size: {fixture}")
    differences = {fixture: values for fixture, values in outputs.items() if len(set(values)) > 1}
    summary = {"expected_records": expected_records, "actual_records": actual_records, "output_bytes": differences}
    (artifacts / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--workspace", type=Path)
    arguments = parser.parse_args()
    self_test()
    if arguments.self_test:
        return
    require(arguments.artifacts is not None and arguments.workspace is not None, "both directories are required")
    artifacts = arguments.artifacts.resolve()
    workspace = arguments.workspace.resolve()
    require(not artifacts.exists() and not workspace.exists(), "diagnostic directories must be new")
    artifacts.mkdir(parents=True)
    workspace.mkdir(parents=True)
    command(["uname", "-a"], Path.cwd(), artifacts / "host.log")
    command(["lscpu"], Path.cwd(), artifacts / "host.log")
    manifest = prepare(Path.cwd(), workspace, artifacts)
    summarize(capture(workspace, artifacts, manifest), artifacts)


if __name__ == "__main__":
    main()
