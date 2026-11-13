"""Four build stages: runtime checks, kernel lowering, firmware linking, audit."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def check_runtime(steps, out, sources, llvm_bin, patch_tool):
    """Patch a runtime copy and exercise all four UART completion paths."""
    nr = sources / "buddy/nr"
    runtime = sources / "example/runtime"
    checks = sources / "example/checks"
    steps.run(
        "adapt-nr-uart-v1",
        [
            patch_tool,
            "--batch",
            "--forward",
            "--fuzz=0",
            "--no-backup-if-mismatch",
            "-p1",
            "-d",
            nr,
            "-i",
            runtime / "nr-uart-v1.patch",
        ],
    )
    host_check = out / "uart-host-check"
    steps.run(
        "compile-uart-host-check",
        [
            llvm_bin / "clang",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            f"-I{nr}",
            f"-I{runtime}",
            checks / "uart.c",
            "-o",
            host_check,
        ],
    )
    for case in ("pass", "fail", "overflow", "trap"):
        steps.run(f"uart-host-{case}", [host_check, case], stdout=out / f"uart-{case}.log")
    steps.run("validate-uart-v1", [sys.executable, checks / "uart.py", out])


def lower_kernel(steps, out, sources, buddy_bin, *, dump=False):
    """Export TileLang, adapt the ABI/layout, then lower and encode AME."""
    tools = sources / "buddy/tools"
    compiler = sources / "example/compiler"
    checks = sources / "example/checks"
    opt = [buddy_bin / "buddy-opt", "--mlir-disable-threading"]
    if dump:
        opt += ["--mlir-print-ir-after-all", "--mlir-print-ir-module-scope"]
    translate, llc = buddy_bin / "buddy-translate", buddy_bin / "buddy-llc"

    def optimize(name, source, result, *passes):
        steps.run(name, [*opt, out / source, *passes, "-o", out / result])

    export = [sys.executable, compiler / "ir.py", "export", "--output", out]
    if dump:
        export.append("--dump")
    steps.run("tilelang-export", export)
    optimize("canonicalize", "02-tilelang.mlir", "03-canonical.mlir", "--canonicalize", "--cse")
    steps.run(
        "adapt-layout-and-abi",
        [
            sys.executable,
            compiler / "ir.py",
            "adapt",
            out / "03-canonical.mlir",
            "--shape",
            out / "shape.json",
            "--output",
            out / "04-adapted.mlir",
        ],
    )
    optimize("contract", "04-adapted.mlir", "05-contract.mlir", "--kernel-contract-annotate", "--kernel-contract-verify")
    optimize("plan", "05-contract.mlir", "06-planned.mlir", "--bosc-ame-plan=plan-report=true", "--bosc-ame-plan-verify")
    optimize("emit-boscame", "06-planned.mlir", "07-boscame.mlir", "--bosc-ame-emit")
    optimize("canonicalize-for-model", "07-boscame.mlir", "07-model.mlir", "--canonicalize")
    steps.run(
        "ir-functional-model",
        [
            sys.executable,
            checks / "model.py",
            out / "07-model.mlir",
            "--tools",
            tools,
            "--shape",
            out / "shape.json",
            "--output",
            out / "model-check.json",
        ],
    )
    optimize("lower-ame", "07-boscame.mlir", "08-llvm.mlir", "--lower-bosc-ame")
    steps.run("translate", [translate, out / "08-llvm.mlir", "--buddy-to-llvmir", "-o", out / "09-kernel.ll"])
    steps.run(
        "ame-assembly",
        [
            llc,
            out / "09-kernel.ll",
            "-mtriple=riscv64-unknown-elf",
            "-mattr=+m,+a,+f,+d,+c,-v,+xboscame",
            "-target-abi=lp64d",
            "-relocation-model=static",
            "-code-model=medium",
            "-O=2",
            "-filetype=asm",
            "-o",
            out / "10-kernel.s",
        ],
    )
    assembly = (out / "10-kernel.s").read_text()
    for mnemonic in ("mlae8.m", "mlbe8.m", "mqma.b.mm", "msce32.m"):
        if mnemonic not in assembly:
            raise RuntimeError(f"Actual AME instruction missing: {mnemonic}")
    steps.run("encode-nr-ame", [sys.executable, tools / "ame_to_word.py"], stdin=out / "10-kernel.s", stdout=out / "11-kernel.words.s")
    steps.run(
        "restrict-nr-assembly",
        [sys.executable, tools / "restrict_fpga_assembly.py", "--reject-vector-ordered-reductions", "--reject-vector-max-reductions"],
        stdin=out / "11-kernel.words.s",
        stdout=out / "12-kernel.nr.s",
    )


def link_firmware(steps, out, sources, llvm_bin):
    """Link the generated kernel, C caller and NR runtime into a boot image."""
    nr = sources / "buddy/nr"
    runtime = sources / "example/runtime"
    shape = json.loads((out / "shape.json").read_text())
    (out / "kernel_config.h").write_text("#pragma once\n" + "".join(f"#define BOSC_{key} {shape[key]}\n" for key in ("M", "N", "K")))
    clang = llvm_bin / "clang"
    cflags = [
        "-v",
        "--target=riscv64-unknown-elf",
        "-march=rv64gc_zicbom",
        "-mabi=lp64d",
        "-mcmodel=medany",
        "-ffreestanding",
        "-fno-builtin",
        "-fno-pie",
        "-fno-stack-protector",
        "-fno-vectorize",
        "-fno-slp-vectorize",
        "-ffp-contract=off",
        "-ffunction-sections",
        "-fdata-sections",
        "-O2",
        f"-I{nr}",
        f"-I{nr.parent / 'uart'}",
        f"-I{out}",
        f"-I{runtime}",
    ]
    inputs = [("kernel", out / "12-kernel.nr.s"), ("launch", sources / "example/src/launch.c")]
    for filename in ("crt.S", "nr_runtime.c", "nr_memory.c", "nr_math.c", "ame_sync.c", "nr_copy.S"):
        inputs.append((Path(filename).stem, nr / filename))
    objects = []
    for name, source in inputs:
        obj = out / f"{name}.o"
        # nr_copy.S is the handwritten RVV runtime routine.
        flags = cflags + ["-march=rv64gcv"] if name == "nr_copy" else cflags
        steps.run(f"compile-{name}", [clang, *flags, "-c", source, "-o", obj])
        objects.append(obj)
    elf, binary = out / "bosc_gemm.elf", out / "bosc_gemm.bin"
    steps.run(
        "link-firmware",
        [
            llvm_bin / "ld.lld",
            "-m",
            "elf64lriscv",
            "-T",
            nr / "nr.ld",
            "--gc-sections",
            "--no-undefined",
            f"-Map={out / 'firmware.map'}",
            "-o",
            elf,
            *objects,
        ],
    )
    steps.run("raw-binary", [llvm_bin / "llvm-objcopy", "-O", "binary", elf, binary])


def check_firmware(steps, out, sources, llvm_bin):
    """Audit the linked image, including the actual kernel and call chain."""
    elf = out / "bosc_gemm.elf"
    tools = sources / "buddy/tools"
    steps.run("elf-info", [llvm_bin / "llvm-readelf", "-h", "-l", "-S", "-A", "-s", elf], stdout=out / "elf-info.txt")
    steps.run("disassemble", [llvm_bin / "llvm-objdump", "-d", "--mattr=+v,+zicbom", elf], stdout=out / "firmware.disasm.txt")
    steps.run("defined-symbols", [llvm_bin / "llvm-nm", "-n", "-S", "--defined-only", elf], stdout=out / "symbols.txt")
    undefined = steps.run("undefined-symbols", [llvm_bin / "llvm-nm", "-u", elf], stdout=out / "undefined-symbols.txt")
    if undefined.stdout.strip():
        raise RuntimeError("Firmware has undefined symbols")
    steps.run(
        "nr-isa-audit",
        [
            sys.executable,
            tools / "check_nr_elf.py",
            elf,
            "--objdump",
            llvm_bin / "llvm-objdump",
            "--require-direct-store",
            "--output",
            out / "nr-isa-audit.json",
        ],
    )
    steps.run("firmware-structure-check", [sys.executable, sources / "example/checks/firmware.py", out])
