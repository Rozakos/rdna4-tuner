import json
import os
from dataclasses import asdict
from pathlib import Path

from filelock import FileLock, Timeout

from .types import CrashBounds, JournalBusy, JournalCorrupt, Settings, Timing


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
    def __init__(self, path: Path, lock_timeout: float = 0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.path) + ".lock")
        try:
            self.lock.acquire(timeout=lock_timeout)
        except Timeout as exc:
            raise JournalBusy("Another process owns this journal") from exc
        try:
            self.records = read_records(self.path)
            raw = self.path.read_bytes() if self.path.exists() else b""
            if raw and not raw.endswith(b"\n"):
                lines = raw.splitlines(keepends=True)
                offset = sum(len(line) for line in lines[: len(self.records)])
                with self.path.open("r+b") as stream:
                    stream.truncate(offset)
                    stream.seek(offset)
                    if offset and not raw[:offset].endswith(b"\n"):
                        stream.write(b"\n")
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
            elif record["kind"] in ("test_result", "passed", "accepted", "rejected", "run_end"):
                pending = None
            elif record["kind"] == "applied" and pending and pending.get("setting") == "reset":
                pending = None
        if pending:
            self.append(
                "rejected",
                reason="crash",
                setting=pending.get("setting", "profile"),
                value=pending.get("value", pending.get("settings")),
                settings=pending.get("settings"),
                recovered_seq=pending["seq"],
            )

    def crash_policy(self, stock: Settings) -> CrashBounds:
        voltage, memory, timings = [], [], set()
        stock_crashed = False
        for r in self.records:
            if r["kind"] != "rejected" or r.get("reason") != "crash":
                continue
            setting, value = r.get("setting"), r.get("value")
            settings = r.get("settings")
            if setting == "profile" and value:
                settings = value
            if settings:
                stock_crashed |= Settings.from_dict(settings) == stock
                if settings["timing"] != stock.timing:
                    timings.add(Timing(settings["timing"]))
            if setting == "voltage":
                if value > stock.reduction_mv:
                    voltage.append(value)
                elif settings is None:
                    stock_crashed = True
            elif setting == "vram":
                if value["mhz"] > stock.vram_mhz:
                    memory.append(value["mhz"])
                if value["timing"] != stock.timing:
                    timings.add(Timing(value["timing"]))
            elif setting == "profile" and value:
                if value["reduction_mv"] > stock.reduction_mv:
                    voltage.append(value["reduction_mv"])
                if value["vram_mhz"] > stock.vram_mhz:
                    memory.append(value["vram_mhz"])
        return CrashBounds(
            min(voltage, default=None),
            min(memory, default=None),
            frozenset(timings),
            stock_crashed,
        )

    def crash_bounds(self) -> tuple[int | None, int | None]:
        start = next((r for r in self.records if r["kind"] == "run_start"), None)
        memory = start["identity"]["limits"]["stock_vram_mhz"] if start else 0
        bounds = self.crash_policy(Settings(0, memory))
        return bounds.voltage, bounds.memory

    def last_passed(self) -> Settings | None:
        for r in reversed(self.records):
            if r["kind"] == "passed":
                return Settings.from_dict(r["settings"])
        return None

    def passed_below(
        self, setting: str, maximum: int, timing: Timing = Timing.DEFAULT
    ) -> int | None:
        """Latest passed lower bracket on this knob, not a final verified profile."""
        for record in reversed(self.records):
            if record["kind"] != "passed":
                continue
            settings = record["settings"]
            if setting == "vram" and settings["timing"] != timing:
                continue
            value = settings["reduction_mv" if setting == "voltage" else "vram_mhz"]
            if value <= maximum:
                return value
        return None

    def last_accepted(self) -> Settings | None:
        for r in reversed(self.records):
            if r["kind"] == "accepted" and r.get("verified"):
                return Settings.from_dict(r["settings"])
        return None

    def accepted(self, settings: Settings, verification_seq: int) -> None:
        self.append(
            "accepted",
            settings=asdict(settings),
            verified=True,
            verification_seq=verification_seq,
        )

    def close(self) -> None:
        self.stream.close()
        self.lock.release()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
