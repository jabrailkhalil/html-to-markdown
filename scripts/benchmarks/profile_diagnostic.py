"""Capture two symbolized Linux profiles without changing benchmark gates."""

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from paired_diagnostic import FIXTURES, command, inventory, require, sha256

SELECTED = ("real-world/issues/gh-190/firsteigen.html", "real-world/issues/gh-121-hacker-news.html")
ITERATIONS = 2000


def validate_capture(data: dict, revision: str, fixture: str, metadata: dict) -> None:
    require(data["schema"] == 2 and data["sha"] == revision, "capture provenance mismatch")
    require(len(data["runs"]) == 1, "expected exactly one fixture record")
    row = data["runs"][0]
    require(row["fixture"] == fixture and row["bytes"] == metadata["bytes"], "fixture identity mismatch")
    require(row["group"] == metadata["group"], "fixture group mismatch")
    require(row["output_bytes"] > 0 and len(row["samples_ms"]) == 9, "empty output or incomplete samples")
    require(all(value > 0 for value in row["samples_ms"]), "nonpositive sample")


def self_test() -> None:
    revision = "a" * 40
    metadata = {"bytes": 10, "group": "test"}
    data = {"schema": 2, "sha": revision, "runs": [dict(metadata, fixture="test", output_bytes=5, samples_ms=[1] * 9)]}
    validate_capture(data, revision, "test", metadata)
    for key, value, expected in (
        ("runs", [], "expected exactly one fixture record"),
        ("sha", "b" * 40, "capture provenance mismatch"),
    ):
        invalid = copy.deepcopy(data)
        invalid[key] = value
        try:
            validate_capture(invalid, revision, "test", metadata)
        except ValueError as error:
            require(str(error) == expected, "negative control failed for the wrong reason")
            continue
        raise RuntimeError("negative control did not fire")
    invalid = copy.deepcopy(data)
    invalid["runs"][0]["output_bytes"] = 0
    try:
        validate_capture(invalid, revision, "test", metadata)
    except ValueError as error:
        require(str(error) == "empty output or incomplete samples", "wrong empty-output rejection")
    else:
        raise RuntimeError("empty-output negative control did not fire")
    print("Profile capture controls: valid record accepted; empty, wrong-SHA and zero-output records rejected")


def setup_perf(artifacts: Path) -> Path:
    log = artifacts / "perf-setup.log"
    command(["sudo", "apt-get", "update", "-qq"], Path.cwd(), log)
    command(["sudo", "apt-get", "install", "-y", "linux-tools-generic"], Path.cwd(), log)
    candidates = sorted(Path("/usr/lib/linux-tools").glob("*/perf"))
    require(bool(candidates), "linux-tools-generic installed no perf executable")
    perf = candidates[-1]
    command([str(perf), "version"], Path.cwd(), log)
    command(["sudo", "sysctl", "kernel.perf_event_paranoid=-1"], Path.cwd(), log)
    command(
        [
            str(perf),
            "record",
            "-e",
            "cpu-clock:u",
            "-o",
            str(artifacts / "permission-probe.data"),
            "--",
            "sleep",
            "0.1",
        ],
        Path.cwd(),
        log,
    )
    return perf


def prepare(repository: Path, workspace: Path, artifacts: Path, revision: str) -> tuple[Path, Path, dict]:
    source = workspace / "source"
    log = artifacts / "build.log"
    command(["git", "fetch", "origin", revision], repository, log)
    command(["git", "worktree", "add", "--detach", str(source), revision], repository, log)
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    require(actual == revision, "checked-out revision mismatch")
    target = workspace / "target-symbols"
    environment = dict(
        os.environ,
        CARGO_TARGET_DIR=str(target),
        CARGO_PROFILE_RELEASE_DEBUG="1",
        CARGO_PROFILE_RELEASE_STRIP="none",
        CARGO_INCREMENTAL="0",
    )
    command(["rustc", "-Vv"], source, log)
    command(["cargo", "-V"], source, log)
    command(
        ["cargo", "build", "--locked", "--release", "-p", "html-to-markdown-bench", "--bin", "htmbench"],
        source,
        log,
        environment,
    )
    binary = artifacts / "htmbench"
    shutil.copy2(target / "release/htmbench", binary)
    sections = subprocess.check_output(["readelf", "-SW", str(binary)], cwd=source, text=True)
    (artifacts / "binary-sections.txt").write_text(sections)
    require(".debug_info" in sections and ".symtab" in sections, "profiling binary lacks debug symbols")
    manifest = {
        "sha": revision,
        "binary_sha256": sha256(binary),
        "lock_sha256": sha256(source / "Cargo.lock"),
        "toolchain_sha256": sha256(source / "rust-toolchain.toml"),
        "fixtures": inventory(source),
        "build_overrides": {"release_debug": "1", "release_strip": "none"},
        "rustflags": environment.get("RUSTFLAGS", ""),
        "iterations": ITERATIONS,
        "note": "Profiling build: instrumented timings must not be compared to production gates.",
    }
    (artifacts / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    command(["git", "diff", "--exit-code"], source, log)
    return source, binary, manifest


def capture(perf: Path, source: Path, binary: Path, artifacts: Path, manifest: dict) -> None:
    completed = []
    for index, fixture in enumerate(SELECTED, 1):
        directory = artifacts / f"profile-{index}"
        directory.mkdir()
        fixture_root = directory / "fixtures"
        destination = fixture_root / fixture
        destination.parent.mkdir(parents=True)
        shutil.copy2(source / FIXTURES / fixture, destination)
        metadata = manifest["fixtures"][fixture]
        require(sha256(destination) == metadata["sha256"], "copied fixture changed")
        (fixture_root / "groups.toml").write_text(
            f"[[fixtures]]\npath = {json.dumps(fixture)}\ngroup = {json.dumps(metadata['group'])}\n"
        )
        output = directory / "result.json"
        data = directory / "perf.data"
        require(sha256(binary) == manifest["binary_sha256"], "preserved binary changed")
        command(
            [
                str(perf),
                "record",
                "-e",
                "cpu-clock:u",
                "-F",
                "199",
                "--call-graph",
                "dwarf,16384",
                "-o",
                str(data),
                "--",
                str(binary),
                "run",
                "--fixtures",
                str(fixture_root),
                "--iters",
                str(ITERATIONS),
                "--output",
                str(output),
            ],
            source,
            directory / "record.log",
        )
        result = json.loads(output.read_text())
        validate_capture(result, manifest["sha"], fixture, metadata)
        for name, arguments in (
            ("report.txt", ["report", "--stdio", "--no-children", "--percent-limit", "0"]),
            ("stacks.txt", ["script"]),
        ):
            with (directory / name).open("w") as report, (directory / "decode.log").open("a") as errors:
                subprocess.run(
                    [str(perf), *arguments, "-i", str(data)], cwd=source, stdout=report, stderr=errors, check=True
                )
        stacks = (directory / "stacks.txt").read_text()
        require("htmbench" in stacks and "html_to_markdown" in stacks, "no symbolized converter samples captured")
        completed.append({"fixture": fixture, "output_bytes": result["runs"][0]["output_bytes"]})
    require(len(completed) == 2, "expected exactly two completed profiles")
    (artifacts / "summary.json").write_text(json.dumps(completed, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    arguments = parser.parse_args()
    self_test()
    if arguments.self_test:
        return
    revision = os.environ.get("SOURCE_SHA", "")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", revision)), "SOURCE_SHA must be a full immutable SHA")
    artifacts = Path("benchmark-profile").resolve()
    workspace = Path("benchmark-profile-worktree").resolve()
    require(not artifacts.exists() and not workspace.exists(), "profile directories must be new")
    artifacts.mkdir()
    workspace.mkdir()
    command(["uname", "-a"], Path.cwd(), artifacts / "host.log")
    command(["lscpu"], Path.cwd(), artifacts / "host.log")
    perf = setup_perf(artifacts)
    source, binary, manifest = prepare(Path.cwd(), workspace, artifacts, revision)
    capture(perf, source, binary, artifacts, manifest)


if __name__ == "__main__":
    main()
