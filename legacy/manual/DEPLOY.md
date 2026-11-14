# 部署指南（工控机运行）

把本工程部署到**实际工控机**（装有 NI-VISA，三台 Oxford Mercury 控制器在线）
的完整步骤。核心结论：**温度/气压/励磁（oxford/ + GUI）是纯标准库 TCP socket，
连 NI-VISA 都不需要**；只有 Keithley / SR830 需要装 1 个库 `pyvisa`。

---

## 1. 环境要求

| 项目      | 要求                                 | 说明                                     |
| ------- | ---------------------------------- | -------------------------------------- |
| Python  | **≥ 3.10**（建议 3.12 / 3.13）         | GUI 用了 `float \| None` 语法，<3.10 直接报错   |
| tkinter | python.org 安装包自带                   | 无需另装                                   |
| pyvisa  | 仅 Keithley 6221 / 2182A / SR830 需要 | 工控机已有 NI-VISA，pyvisa 自动用它做后端           |
| 网络      | 工控机在 `192.168.1.x` 网段              | 能 ping 通 `192.168.1.10 / .114 / .115` |

### 依赖全景（已核实，无需其它）

| 模块                                                 | 依赖                | 备注                                  |
| -------------------------------------------------- | ----------------- | ----------------------------------- |
| `oxford/oxford_teslatronpt_api.py`（温度/气压/励磁）       | **纯标准库**          | `socket/time/re`，走 7020 TCP，不用 VISA |
| `GUI/teslatronpt_gui_v2.py`                        | **纯标准库**（tkinter） | 独立应用，不 import pyvisa                |
| `keithely6221_2182A/keithley_6221_2182a_driver.py` | `pyvisa`（1 个库）    | 用 NI-VISA 后端                        |
| `sr830/sr830_fixed.py`                             | `pyvisa`（1 个库）    | 用 NI-VISA 后端                        |
| `examples.py`                                      | `pyvisa`          | 同上                                  |

---

## 2. 安装步骤

1. **装 Python**：python.org 下载 Windows 安装包（3.12 或 3.13），
   安装时勾选 **Add python.exe to PATH**。

2. **（可选）装 pyvisa**（仅 Keithley/SR830 需要）：
   
   ```
   py -m pip install pyvisa
   ```
   
   > 不用装 `pyvisa-py`——工控机已有 NI-VISA。

3. **拷贝工程**：把整个 `python_interface` 文件夹拷到工控机
   （如 `C:\TeslatronPT\`），**保持目录结构不变**（`oxford/`、`keithely6221_2182A/`、
   `sr830/`、`GUI/` 同级，根 `__init__.py` 不动）。

---

## 3. 运行前检查

```bat
py --version              :: 确认 >= 3.10
ping 192.168.1.10        :: 三台都要通
ping 192.168.1.11
ping 192.168.1.12
```

Windows 防火墙：能连上 7020 说明出站通常已放行；若失败，
检查防火墙是否拦截到 7020 端口（TCP）的出站连接。

---

## 4. 冒烟测试（先不跑 GUI）

在工程根目录执行：

```bat
py -c "from oxford.oxford_teslatronpt_api import TeslatronPT; t=TeslatronPT(); print('VTI :', t.idn_vti()); print('PROBE:', t.idn_probe()); print('IPS :', t.idn_ips()); r=t.readings(); print('T_vti=', r.vti_temperature_k, ' T_probe=', r.probe_temperature_k, ' P=', r.pressure_mbar)"
```

预期：

- 三条 `IDN` 均回 `OXFORD INSTRUMENTS:MERCURY ...`；
- 温度/气压读数合理（数值+单位正常）。

若某行报 `INVALID` → 该控制器的**模块号不对**（不是地址问题），
核对前面板模块号后改 `oxford/oxford_teslatronpt_api.py` 顶部常量
（`VTI_MODULE` / `PROBE_MODULE` / `PRESSURE_MODULE`）或 GUI 顶部常量。

---

## 5. 运行 GUI

```bat
cd C:\TeslatronPT
py GUI\teslatronpt_gui_v2.py
```

- 顶部信息栏显示三台地址：`iTC-VTI 192.168.1.10` / `iTC-Probe 192.168.1.11` / `iPS 192.168.1.12`。
- 连接成功后 Log 区打印三台 IDN。
- **首次上机**：小磁场、小扫速（GUI 硬限制 ≤ 0.237 T/min ≈ 39.5 Oe/s）；
  先点 `Set temperature` 验证联动（probe TSET=T，VTI TSET=T−Δ），
  再在磁场输入 0.1~0.5 T 小目标测试 `Set field + RTOS` 与 `HOLD`。

---

## 6. 故障排查

| 现象                                        | 原因 / 处理                                                |
| ----------------------------------------- | ------------------------------------------------------ |
| `Connection failed: ...`                  | 网络不通 → ping；防火墙拦截 → 放行 7020 出站                         |
| 某条读数 `INVALID`                            | 该控制器模块号不对（地址已通）→ 核对 DB6/DB8/DB3                        |
| 设置后 Log 报 `FAILED: ... INVALID`           | 设定值超范围 / 动作冲突 → 减小设定值重试                                |
| `TypeError: unsupported operand...` 或语法错误 | Python < 3.10 → 升级                                     |
| GUI 弹窗不出现 / 黑窗                            | 缺 tkinter → 重装 python.org 官方版（勿用精简版）                   |
| 温度不联动（VTI 不动）                             | 检查 auto-pressure 勾选、10 K 跨越是否触发；或手动 `Apply ramp` 后重设温度 |

---

## 7. 硬件拓扑备忘（部署时对照）

| 控制器       | 地址                   | 职责                      | 模块                   |
| --------- | -------------------- | ----------------------- | -------------------- |
| iTC-VTI   | `192.168.1.10:7020` | VTI 浴温 + 交换气气压          | VTI=DB6.T1，气压=DB3.P1 |
| iTC-Probe | `192.168.1.11:7020` | 样品杆精细控温（样品温度以 probe 为准） | Probe=DB8.T1         |
| iPS       | `192.168.1.12:7020` | 励磁磁铁                    | 组 GRPZ               |

- 联动控温：`set_sample_temperature(T)` → probe TSET=T（114）、VTI TSET=T−Δ（113），
  Δ=1.0 K（T≥10 K）/ 0.2 K（T<10 K）。
- 变温速率：`SET:DEV:<模块>.<通道>:TEMP:LOOP:RSET:<K/min>` + `RENA:ON|OFF`（回路级、只能远程）。
- 磁场-电流校准**存储在 iPS 本机**（前面板/Configuration 配置），本工程只使用、不写入。

---

## 8. measureGUI 三个测量程序（I-V / R-T / MR）

独立 Tk 测量程序，位于 `measureGUI/`，互不 import，只依赖 `oxford/`、
`keithely6221_2182A/`、`matrix/` API 包。

```bat
cd C:\TeslatronPT
py measureGUI\teslatronpt_iv_gui.py     :: I-V 曲线（仅 DC）
py measureGUI\teslatronpt_rt_gui.py     :: R-T（STEP/SWEEP × 离散磁场；DC/DELTA；矩阵批量）
py measureGUI\teslatronpt_mr_gui.py     :: R-B 磁阻（离散温度 × 分段磁场；DC/DELTA）
```

- **依赖**：与第 1 节相同（tkinter；pyvisa 仅 Keithley 6221/2182A 需要）。
- **地址**：6221 / 2182A 经 GUI 下拉选择 GPIB（默认 `GPIB0::12::INSTR` /
  `GPIB0::3::INSTR`）；Oxford 三台走 7020 TCP 固定地址（`.113/.114/.115`）。
- **电学模式**：I-V 仅 DC（6221 输出全程常开 + Point settle + 2182A 直读）；
  R-T / MR 可选 DC 或 DELTA（DELTA 时 2182A 由 6221 串口链路控制，可留空地址）。
- **温控联动 / 自动气压 / 磁场硬限**（≤ 0.237 T/min）与主 GUI 完全一致。
- **矩阵批量**（仅 R-T）：启用后强制 DELTA + STEP 温度模式（SWEEP 被禁止），
  每个温度点稳定后按组合列表轮询全部引脚对，CSV 含 `pin_combo` 列。
- **数据落盘**：三程序均为**逐点实时追加** CSV（每点独立时间戳 + 实时
  温度/气压/磁场快照），STOP 或仪器异常不丢已采数据。
- 三个程序各自的字段说明见 `measureGUI/teslatronpt_*_gui_README.txt`；
  2026-08-29 以来的行为变更见 `CHANGELOG.md`。
