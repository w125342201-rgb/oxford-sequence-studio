# M6×24 矩阵开关 — Python 驱动接口

基于《矩阵开关命令.txt》《矩阵开关使用说明.pdf》以及实测确认，
将 Multi-field M6×24 矩阵开关的控制逻辑整理为 Python 接口。

> 接口类型：**GPIB**（VISA 资源名，如 `GPIB0::23::INSTR`）。RS232（USB‑DB9）作为可选兼容后端。

## 1. 硬件与协议速览

| 项目 | 参数 |
|------|------|
| 设备 | Multi-field Coaxial Matrix 6×24（前面板铭牌：Coaxial Matrix - 6X24） |
| 拓扑 | 6 路同轴输出（行）× 24 路输入（列） |
| 输出 BNC | 前面板 OUTPUT 区，A B C D E F 自上而下 |
| 输入 | 后面板 26 芯多芯端子（1–24 信号，25/26 备用） |
| 状态指示 | 前面板 6×24 继电器点阵（行 A-F，列 1-24），与 `state_matrix()` 布局一致 |
| **通信接口** | **GPIB**（VISA，如 `GPIB0::<addr>::INSTR`）；可选 RS232 115200/8/N/1 |
| 回读 | 无（设备只接收命令帧） |

**命令帧（以 CRLF 结尾）：**

```
[nnnnnnnn,nnnnnnnn,nnnnnnnn]\r\n
```

- 三段，每段 8 位，字符取值 `0`–`6`
- 段 1/2/3 分别描述输入列 **1–8 / 9–16 / 17–24**
- 第 *i* 位（1‑indexed）的数字 = 该输入端所连的输出行号；`0` = 断开
- 命令为**整帧绝对状态**：每次下发都是 24 列的完整快照，而非增量

文档示例对照（已写入单元测试，严格通过）：

| 语义 | 帧 |
|------|----|
| 断开所有 | `[00000000,00000000,00000000]` |
| 第一行第一列 | `[10000000,00000000,00000000]` |
| 第二行第四列 + 第三行第五列 | `[00023000,00000000,00000000]` |

## 2. 文件结构

```
python_driver/
├── matrix_switch.py        # 驱动本体（纯函数层 + 设备层 + 实验辅助）
├── test_matrix_switch.py   # 离线测试套件（23 项，无需硬件）
└── README.md               # 本文件
```

## 3. 依赖

```bash
pip install pyvisa          # GPIB 模式（需系统已装 NI-VISA 后端）
# RS232 兼容后端才需要：
pip install pyserial
```

（本机 Python 3.13 已具备 `pyvisa 1.16.2`，且系统装有 NI‑VISA 后端 `visa32.dll`；`pyserial 3.5` 亦在。）

## 4. 快速上手（模拟模式，无硬件）

```python
from matrix_switch import MatrixSwitch

sw = MatrixSwitch(simulate=True)
sw.connect("A", 1)        # 输出 A(行1) ← 输入 1
sw.connect(3, 5)          # 输出 C(行3) ← 输入 5
print(sw.last_frame)      # b'[10003000,00000000,00000000]\r\n'
print(sw.get_mapping())   # {1: 1, 5: 3}
sw.disconnect(1)
sw.open_all()             # 全部断开
```

## 5. 真实硬件（GPIB）

```python
from matrix_switch import MatrixSwitch, list_visa_resources

print(list_visa_resources())                 # 先查到 GPIB 资源名
sw = MatrixSwitch(resource="GPIB0::23::INSTR", timeout=2.0)
with sw.open():                              # 上下文管理器，退出自动关闭
    sw.connect("A", 1)
    sw.route_4wire(1)                        # 四端法：输入 1–4 → 输出 1–4
    sw.open_all()
```

若你的设备实际走 RS232，可指定 `backend="serial", port="COM3"`：

```python
sw = MatrixSwitch(backend="serial", port="COM3")
```

若通信无响应，可尝试去掉终止符：`terminator=b"\n"`。

## 6. API 速查

| 方法 | 说明 |
|------|------|
| `open()` / `close()` | 建立/关闭连接（模拟模式跳过） |
| `connect(row, col)` | 闭合 输出行↔输入列（row 可传 1–6 或 'A'–'X'） |
| `disconnect(col)` | 断开指定输入列 |
| `open_all()` | 全部断开（发送全 0 帧） |
| `set_mapping({col:row})` | 批量设置；`open_others=True` 时未列列清零 |
| `get_mapping()` | 返回非空连接 `列→行` |
| `state_matrix()` | 返回 6×24 布尔矩阵 |
| `route_4wire(col, rows=(1,2,3,4))` | 四端法路由，连续 4 列接 4 个输出 |
| `route_samples({id:起始列})` | 多样品切换（轮询用） |
| `last_frame` | 最近一次下发帧字节串（便于核对） |
| `build_frame(state)` / `parse_frame(text)` | 纯函数，帧构造/解析，可离线调用 |
| `list_visa_resources()` / `discover_resource()` | 查询/推荐 GPIB 资源名 |

构造参数：`resource`（VISA 名）、`port`、`backend='visa'|'serial'`、`baudrate`、`timeout`、`terminator`、`allow_row_sharing`、`simulate`。

## 7. 操作对应关系

| 原控制程序 | Python |
|---------|--------|
| 初始化（VISA Configure / GPIB） | `MatrixSwitch(resource="GPIB0::23::INSTR")` |
| 前面板 Boolean Array | `MatrixSwitch._state` 状态缓存 |
| 程序框图 `%.0f` 格式化 + `[` `,` `]` 拼接 | `build_frame()` |
| `VISA Write` + `write buffer` | `_send()` → `instr.write(...)`（GPIB） |

## 8. 注意事项

1. **整帧语义**：设备不维护增量状态。本驱动内部缓存 `_state`，任何单通道操作都会重新下发整帧，避免“切一个通道把别的冲掉”。
2. **GPIB 资源名**：地址需按设备实际 GPIB 主地址填写（例如 `GPIB0::23::INSTR`）。先用 `list_visa_resources()` 确认。
3. **短路保护**：默认 `allow_row_sharing=False`，禁止同一输出行同时连多列（样品间意外短路）。确有必要（如复用仪表端）时显式开启。
4. **同一列唯一连接**：一个输入列同一时刻只能连一个输出行，再连其他行会抛 `MatrixError`。
5. **设备无回读**：无法从硬件读回当前连接状态，状态以本地缓存为准；上电后建议先 `open_all()` 同步。
6. **终止符**：默认 `\r\n`，与文档一致；个别固件差异可用 `terminator` 调整。
