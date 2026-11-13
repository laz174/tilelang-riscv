#!/usr/bin/env python3
"""Arithmetic/memory-range checks of emitted BOSCAME IR in Buddy's model.

This is a software IR interpretation, not an execution of the ELF or a cache,
boot, timing, or FPGA validation. Rank-2 entry descriptors are supplied directly
because Buddy's convenience simulate() initializes arguments as flat byte views.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
from pathlib import Path


def test_inputs(m, n, k, case):
    """Match launch.c: ordinary values, accumulation, INT8 limits, zero A."""
    a = [(3 * i + 1 + case) % 7 - 3 for i in range(m * k)]
    b = [(5 * i + 2 + case) % 9 - 4 for i in range(n * k)]
    initial = [0 if case == 0 else i % 5 - 2 for i in range(m * n)]
    if case == 2:
        a = [(-128, 127, 0)[i % 3] for i in range(m * k)]
        b = [1 if i % 2 else -1 for i in range(n * k)]
    elif case == 3:
        a = [0] * (m * k)
    return a, b, initial


def run_function(sim, function, machine):
    """Interpret the emitted function, accepting only identity memref casts."""
    for op in function.blocks[0]:
        try:
            if sim.op_name(op.text) == "memref.cast":
                # canonicalize introduces identity layout casts. Buddy's
                # small interpreter lacks this op; preserve the descriptor
                # only after checking its complete shape/stride/type.
                match = re.fullmatch(r"(%\S+) = memref.cast (%\S+) : (.+) to (.+)", op.text)
                if match is None:
                    raise ValueError("Unsupported model memref.cast syntax")
                descriptor = machine.values[match[2]]
                target = sim.parse_type(match[4])
                layout = re.search(r"strided<\[(\d+),\s*(\d+)\]>", match[4])
                if layout is None:
                    raise ValueError("Expected static rank-2 layout in model cast")
                target_strides = [int(layout[1]), int(layout[2])]
                if (
                    list(descriptor.sizes) != list(target.shape)
                    or list(descriptor.strides) != target_strides
                    or descriptor.element != target.element
                ):
                    raise ValueError("Model only supports identity descriptor casts")
                machine.values[match[1]] = descriptor
            else:
                sim.run_op(op, machine, None)
        except sim._ReturnSignal:
            break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("module", type=Path)
    parser.add_argument("--tools", required=True, type=Path)
    parser.add_argument("--shape", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.tools.resolve()))
    import boscame_sim as sim

    shape = json.loads(args.shape.read_text())
    m, n, k = (shape[key] for key in ("M", "N", "K"))
    function = next(op for op in sim.read_module(args.module) if sim.op_name(op.text) == "func.func" and "@bosc_gemm(" in op.text)
    records = []
    guard = bytes([0xA5]) * 64
    for case in range(4):
        a, b, initial = test_inputs(m, n, k, case)
        expected = [initial[i * n + j] + sum(a[i * k + r] * b[j * k + r] for r in range(k)) for i in range(m) for j in range(n)]
        payloads = [bytes(value & 255 for value in a), bytes(value & 255 for value in b), struct.pack(f"<{m * n}i", *initial)]
        buffers = [bytearray(guard + data + guard) for data in payloads]
        machine = sim.Machine()
        # Direct hardware AME loads use the interpreter's common read accessor.
        # Set its read policy for that accessor; this does not model CPU caches.
        machine.ddr_policy = "read"
        for index, (data, dims, strides, dtype) in enumerate(
            zip(buffers, [(m, k), (n, k), (m, n)], [(k, 1), (k, 1), (n, 1)], ["i8", "i8", "i32"])
        ):
            name = f"%arg{index}"
            machine.ddr[name] = data
            machine.values[name] = sim.MemRef(data, 64, dims, strides, dtype, None, name)
        run_function(sim, function, machine)
        actual = list(struct.unpack(f"<{m * n}i", buffers[2][64:-64]))
        if actual != expected:
            raise AssertionError(f"BOSCAME model mismatch in case {case}")
        for index in (0, 1):
            if buffers[index] != guard + payloads[index] + guard:
                raise AssertionError(f"Input or guard modified in case {case}, operand {index}")
        if buffers[2][:64] != guard or buffers[2][-64:] != guard:
            raise AssertionError(f"Output guard modified in case {case}")
        if any(access["start"] < 64 or access["end"] > len(machine.ddr[access["buffer"]]) - 64 for access in machine.accesses):
            raise AssertionError("Modeled access outside payload")
        records.append(
            {
                "case": case,
                "elements_checked": m * n,
                "accesses": len(machine.accesses),
                "output_sha256": hashlib.sha256(buffers[2][64:-64]).hexdigest(),
                "status": "passed",
            }
        )
    report = {
        "status": "passed",
        "kind": "BOSCAME IR functional model",
        "hardware_run": False,
        "module_sha256": hashlib.sha256(args.module.read_bytes()).hexdigest(),
        "model_setup": "ranked entry descriptors; checked identity memref.cast; direct hardware reads via read accessor",
        "cases": records,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"BOSCAME IR functional model: PASS ({len(records)} cases, {len(records) * m * n} outputs; hardware not run)")


if __name__ == "__main__":
    main()
