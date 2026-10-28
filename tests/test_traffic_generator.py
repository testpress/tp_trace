"""Exercise the traffic CLI without starting infrastructure or sending HTTP traffic."""

import os
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "sample-app/generate_traffic.sh"


@pytest.mark.parametrize(
    "count,workers,mode", [(1, 1, []), (7, 3, []), (2, 5, []), (5, 1, ["--once"]), (9, 2, ["--duration", "1"])]
)
def test_exact_request_count(tmp_path, count, workers, mode):
    curl = tmp_path / "curl"
    curl.write_text('#!/bin/sh\nprintf "request\\n" >> "$REQUEST_LOG"\nprintf "\\n200"\n')
    curl.chmod(0o755)
    request_log = tmp_path / "requests"
    result = subprocess.run(
        ["bash", str(SCRIPT), "--count", str(count), "--concurrency", str(workers), "--sleep", "0", "--redis", *mode],
        env=dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"], REQUEST_LOG=str(request_log)),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert len(request_log.read_text().splitlines()) == count
    assert "Total Requests: \x1b[1m{}\x1b[0m".format(count) in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["--count"],
        ["--count", "0"],
        ["--count", "-1"],
        ["--count", "abc"],
        ["--concurrency", "0"],
        ["--gap"],
        ["--gap", "-1"],
        ["--gap", "abc"],
    ],
)
def test_invalid_request_limits(args):
    result = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=5)
    assert result.returncode != 0


def test_gap_pacing(tmp_path):
    curl = tmp_path / "curl"
    curl.write_text('#!/bin/sh\nprintf "request\\n" >> "$REQUEST_LOG"\nprintf "\\n200"\n')
    curl.chmod(0o755)
    request_log = tmp_path / "requests"
    result = subprocess.run(
        ["bash", str(SCRIPT), "--count", "3", "--concurrency", "1", "--sleep", "0", "--gap", "0.05", "--redis"],
        env=dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"], REQUEST_LOG=str(request_log)),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert len(request_log.read_text().splitlines()) == 3
    assert "Request Gap: 0.05s" in result.stdout
