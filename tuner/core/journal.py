import json
import os
from dataclasses import asdict
from pathlib import Path

from filelock import FileLock, Timeout

from .types import JournalBusy, JournalCorrupt, Settings


def read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_bytes().splitlines(keepends=True)
    records = []
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
            if not isinstance(record, dict) or "kind" not in record or "seq" not in record:
                raise ValueError("invalid record")
            if record["seq"] != len(records) + 1:
                raise ValueError("invalid sequence")
            records.append(record)
        except (ValueError, UnicodeDecodeError) as exc:
            if index == len(lines) - 1 and not line.endswith(b"\n"):
                break  # A torn final write is recoverable; interior corruption is not.
            raise JournalCorrupt(f"Invalid journal record {index + 1}") from exc
    return records


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.path) + ".lock")
        try:
            self.lock.acquire(timeout=0)
        except Timeout as exc:
            raise JournalBusy("Another process owns this journal") from exc
        try:
            self.records = read_records(self.path)
            # Remove only an incomplete tail, retaining every committed JSON record.
            valid = b"".join(
                (json.dumps(r, separators=(",", ":")) + "\n").encode() for r in self.records
            )
            raw = self.path.read_bytes() if self.path.exists() else b""
            if raw and not raw.endswith(b"\n"):
                complete = raw.splitlines(keepends=True)[: len(self.records)]
                valid = b"".join(complete).rstrip(b"\n") + b"\n" if complete else b""
                with self.path.open("wb") as stream:
                    stream.write(valid)
                    stream.flush()
                    os.fsync(stream.fileno())
            self.stream = self.path.open("ab")
            self.recover()
        except BaseException:
            self.lock.release()
            raise

    def append(self, kind: str, **data) -> dict:
        record = {"kind": kind, "seq": len(self.records) + 1, **data}
        self.stream.write((json.dumps(record, separators=(",", ":")) + "\n").encode())
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.records.append(record)
        return record

    def recover(self) -> None:
        pending = None
        for record in self.records:
            if record["kind"] in ("intent", "test_start"):
                pending = record
            elif record["kind"] in ("test_result", "accepted", "rejected", "run_end"):
                pending = None
            elif record["kind"] == "applied" and pending and pending.get("setting") == "reset":
                pending = None
        if pending:
            self.append(
                "rejected",
                reason="crash",
                setting=pending.get("setting", "profile"),
                value=pending.get("value", pending.get("settings")),
                recovered_seq=pending["seq"],
            )

    def crash_bounds(self) -> tuple[int | None, int | None]:
        voltage, memory = [], []
        for r in self.records:
            if r["kind"] != "rejected" or r.get("reason") != "crash":
                continue
            setting, value = r.get("setting"), r.get("value")
            if setting == "voltage":
                voltage.append(value)
            elif setting == "vram":
                memory.append(value["mhz"])
            elif setting == "profile" and value:
                voltage.append(value["reduction_mv"])
                memory.append(value["vram_mhz"])
        return min(voltage, default=None), min(memory, default=None)

    def last_accepted(self) -> Settings | None:
        for r in reversed(self.records):
            if r["kind"] == "accepted":
                return Settings.from_dict(r["settings"])
        return None

    def accepted(self, settings: Settings) -> None:
        self.append("accepted", settings=asdict(settings))

    def close(self) -> None:
        self.stream.close()
        self.lock.release()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
