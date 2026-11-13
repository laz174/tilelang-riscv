#!/usr/bin/env python3
"""Check actual host-harness UART records from the adapted NR console reporter."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    expected = {
        "pass": {"version": 1, "status": "exited", "app_exit_code": 0},
        "fail": {"version": 1, "status": "exited", "app_exit_code": 1},
        "overflow": {"version": 1, "status": "exited", "app_exit_code": 1},
        "trap": {
            "version": 1,
            "status": "trap",
            "mcause": "0x0000000000000002",
            "mepc": "0x0000000080000040",
            "mtval": "0x00000000FFFFFFFF",
        },
    }
    cases = []
    for name, result in expected.items():
        raw = (out / f"uart-{name}.log").read_bytes()
        lines = raw.splitlines()
        if not raw.endswith(b"\n") or not lines:
            raise ValueError(f"Missing terminal newline in {name}")
        records = [line for line in lines if line.startswith(b"@@FPGA_")]
        if len(records) != 2 or records[0] != lines[0] or records[-1] != lines[-1]:
            raise ValueError(f"Expected one START first and one RESULT last in {name}")
        if records[0] != b'@@FPGA_START {"version":1}':
            raise ValueError(f"Unexpected START in {name}")
        prefix = b"@@FPGA_RESULT "
        if not records[1].startswith(prefix) or json.loads(records[1][len(prefix) :]) != result:
            raise ValueError(f"Unexpected RESULT in {name}")
        if name != "trap":
            verdict = b"PASS" if name == "pass" else b"FAIL"
            if b"verify NR runtime: " + verdict not in lines:
                raise ValueError(f"Runtime verdict differs from protocol in {name}")
        if name == "overflow" and b"[nr] console overflow: FAIL (output truncated)" not in lines:
            raise ValueError("Console overflow path was not exercised")
        cases.append({"case": name, "status": "passed", "terminal_record": result})
    report = {"status": "passed", "kind": "host NR console/UART v1 check", "hardware_run": False, "cases": cases}
    (out / "uart-check.json").write_text(json.dumps(report, indent=2) + "\n")
    print("UART v1 host check: PASS (success, failure, console overflow, NH trap; hardware not run)")


if __name__ == "__main__":
    main()
