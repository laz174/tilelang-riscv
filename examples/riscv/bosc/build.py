#!/usr/bin/env python3
"""Build and validate the TileLang INT8 GEMM firmware; never submit to FPGA."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

from compiler.pipeline import check_firmware, check_runtime, link_firmware, lower_kernel
from compiler.support import Steps, record_environment, sha256, snapshot_sources, toolchain, write_json

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
REPORTS = ("model-check.json", "nr-isa-audit.json", "firmware-check.json", "uart-check.json")
PRODUCTS = ("bosc_gemm.bin", "bosc_gemm.elf", "commands.log", *REPORTS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="New/empty output directory; default: bosc/build/<timestamp>")
    parser.add_argument("--dump", action="store_true", help="Keep intermediate IR, per-pass dumps, sources and step logs")
    args = parser.parse_args()
    out = (args.output or HERE / "build" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")).expanduser().resolve()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        parser.error(f"Output must be new/empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "status": "started",
        "dump": args.dump,
        "hardware_run": "not run",
        "target_profile": "nr-fpga",
        "python": sys.executable,
        "semantics": "C += A @ B.T, signed i8 x i8 -> i32",
        "grid": [1, 1, 1],
        "scheduler_protocol": "UART v1 START/RESULT, emitted by NH",
    }
    write_json(out / "manifest.json", manifest)
    work = out if args.dump else Path(tempfile.mkdtemp(prefix=".build-", dir=out))
    steps = Steps(work)
    try:
        buddy, llvm_bin, buddy_bin, patch_tool = toolchain(REPO)
        record_environment(steps, manifest, REPO, buddy, llvm_bin, buddy_bin)
        sources = snapshot_sources(HERE, work, buddy)
        check_runtime(steps, work, sources, llvm_bin, patch_tool)
        lower_kernel(steps, work, sources, buddy_bin, dump=args.dump)
        link_firmware(steps, work, sources, llvm_bin)
        check_firmware(steps, work, sources, llvm_bin)
        binary = work / "bosc_gemm.bin"
        manifest["status"] = "success"
        manifest["binary_sha256"] = sha256(binary)
        manifest["binary_bytes"] = binary.stat().st_size
        manifest["validation"] = {name: json.loads((work / name).read_text()) for name in REPORTS}
        if not args.dump:
            for name in PRODUCTS:
                shutil.copy2(work / name, out / name)
            shutil.rmtree(work)
    except Exception:
        manifest["status"] = "failed"
        error = traceback.format_exc()
        (out / "error.log").write_text(error)
        print(error, file=sys.stderr)
        print(f"Failed build files retained: {work}", file=sys.stderr)
    finally:
        manifest["steps"] = steps.records
        manifest["files"] = {
            str(path.relative_to(out)): {"sha256": sha256(path), "bytes": path.stat().st_size}
            for path in sorted(out.rglob("*"))
            if path.is_file() and path.name != "manifest.json" and "__pycache__" not in path.parts
        }
        write_json(out / "manifest.json", manifest)
    print(f"Output: {out}")
    print(f"Status: {manifest['status']}; FPGA execution: not run")
    return 0 if manifest["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
