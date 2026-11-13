# 开发指南

[返回入口](../README.md) · [验证记录](validation.md)

## 1. 构建流程

[build.py](../build.py) 调用 [compiler/pipeline.py](../compiler/pipeline.py)
的四个阶段。所有源码修改（包括 UART 补丁）都应用到输出目录内的快照：

| 阶段 | 工作 | 主要产物 |
| --- | --- | --- |
| `check_runtime` | 应用协议补丁，检查成功、失败、日志溢出和 NH trap | `uart-*.log`、`uart-check.json` |
| `lower_kernel` | 导出 → 适配 → contract → plan → AME → LLVM → NR 编码 | 各阶段 IR/汇编、`model-check.json` |
| `link_firmware` | 编译 kernel、launch、CRT/runtime，链接并导出 raw 镜像 | `.o`、`bosc_gemm.elf/bin` |
| `check_firmware` | 检查 ISA、ELF/bin、调用链、buffer ABI 和协议字符串 | `nr-isa-audit.json`、`firmware-check.json` |

[compiler/support.py](../compiler/support.py) 负责工具路径、源码快照和命令日志。
默认保留最终 ELF/bin、四份检查报告、`commands.log` 和 `manifest.json`。
`--dump` 额外保留全部中间 IR/汇编、每个 pass 的 dump、`steps.json`、每步
stdout/stderr 和 `sources/`；默认模式成功后清理本次临时文件，失败则保留
`.build-*` 工作目录。所有检查在两个模式下都会执行。
本地 `archive/`、`experiments/` 和 `build/` 不纳入提交，也不是构建依赖。

## 2. 布局与 ABI

[src/kernel.py](../src/kernel.py) 保持 `C += A @ B.T` 的数学语义。
[compiler/ir.py](../compiler/ir.py) 检查完整函数、签名和 indexing maps，只接受
本例的直接 buffer INT8 matmul，再用零拷贝 view 将物理 `[N,K]` 的 B 表达为
逻辑 `[K,N]`、strides `[1,K]`。没有替换计算，也没有删除未支持的操作。

入口承诺 64 字节对齐、A/B/C 不别名、grid `[1,1,1]`。
`llvm.emit_c_interface` 生成 `_mlir_ciface_bosc_gemm`，接收三个 rank-2
memref descriptor 指针；RV64 下各为 56 字节：

```c
{ allocated_ptr, aligned_ptr, offset, sizes[2], strides[2] }
```

[src/launch.c](../src/launch.c) 提供独立对齐 buffer 和前后保护区；B 的调用参数
仍为物理 `[N,K]`、strides `[K,1]`。它用标量参考计算检查四组数据：普通乘法、
非零 C 累加、INT8 边界、零输入；同时检查输入保持和保护区。kernel 前后执行
`ame_fence()`，链接后再次检查实际地址与调用链。

## 3. UART 完成协议

NH 初始化 UART 后发送一次 START，再释放 RA。RA 完成 launch 和自检并返回；
NH 转发完日志、检查溢出后发送一次 RESULT，scheduler 才能收尾和复位。

[runtime/uart_protocol.h](../runtime/uart_protocol.h) 定义记录格式，
[runtime/nr-uart-v1.patch](../runtime/nr-uart-v1.patch) 将它接入 NR runtime。
补丁只修改快照，使用 `--fuzz=0`，上下文不匹配时构建失败。

- 每条记录独占一行，以 LF 或 CRLF 结束，无日志前缀、颜色或重复记录。
- 正常 RESULT 为 `status: "exited"`，成功退出码 `0`；数值错误、RA trap/abort
  或日志溢出退出码为 `1`。
- NH trap 使用 `status: "trap"`，携带十六进制 `mcause`、`mepc`、`mtval`，
  不带 `app_exit_code`。

[checks/uart.c](../checks/uart.c) 用真实 NR console reporter 和宿主 I/O stub
产生四种结果，[checks/uart.py](../checks/uart.py) 检查顺序、字段及 runtime
判据。新增 launch 只需返回状态，继续由 NH 发送最终 RESULT。

## 4. 新算子改哪些地方

在 `examples/riscv/` 下建立独立目录，复制本例的 `build.py`、`env.sh`、
`.gitignore`、`README.md` 及 `src/`、`compiler/`、`runtime/`、`checks/`、`docs/`。
先明确 shape、dtype、layout、覆盖还是累加，再逐项适配：

| 位置 | 需要修改 |
| --- | --- |
| `src/kernel.py` | 计算定义、shape 和调度 |
| `compiler/ir.py` | 导入/导出、shape 元数据、布局与 ABI 假设 |
| `src/launch.c` | wrapper、descriptor、输入、独立参考计算和全部检查 |
| `compiler/pipeline.py` | 配置头、产物名称及算子专属指令检查 |
| `checks/model.py` | 模型入口、类型/形状、参考公式和访问范围 |
| `checks/firmware.py` | 调用链、buffer 尺寸、ABI 和目标指令 |

保留 UART 与固件检查。整数精确比较，浮点明确容差与非有限值规则。
改变 rank 必须调整 descriptor；多 workgroup 需要真实调度入口，单改 grid
属性不会实现 launcher。主机张量传入、结果下载和 DMA 需要额外设计。

本地模型、UART 和最终固件检查通过后再上板，保存镜像 SHA256、manifest、
完整 UART 和任务摘要；验证任务完成、退出码、复位及同板任务切换。

## 5. 复查已有产物

对使用 `--dump` 保留的完整构建，可直接重跑检查器；`OUT` 指向该构建目录：

```bash
OUT="examples/riscv/bosc/build/<构建目录>"
python examples/riscv/bosc/checks/model.py "$OUT/07-model.mlir" \
  --tools "$OUT/sources/buddy/tools" --shape "$OUT/shape.json" \
  --output "$OUT/model-check.json"
python examples/riscv/bosc/checks/uart.py "$OUT"
python examples/riscv/bosc/checks/firmware.py "$OUT"
```

编译失败先看 `error.log`、`commands.log` 与失败步骤的 stderr。
无 START 时检查启动与 UART；有 PASS 无任务完成时检查 RESULT；数值错误时
检查 dtype、stride、累加初值和同步。通用 objdump 可能将 AME words 显示为
unknown，需结合 `10-kernel.s` 和最终 ELF 的 ISA audit 判断。
