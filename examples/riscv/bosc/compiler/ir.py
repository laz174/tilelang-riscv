#!/usr/bin/env python3
"""Export TileLang IR and adapt the supported transposed-B INT8 GEMM layout."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def export(out, *, dump=False):
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import tilelang
    from tilelang import tvm
    from kernel import K, M, N, bosc_gemm

    if dump:
        (out / "00-input.tir.py").write_text(bosc_gemm.script())
    instruments = [tvm.ir.instrument.DumpIR(dump_dir=out / "tir-passes")] if dump else []
    with tvm.transform.PassContext(opt_level=3, instruments=instruments):
        artifact = tilelang.lower(bosc_gemm, target="riscv")
    if dump:
        (out / "01-lowered.tir.py").write_text(artifact.device_mod.script())
    (out / "02-tilelang.mlir").write_text(artifact.kernel_source)
    (out / "shape.json").write_text(json.dumps({"M": M, "N": N, "K": K, "grid": [1, 1, 1], "transpose_B": True}, indent=2) + "\n")
    print(f"Exported TileLang Linalg to {out / '02-tilelang.mlir'}")


def adapt(source, shape):
    """Express B as a zero-copy [K,N] view and attach caller ABI promises."""
    m, n, k = (shape[key] for key in ("M", "N", "K"))
    a_type, b_type, c_type = f"memref<{m}x{k}xi8>", f"memref<{n}x{k}xi8>", f"memref<{m}x{n}xi32>"
    expected_maps = ["(d0,d1,d2)->(d0,d2)", "(d0,d1,d2)->(d1,d2)", "(d0,d1,d2)->(d0,d1)"]
    maps = {name: re.sub(r"\s+", "", body) for name, body in re.findall(r"^(#[\w]+) = affine_map<(.+)>$", source, re.MULTILINE)}
    pattern = (
        r"(?P<indent> +)linalg\.matmul indexing_maps = \[(?P<maps>[^\]]+)\] "
        rf"ins\((?P<a>%\w+), (?P<b>%\w+) : {re.escape(a_type)}, {re.escape(b_type)}\) "
        rf"outs\((?P<c>%\w+) : {re.escape(c_type)}\)"
    )
    matches = list(re.finditer(pattern, source))
    if len(matches) != 1 or source.count("func.func ") != 1 or source.count("linalg.matmul") != 1:
        raise ValueError("Expected exactly one canonicalized, direct-buffer TileLang transposed-B INT8 matmul")
    match = matches[0]
    if [maps.get(name.strip()) for name in match["maps"].split(",")] != expected_maps:
        raise ValueError("Unexpected indexing maps: this adapter only supports A[m,k] * B[n,k] -> C[m,n]")
    a, b, c = (match[key] for key in ("a", "b", "c"))
    signature = f"func.func @bosc_gemm({a}: {a_type}, {b}: {b_type}, {c}: {c_type})"
    entry = signature + " {"
    if source.count(entry) != 1 or source.count("module {") != 1:
        raise ValueError("Unexpected entry signature/module attributes")
    # Do not hide extra operations, nested scopes, calls, or offset arithmetic.
    body = source.split(entry, 1)[1].rsplit("}", 2)[0]
    if re.sub(r"\s+", "", body.replace(match[0], "", 1)) != "return":
        raise ValueError("Expected only matmul and return after canonicalization")
    if "%bosc_rhs_logical" in source:
        raise ValueError("Reserved adapter SSA name is already in use")
    indent = match["indent"]
    view_type = f"memref<{k}x{n}xi8, strided<[1, {k}], offset: 0>>"
    view = (
        f"{indent}%bosc_rhs_logical = memref.reinterpret_cast {b} to "
        f"offset: [0], sizes: [{k}, {n}], strides: [1, {k}] : {b_type} to {view_type}\n"
    )
    matmul = f"{indent}linalg.matmul ins({a}, %bosc_rhs_logical : {a_type}, {view_type}) outs({c} : {c_type})"
    result = source[: match.start()] + view + matmul + source[match.end() :]
    result = result.replace("module {", "module attributes {contract.abi = {args_aligned_bytes = 64 : i64, args_disjoint = true}} {", 1)
    result = result.replace(entry, signature + " attributes {llvm.emit_c_interface, contract.grid = array<i32: 1, 1, 1>} {", 1)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export_args = commands.add_parser("export", help="Export TIR, Linalg and shape from src/kernel.py")
    export_args.add_argument("--output", required=True, type=Path)
    export_args.add_argument("--dump", action="store_true", help="Dump input/lowered TIR and each TIR pass")
    adapt_args = commands.add_parser("adapt", help="Normalize B layout and declare the entry ABI")
    adapt_args.add_argument("input", type=Path)
    adapt_args.add_argument("--shape", required=True, type=Path)
    adapt_args.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "export":
        export(args.output.resolve(), dump=args.dump)
    else:
        shape = json.loads(args.shape.read_text())
        args.output.write_text(adapt(args.input.read_text(), shape))
        print("Preserved C += A @ B.T; 64-byte aligned, disjoint entry buffers; grid=[1,1,1]; C wrapper.")


if __name__ == "__main__":
    main()
