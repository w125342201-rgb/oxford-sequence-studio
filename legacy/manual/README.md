# TeslatronPT · Python 仪器控制接口

**TeslatronPT 低温系统**的 VTI / 探针 / 励磁磁铁，以及 **Keithley 6221、
2182A、Stanford Research SR830** 三台仪器的 Python 控制接口。

所有 SCPI / 串口指令依据仪器官方手册整理，并在工控机上核对过实际通信行为。

---

## 1. 硬件拓扑与通信方式

| 设备 | 控制器 / 接口 | 资源字符串 | 协议 |
|------|--------------|------------|------|
| VTI 浴温 + 交换气气压 | Mercury iTC-VTI | `TCPIP0::192.168.1.113::7020::SOCKET` | Oxford Mercury `READ:DEV:` / `SET:DEV:` |
| 探针温度 + 精细控温 | Mercury iTC-Probe | `TCPIP0::192.168.1.114::7020::SOCKET` | Oxford Mercury `READ:DEV:` / `SET:DEV:` |
| 励磁超导磁体 | Mercury iPS（组 `GRPZ`） | `TCPIP0::192.168.1.115::7020::SOCKET` | Oxford Mercury `READ:DEV:` / `SET:DEV:` |
| Keithley 6221 电流源 | GPIB / USB | `GPIB0::12::INSTR`（或 Keithley USB） | SCPI |
| Keithley 2182A 纳伏表 | GPIB / USB | `GPIB0::3::INSTR`（或 Keithley USB） | SCPI |
| SRS SR830 锁放 | GPIB | `GPIB0::8::INSTR` | SRS 4-字符指令 |

> 温控为**两台独立的 Mercury iTC**（iTC-VTI 192.168.1.113、iTC-Probe 192.168.1.114）
> + 一台 iPS（192.168.1.115），均走 7020 端口，可用 `*IDN?` 回读确认。
> 物理背景：样品杆插在 VTI 中，VTI 提供背景温度与交换气气压，probe 温控仪
> 精细控温；样品温度以 probe（DB8.T1）为准。联动控温规则：probe TSET=T，
> VTI TSET=T−Δ，Δ=1.0 K（T≥10 K）/ 0.2 K（T<10 K）。
> GPIB 地址为 `GPIB0::12::INSTR`（6221）、`GPIB0::3::INSTR`（2182A），
> Keithley 设备也可用 USB 资源字符串。

### Mercury 协议（VTI / 探针 / 励磁）
- 读：`READ:DEV:<模块>.<通道>:<块>:SIG:<字段>` → 回 `STAT:DEV:...:<数值><单位>:VALID`
- 写：`SET:DEV:<模块>.<通道>:<块>:<子块>:<字段> <值>` → 回 `STAT:SET:DEV:...:VALID`
- 行结束符 `\n`；纯 TCP socket（端口 7020），无需 NI-VISA。

---

## 2. 模块 → 指令映射

### 励磁磁铁（组 `GRPZ`，位于 192.168.1.115）
| 功能 | 指令 |
|------|------|
| 置目标电流 | `SET:DEV:GRPZ:PSU:SIG:CSET:<A>` |
| 置电流扫速(A/min) | `SET:DEV:GRPZ:PSU:SIG:RCST:<A/min>` |
| 置磁场扫速(T/min) | `SET:DEV:GRPZ:PSU:SIG:RFST:<T/min>`（协议对称推断） |
| 开关加热励磁（persistent heater） | `SET:DEV:GRPZ:PSU:SIG:SWHT:ON\|OFF` |
| 保持 | `SET:DEV:GRPZ:PSU:ACTN:HOLD` |
| 扫到设定值 | `SET:DEV:GRPZ:PSU:ACTN:RTOS` |
| 读持久电流 | `READ:DEV:GRPZ:PSU:SIG:PCUR` |

其它可用信号（出自 MercuryiPS 手册 p.113）：`CURR`(电流)、`FLD`(场)、
`FSET`(目标场)、`RFST`(T/min 磁场扫速)、`PFLD`(持久场)、`ACTN:RTOZ`(扫零)、
`ACTN:CLMP`(钳位)、`ACTN:PAUS`(暂停)。

> **磁场-电流对应关系（校准）存储在 iPS 本机内**（前面板 / Mercury Configuration
> 软件配置），`FSET`（T）与 `RFST`（T/min）只**使用**该内部校准换算，本工程
> 任何命令都**不写入**校准参数（无 CAL 动词、无磁体系数 SET）。

### VTI / 探针温度、压力（iTC-VTI 192.168.1.113 + iTC-Probe 192.168.1.114）
| 功能 | 指令 | 位置 |
|------|------|------|
| VTI 温度 | `READ:DEV:DB6.T1:TEMP:SIG:TEMP` | iTC-VTI (192.168.1.113) |
| 探针温度 | `READ:DEV:DB8.T1:TEMP:SIG:TEMP` | iTC-Probe (192.168.1.114) |
| 探针控温设定 | `SET:DEV:DB8.T1:TEMP:LOOP:TSET:<K>` | iTC-Probe (192.168.1.114) |
| VTI 控温设定 | `SET:DEV:DB6.T1:TEMP:LOOP:TSET:<K>` | iTC-VTI (192.168.1.113) |
| 变温速率 | `SET:DEV:<模块>.<通道>:TEMP:LOOP:RSET:<K/min>` | 两 iTC 各自回路 |
| 斜坡开关 | `SET:DEV:<模块>.<通道>:TEMP:LOOP:RENA:ON\|OFF` | 两 iTC 各自回路 |
| 压力 | `READ:DEV:DB3.P1:PRES:SIG:PRES` | iTC-VTI (192.168.1.113) |
| 压力设定 | `SET:DEV:DB3.P1:TEMP:LOOP:TSET:<mbar>` | iTC-VTI (192.168.1.113)（针阀控温回路挂在 TEMP 块，PRES 仅回读） |

> 模块名（DB8/DB6/DB3）已在实机上核实；
> 变温速率 RENA 为控制回路级、只能远程设置（Mercury iTC 手册 §4.8）。

### Keithley 6221（电流源）
- 设直流电流：`:SOUR:CURR <A>`（`%.15f` 精度）
- 输出开/关：`OUTPUT ON` / `OUTPUT OFF`
- 自动量程：`:SOUR:CURR:RANG:AUTO ON`
- AC 波形：`SOUR:WAVE:FUNC SIN` / `:FREQ` / `:AMPL` / `:OFFS 0` /
  `:PMAR:STAT ON` / `:PMAR 0` / `:DUR:TIME INF` / `:ARM` / `:INIT`
- Delta 模式（配合 2182A）：`:SOUR:DELT:HIGH/LOW/DELay/COUNt/NVPResent?/ARM/ARM?`、
  `:SOUR:SWEep:COUNt`、`:SYST:COMM:SER:SEND "(2182 cmd)"`（经由 6221 RS-232 链路指挥 2182A）、
  `:INIT:IMM`、`:ABOR`

### Keithley 2182A（纳伏表）
- 取数：`:FETC?`
- 配置/读数：`:CONF:VOLT`、`:SENS:CHAN`、`:SENS:VOLT:NPLC`、`:SENS:VOLT:RANG[:AUTO]`、
  `:READ?`、`:MEAS:VOLT?`

### SRS SR830（锁放）
- 取数：`OUTR? 1|2`（读显示值）、`SNAP? 1,2,3,4`（X,Y,R,θ 同采）
- 参考/相位：`FREQ`、`PHAS`、`FMOD`(谐波)、`RSLP`(斜率)
- 输入/滤波：`ISRC`、`ICPL`、`ILIN`
- 增益/时间常数：`SENS`、`RRES`、`TCON`、`OFLT`、`SYNC`
- 自动：`AGAN`/`ARSV`/`APHS`；接口：`OUTX`、`LOCL`

---

## 3. 安装

```bash
pip install -r requirements.txt
# 若没有 NI-VISA，安装纯 Python 后端：
pip install pyvisa-py
```

Mercury（VTI/探针/励磁）走纯 TCP socket，**不需要** VISA 后端。
6221 / 2182A / SR830 走 GPIB/USB，需要 VISA 后端（NI-VISA 或 pyvisa-py + 相应 GPIB 适配器）。

---

## 4. 快速开始

```python
from oxford.oxford_teslatronpt_api import TeslatronPT
from sr830.sr830_fixed import SR830
import pyvisa

# 温度 + 励磁（无需 VISA）
cryo = TeslatronPT(itc_vti_host="192.168.1.113", itc_probe_host="192.168.1.114",
                   ips_host="192.168.1.115")
r = cryo.readings()
print(r.vti_temperature_k)    # VTI 温度 (K)
print(r.probe_temperature_k)  # 探针温度 (K)
print(r.pressure_mbar)        # 压力 (mbar)
cryo.set_field(2.0, ramp_rate_t_per_min=0.1)   # 励磁扫到 2 T（FSET+RTOS）
print(cryo.field(), "T")
cryo.close()

# 锁放
rm = pyvisa.ResourceManager()
lia = SR830("GPIB0::8::INSTR", rm)
x, y, r, th = lia.read_all()
print(x, y, r, th)
lia.close(); rm.close()
```

更完整的示例见 `examples.py`（含 Delta 四引线测阻、锁放随场扫描等）。

---

## 5. 目录结构

本工程按设备拆分为 3 个独立驱动模块（各含 README），外加一个独立 GUI：

```
OXFORD_python_interface/
├── oxford/
│   ├── oxford_teslatronpt_api.py        # 牛津温控/励磁（Mercury 协议，纯 TCP socket，无 VISA）
│   └── oxford_teslatronpt_api_README.txt
├── keithely6221_2182A/
│   ├── keithley_6221_2182a_driver.py    # Keithley 6221 + 2182A（SCPI / GPIB·USB，需 pyvisa）
│   └── keithley_6221_2182a_driver_README.txt
├── sr830/
│   ├── sr830_fixed.py                   # SRS SR830 锁放（SRS 指令 / GPIB·USB，需 pyvisa）
│   └── sr830_fixed_README.txt
├── GUI/
│   ├── teslatronpt_gui_v2.py            # 独立 Tk 控制界面（内嵌 Mercury 传输，自包含）
│   ├── teslatronpt_gui_v2_README.txt
│   └── gui_preview.html
├── measureGUI/                          # 三个独立测量程序（互不 import）
│   ├── teslatronpt_iv_gui.py            # I-V（仅 DC 模式，2026-08-29 定论）
│   ├── teslatronpt_rt_gui.py            # R-T（STEP/SWEEP × 离散磁场；DC/DELTA；矩阵批量）
│   ├── teslatronpt_mr_gui.py            # R-B 磁阻（离散温度 × 分段磁场；DC/DELTA）
│   ├── teslatronpt_{iv,rt,mr}_gui_README.txt
│   ├── matrix_batch_measurement.md      # R-T 矩阵批量测量设计说明
│   └── 代码核查与优化建议报告.md
├── matrix/
│   ├── matrix_switch.py                 # M6x24 矩阵开关驱动（GPIB/RS232/simulate，线程安全）
│   ├── test_matrix_switch.py            # 驱动单元测试（29 项）
│   └── README.md
├── examples.py             # 使用示例（Delta 四引线测阻、锁放随场扫描等）
├── requirements.txt
├── DEPLOY.md               # 工控机部署指南
├── CHANGELOG.md            # 变更归档（2026-08-29 起）
└── README.md
```

> 三个测量程序（`measureGUI/`）为独立 Tk 应用，与根包约定一致：只依赖
> `oxford/`、`keithely6221_2182A/`、`matrix/` API 包，**互不 import**；
> 运行方式见 `DEPLOY.md` 第 8 节。

导入方式（需将本仓库目录加入 `sys.path`；包名 = 目录名，例如目录为
`OXFORD_python_interface` 时用 `import OXFORD_python_interface as p`）：

```python
from oxford.oxford_teslatronpt_api import TeslatronPT
from keithely6221_2182A.keithley_6221_2182a_driver import Keithley6221, Keithley2182A
from sr830.sr830_fixed import SR830
# 也可整体导入： import python_interface as p; p.TeslatronPT ...
```

> GUI 为独立 Tk 应用（`python GUI/teslatronpt_gui_v2.py`），内嵌自己的 Mercury
> 传输层，不依赖本包；三套驱动模块各自可单独 import。

> 说明：模块名（DB8/DB6/DB3）与三台控制器地址（iTC-VTI 192.168.1.113 /
> iTC-Probe 192.168.1.114 / iPS 192.168.1.115）均已在实机上核实。
