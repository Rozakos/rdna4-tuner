import json
import subprocess
import sys
import time

import pytest

from tuner.core.journal import Journal, read_records
from tuner.core.types import JournalBusy, JournalCorrupt


def test_unfinished_intent_and_exclusive_owner(tmp_path):
    path = tmp_path / "journal.jsonl"
    with Journal(path) as journal:
        journal.append("intent", setting="voltage", value=55)
        with pytest.raises(JournalBusy):
            Journal(path)
    with Journal(path) as journal:
        assert journal.crash_bounds() == (55, None)
        assert journal.records[-1]["reason"] == "crash"
    with Journal(path) as journal:
        assert sum(r.get("reason") == "crash" for r in journal.records) == 1


def test_unfinished_test_and_truncated_tail(tmp_path):
    path = tmp_path / "journal.jsonl"
    with Journal(path) as journal:
        journal.append(
            "test_start", settings={"reduction_mv": 60, "vram_mhz": 2600, "timing": "default"}
        )
    with path.open("ab") as stream:
        stream.write(b'{"kind":"test_result","seq":2')
    with Journal(path) as journal:
        assert journal.crash_bounds() == (60, 2600)
        journal.append("run_end", outcome="recovered")
    assert len(read_records(path)) == 3


def test_interior_corruption_is_not_silently_discarded(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text('{"kind":"run_start","seq":1}\ninvalid\n')
    with pytest.raises(JournalCorrupt):
        Journal(path)


def test_real_process_killed_mid_write(tmp_path):
    path, ready = tmp_path / "journal.jsonl", tmp_path / "ready"
    script = """
import os, sys, time
from pathlib import Path
from tuner.core.journal import Journal
j = Journal(Path(sys.argv[1]))
j.append('intent', setting='voltage', value=73)
j.stream.write(b'{"kind":"applied","seq":2')
j.stream.flush()
os.fsync(j.stream.fileno())
Path(sys.argv[2]).write_text('ready')
time.sleep(60)
"""
    child = subprocess.Popen([sys.executable, "-c", script, str(path), str(ready)])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            assert child.poll() is None
            time.sleep(0.01)
        assert ready.exists()
        child.kill()
        child.wait(timeout=10)
        with Journal(path) as journal:
            assert journal.crash_bounds() == (73, None)
            assert journal.records[-1]["reason"] == "crash"
        assert all(isinstance(json.loads(line), dict) for line in path.read_text().splitlines())
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
