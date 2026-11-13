# TileLang INT8 GEMM → NR FPGA

本例通过 Buddy BOSC AME 后端编译 `C += A @ B.T`，生成包含启动代码、NR
runtime 和数值自检的 FPGA 固件。A/B 为 `int8[16,64]`，C 为
`int32[16,16]`；一个 workgroup、一个完整 tile，NH 启动、RA 执行。

## 构建

需要已启用 MLIR 的 TileLang Python 环境、匹配的 Buddy/LLVM，以及 GNU
`patch`。本例独立构建，无需先运行其他 RISC-V 示例。从仓库根目录执行：

```bash
conda activate opt
(
  source examples/riscv/bosc/env.sh
  python examples/riscv/bosc/build.py
)
```

括号将环境设置限制在本次构建。`env.sh` 默认使用相邻 `buddy-mlir` 的
`llvm/build` 和 `build-24`（后者不存在时使用 `build`）。自定义安装位置可在
source 前设置 `BOSC_BUDDY_SOURCE`、`TILELANG_RISCV_LLVM_ROOT`、
`TILELANG_RISCV_BUDDY_ROOT`。

默认输出到 `build/<时间戳>/`；`--output <新目录或空目录>` 可指定位置。
构建自动执行软件模型、UART 和固件检查，默认保留 `bosc_gemm.elf/bin`、
四份检查报告、`commands.log` 和 `manifest.json`。添加 `--dump` 才保留中间
IR、每个 pass 的 dump、汇编、目标文件、源码快照和详细步骤日志：

```bash
python examples/riscv/bosc/build.py --dump --output /tmp/bosc-dump
```

关闭 dump 时仍执行全部检查；成功后清理本次临时文件，失败时保留输出目录内
的 `.build-*` 工作目录和 `error.log` 供排查。

## 上板

将 bin 复制到已配置的 FPGA 客户端，执行：

```bash
fpga-run ./test/bosc_gemm.bin --timeout 600
```

成功时四组 `errors=0`、GEMM 和 runtime 均报告 PASS，UART 首尾包含：

```text
@@FPGA_START {"version":1}
……启动、计算和校验日志……
@@FPGA_RESULT {"version":1,"status":"exited","app_exit_code":0}
```

任务应为 `completed`、`Execution: exited`，应用/平台退出码均为 `0`，
`Reset confirmed: yes`，客户端返回终端。只有 PASS 日志不能完成调度任务。

## 代码入口

先读 [src/kernel.py](src/kernel.py) 的计算，再读 [src/launch.c](src/launch.c)
的输入、descriptor 和独立参考检查。[build.py](build.py) 按四个阶段组织构建：

```text
runtime 协议检查 → TileLang 导出/布局适配/AME 编译 → 链接固件 → 固件审计
```

实际工具命令在 [compiler/pipeline.py](compiler/pipeline.py)，布局适配在
[compiler/ir.py](compiler/ir.py)。[开发指南](docs/development.md) 说明 ABI、
UART 协议、新算子适配和独立复查命令。

2026-11-10 的 FPGA6 日志确认四组数值自检、UART 握手、任务完成和复位通过，
随后同板 Hello 也通过，详见[验证记录](docs/validation.md)。这些结果仅覆盖
本例静态单 tile INT8 GEMM；其他 shape/dtype、分块或 DMA 路线需另行验收。
