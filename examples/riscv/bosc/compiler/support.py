"""Persist each explicit compiler command and both output streams."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Steps:
    def __init__(self, output):
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.records = []

    def run(self, name, argv, *, stdin=None, stdout=None, check=True):
        argv = [os.fspath(value) for value in argv]
        directory = self.output / "steps" / f"{len(self.records) + 1:02d}-{name}"
        directory.mkdir(parents=True)
        record = {"name": name, "argv": argv, "cwd": str(Path.cwd()), "status": "running", "directory": str(directory)}
        self.records.append(record)
        command = shlex.join(argv)
        if stdin is not None:
            record["stdin"] = str(Path(stdin).resolve())
            command += " < " + shlex.quote(record["stdin"])
        if stdout is not None:
            record["stdout"] = str(Path(stdout).resolve())
            command += " > " + shlex.quote(record["stdout"])
        with (self.output / "commands.log").open("a") as log:
            log.write(f"[{name}] {command}\n\n")
        print(f"[{name}] {command}", flush=True)
        start = time.monotonic()
        try:
            proc = subprocess.run(
                argv, input=Path(stdin).read_text() if stdin is not None else None, text=True, capture_output=True, check=False
            )
            (directory / "stdout.log").write_text(proc.stdout)
            (directory / "stderr.log").write_text(proc.stderr)
            if stdout is not None:
                Path(stdout).write_text(proc.stdout)
            record["returncode"] = proc.returncode
            record["status"] = "success" if proc.returncode == 0 else "failed"
            if check and proc.returncode:
                raise RuntimeError(f"Step {name} failed ({proc.returncode}); see {directory / 'stderr.log'}\n{proc.stderr[-5000:]}")
            return proc
        except OSError as error:
            record["status"] = "launch_error"
            record["error"] = str(error)
            raise
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - start, 6)
            write_json(self.output / "steps.json", self.records)


def toolchain(repo):
    buddy = Path(os.environ.get("BOSC_BUDDY_SOURCE", repo.parent / "buddy-mlir")).resolve()
    llvm_bin = Path(os.environ["TILELANG_RISCV_LLVM_ROOT"]) / "bin"
    buddy_bin = Path(os.environ["TILELANG_RISCV_BUDDY_ROOT"]) / "bin"
    required = [buddy_bin / name for name in ("buddy-opt", "buddy-translate", "buddy-llc")]
    required += [llvm_bin / name for name in ("clang", "ld.lld", "llvm-objcopy", "llvm-objdump", "llvm-readelf", "llvm-nm")]
    patch_tool = shutil.which("patch")
    if patch_tool is None:
        raise RuntimeError("GNU patch is required for the example-local NR UART v1 adapter")
    for tool in required:
        if not tool.is_file() or not os.access(tool, os.X_OK):
            raise RuntimeError(f"Missing executable {tool}; source {repo / 'examples/riscv/bosc/env.sh'} first")
    return buddy, llvm_bin, buddy_bin, patch_tool


def record_environment(steps, manifest, repo, buddy, llvm_bin, buddy_bin):
    manifest["environment"] = {
        key: os.environ[key]
        for key in (
            "BOSC_BUDDY_SOURCE",
            "TILELANG_RISCV_LLVM_ROOT",
            "TILELANG_RISCV_BUDDY_ROOT",
            "TILELANG_RISCV_OPT",
            "LD_LIBRARY_PATH",
        )
        if key in os.environ
    }
    for name, directory in (("tilelang", repo), ("buddy", buddy), ("llvm", buddy / "llvm")):
        result = steps.run(f"revision-{name}", ["git", "-C", directory, "rev-parse", "HEAD"])
        manifest[f"{name}_revision"] = result.stdout.strip()
    for tool in (buddy_bin / "buddy-opt", buddy_bin / "buddy-llc", llvm_bin / "clang"):
        steps.run(f"version-{tool.name}", [tool, "--version"])
    steps.run("python-packages", [sys.executable, "-m", "pip", "freeze"], stdout=steps.output / "host-packages.txt")


def snapshot_sources(example, out, buddy):
    """Preserve the categorized source tree without copying generated outputs."""
    source = out / "sources"
    own = source / "example"
    own.mkdir(parents=True)
    for name in ("build.py", "env.sh", "README.md"):
        shutil.copy2(example / name, own / name)
    for name in ("src", "compiler", "runtime", "checks", "docs"):
        shutil.copytree(example / name, own / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    nr = source / "buddy/nr"
    nr.mkdir(parents=True)
    for path in (buddy / "runtime/boscame/nr").iterdir():
        if path.is_file():
            shutil.copy2(path, nr / path.name)
    shutil.copytree(buddy / "runtime/boscame/uart", source / "buddy/uart")
    tools = source / "buddy/tools"
    tools.mkdir()
    for name in ("ame_to_word.py", "restrict_fpga_assembly.py", "nr_isa.py", "check_nr_elf.py", "boscame_sim.py"):
        shutil.copy2(buddy / "examples/BOSCAMEFPGA/tools" / name, tools / name)
    return source
