#!/usr/bin/env python3
"""Check image layout, kernel reachability, real kernel AME words, and ABI data."""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path


UART_RECORDS = {
    "start": b'@@FPGA_START {"version":1}\r\n',
    "pass": b'@@FPGA_RESULT {"version":1,"status":"exited","app_exit_code":0}\r\n',
    "fail": b'@@FPGA_RESULT {"version":1,"status":"exited","app_exit_code":1}\r\n',
    "trap": b'@@FPGA_RESULT {"version":1,"status":"trap","mcause":"0x',
}
CALL_EDGES = [
    ("_start", "nr_nh_main"),
    ("_ra_start", "nr_ra_main"),
    ("nr_ra_main", "launch"),
    ("launch", "_mlir_ciface_bosc_gemm"),
    ("_mlir_ciface_bosc_gemm", "bosc_gemm"),
]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check_image(elf, binary):
    """Match raw image bytes to identity-mapped, allocated ELF sections."""
    require(elf[:6] == b"\x7fELF\x02\x01", "Expected little-endian ELF64")
    elf_type, machine = struct.unpack_from("<HH", elf, 16)
    require(elf_type == 2 and machine == 243, "Expected RISC-V ET_EXEC firmware")
    entry, phoff = struct.unpack_from("<QQ", elf, 24)
    require(entry == 0x80000000, "Unexpected NR NH entry")
    phentsize, phnum = struct.unpack_from("<HH", elf, 54)
    require(phentsize == 56, "Unexpected program header size")
    segments = [struct.unpack_from("<IIQQQQQQ", elf, phoff + index * phentsize) for index in range(phnum)]
    require(not any(segment[0] in {2, 3} for segment in segments), "Firmware requires a dynamic runtime/loader")
    loads = [segment for segment in segments if segment[0] == 1 and segment[5]]
    # objcopy serializes allocated PROGBITS sections, excluding segment padding.
    shoff = struct.unpack_from("<Q", elf, 40)[0]
    shentsize, shnum = struct.unpack_from("<HH", elf, 58)
    require(shentsize == 64 and shnum > 0, "Unexpected section table")
    sections = [struct.unpack_from("<IIQQQQIIQQ", elf, shoff + index * shentsize) for index in range(shnum)]
    allocated = [section for section in sections if section[2] & 2 and section[1] != 8 and section[5]]
    base = min(section[3] for section in allocated)
    end = max(section[3] + section[5] for section in allocated)
    require(base == entry and len(binary) == end - base, "Raw .bin load span does not match allocated ELF sections")
    for _, _, _, address, offset, size, *_ in allocated:
        require(
            binary[address - base : address - base + size] == elf[offset : offset + size], "Raw image differs from an allocated ELF section"
        )
        require(
            any(segment[3] <= address and address + size <= segment[3] + segment[5] and segment[3] == segment[4] for segment in loads),
            "An allocated section has no matching identity-mapped LOAD segment",
        )
    return entry, base


def read_symbols(path):
    symbols, sizes = {}, {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 3 and re.fullmatch(r"[0-9a-fA-F]+", parts[0]):
            symbols[parts[-1]] = int(parts[0], 16)
            if len(parts) == 4:
                sizes[parts[-1]] = int(parts[1], 16)
    return symbols, sizes


def check_calls(dump, symbols, sizes, entry):
    required = {name for edge in CALL_EDGES for name in edge}
    require(required <= symbols.keys(), "Firmware is missing boot/launch/kernel symbols")
    require(symbols["_start"] == entry, "ELF entry does not name _start")
    # Local labels can occur inside a function; use ELF symbol sizes as bounds.
    instructions = [(int(match[1], 16), match[2]) for match in re.finditer(r"^([0-9a-f]+):\s+[0-9a-f]+\s+(.+)$", dump, re.MULTILINE)]
    for caller, callee in CALL_EDGES:
        begin = symbols[caller]
        end = begin + sizes[caller]
        body = "\n".join(text for address, text in instructions if begin <= address < end)
        require(
            re.search(r"\b(?:jal|j|jalr|tail|call)\b[^\n]*<" + re.escape(callee) + r">", body),
            f"Missing linked call edge: {caller} -> {callee}",
        )


def check_buffers(shape, symbols):
    ranges = []
    for name, size in (
        ("input_a", shape["M"] * shape["K"]),
        ("input_b", shape["N"] * shape["K"]),
        ("output_c", shape["M"] * shape["N"] * 4),
    ):
        address = symbols[name] + 64  # Skip launch.c's prefix guard.
        require(address % 64 == 0, f"Entry alignment promise is false for {name}")
        ranges.append((address, address + size, name))
    for index, (begin, finish, name) in enumerate(ranges):
        require(
            all(finish <= other_begin or other_end <= begin for other_begin, other_end, _ in ranges[index + 1 :]),
            f"Entry noalias promise is false for {name}",
        )
    return [{"name": name, "start": hex(begin), "bytes": finish - begin} for begin, finish, name in ranges]


def check_kernel(kernel):
    from nr_isa import is_ame, validate_ame_word

    offset, words = 0, []
    while offset < len(kernel):
        half = int.from_bytes(kernel[offset : offset + 2], "little")
        width = 4 if half & 3 == 3 else 2
        word = int.from_bytes(kernel[offset : offset + width], "little")
        if width == 4 and is_ame(word):
            validate_ame_word(word)
            words.append(word)
        offset += width
    require(0x28180877 in words, "Linked TileLang kernel has no NR mqma.b.mm instruction")
    require(0x02B52077 in words, "Linked TileLang kernel has no NR accumulator store")
    return words


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    out = parser.parse_args().output.resolve()
    elf = (out / "bosc_gemm.elf").read_bytes()
    binary = (out / "bosc_gemm.bin").read_bytes()
    shape = json.loads((out / "shape.json").read_text())
    sys.path.insert(0, str(out / "sources/buddy/tools"))

    entry, base = check_image(elf, binary)
    symbols, sizes = read_symbols(out / "symbols.txt")
    check_calls((out / "firmware.disasm.txt").read_text(), symbols, sizes, entry)
    buffers = check_buffers(shape, symbols)
    kernel_offset = symbols["bosc_gemm"] - base
    words = check_kernel(binary[kernel_offset : kernel_offset + sizes["bosc_gemm"]])
    for name, record in UART_RECORDS.items():
        require(record in binary, f"Linked firmware is missing the UART v1 {name} record")

    report = {
        "status": "passed",
        "entry": hex(entry),
        "raw_base": hex(base),
        "raw_bytes": len(binary),
        "call_chain": [list(edge) for edge in CALL_EDGES],
        "entry_buffers": buffers,
        "kernel_ame_instructions": len(words),
        "kernel_ame_words": [hex(word) for word in words],
        "scheduler_protocol": {"version": 1, "records": list(UART_RECORDS)},
        "hardware_run": False,
    }
    (out / "firmware-check.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Firmware structure: PASS; entry={entry:#x}, bytes={len(binary)}, kernel AME instructions={len(words)}")
    print("RA boot -> launch -> MLIR C wrapper -> TileLang kernel is linked. Hardware was not run.")


if __name__ == "__main__":
    main()
