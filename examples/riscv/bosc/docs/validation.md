# 验证记录

[返回入口](../README.md) · [开发指南](development.md)

## FPGA 实机结果：2026-11-10

用户提供的串口和任务摘要确认以下两次运行，设备均为 `local/FPGA6`，时间
保留客户端的 `CST` 标记：

| 项目 | BOSC GEMM | 原始 Hello |
| --- | --- | --- |
| Task ID | `4fc45daf3f314c26941bcc74ff164556` | `f70f03f536724403af4303d47f3271c7` |
| Started | 2026-11-10 11:45:40 | 2026-11-10 11:45:58 |
| Finished | 2026-11-10 11:45:42 | 2026-11-10 11:46:00 |
| Duration | 1.23 s | 1.00 s |
| State / Execution | `completed` / `exited` | `completed` / `exited` |
| App / Platform exit code | `0` / `0` | `0` / `0` |
| Reset confirmed | `yes` | `yes` |

GEMM 关键日志节选：

```text
@@FPGA_START {"version":1}
[tilelang-bosc] case=0x00000000 errors=0x00000000
[tilelang-bosc] case=0x00000001 errors=0x00000000
[tilelang-bosc] case=0x00000002 errors=0x00000000
[tilelang-bosc] case=0x00000003 errors=0x00000000
TileLang BOSC GEMM: PASS
[nr] launch cycles=0x00000000000E0D0E status=0x00000000
[nr] RA returned: PASS
verify NR runtime: PASS
@@FPGA_RESULT {"version":1,"status":"exited","app_exit_code":0}
```

四组检查共 1024 个 INT32 输出，覆盖普通乘法、非零 C 累加、INT8 边界和零
输入，同时检查输入保持和前后保护区。随后 Hello 打印
`computed: 40 + 2 = 0000002A`、`verify hello: PASS` 并正常结束。

这份记录证明本次成功路径的数值自检、UART 握手、调度完成、复位确认及
GEMM → Hello 同板切换。重复 GEMM、压力测试和异常/取消/超时后的复用尚未
覆盖；服务常驻模式仍只声明 `nh-only`，不由本次观测扩展到任意 NH/RA/DMA。
失败、日志溢出和 NH trap 的协议路径目前来自宿主测试。

`Duration` 是任务耗时；`launch cycles` 包含初始化、参考计算、四次 kernel
和校验，两者都不作为单次 GEMM 性能。用户日志未附镜像 SHA256，以下本地
构建身份和用户运行证据分别保留来源。

## 本地构建检查

构建自动检查以下内容，结果和文件 SHA256 保存在 `manifest.json`。
完整的中间文件、步骤日志和 `sources/` 源码快照使用 `--dump` 保留：

- 软件模型：四组 / 1024 个输出，输入保持、保护区和访问范围通过。
- UART 宿主检查：成功、失败、日志溢出、NH trap 的 START/RESULT 通过。
- 固件审计：ELF/bin section 一致，入口 `0x80000000`，实际调用链、buffer
  对齐/不别名和 ISA/fence 检查通过；kernel 含 21 条合法 NR AME 指令。

历史构建身份如下。UART v1 和目录重组版的 bin、kernel.o、launch.o 逐字节
一致；协议改动前后 kernel.o、launch.o 也逐字节一致。

| 构建 | bin 字节数 | bin SHA256 |
| --- | --- | --- |
| 初始版，2026-11-07 | 74,369 | `9434956cd89e65368f17bf58bf260fa703e64c0a3df33f1fc38732707197d700` |
| UART v1，2026-11-10 | 75,265 | `eb2410cb33d8cb539c4255ac2e2ea76c34a5a1a89b628b9f82cb7568796a3364` |

软件模型检查算术和访存，不模拟 CPU 缓存、启动或 FPGA 时序。本例只使用 NR
runtime；NH/E6、分块、多 tile、SPM/DMA 路线需要单独验证。早期 probe 和
失败现场仅留在本地 `archive/`、`experiments/`，不作为构建或验收前提。
