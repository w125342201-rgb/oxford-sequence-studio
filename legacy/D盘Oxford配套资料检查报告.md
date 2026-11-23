# D:\Oxford 随机配套资料检查报告

检查时间：2026-09-03。资料齐全度很高，且**直接解决了昨天调研中缺失的两块拼图**：你这台 iPS 的出厂速率限制表（比我网上找的任何手册都权威）和 Issue 20 正式手册原文。

---

## 一、资料清单与定位

### PT/（TeslatronPT 主系统）
| 文件/文件夹 | 内容 | 与本项目关系 |
|---|---|---|
| **Mercury iPS manual - Issue 20.pdf** | UMC0071, 2018-07, 178 页 | ⭐ 核心手册，比昨天网上下载的 2012 版 UMC0072 新 6 年，指令参考/速率限制章节已全部提取 |
| Mercury iTC manual - Issue 18.pdf | iTC 手册 | 温控参考 |
| TeslatronPT System Manual CN0647 rev 1.pdf + Site Planning Guide + D.O.C. + pss-teslatron | 系统手册/选址/符合性声明 | 系统级参考 |
| **J1119_IPS_FACTORY_USB/** | **你这台 iPS 的出厂配置 USB 备份** | ⭐⭐ 最关键，见第二节 |
| J1119 Test Results.pdf | 出厂测试结果 | 存档 |
| X178493 / X178496 / X178499 / X196688 | LakeShore CX-1050-CU-HT 温度计校准证书（.curve 电阻-温度表 + 证书 PDF） | 温度计标定溯源 |
| Cryomech PT415 / Sumitomo F70 压缩机手册、practical cryogenics、safety matters（含中文版） | 制冷机与安全 | 参考 |

### VT/（HelioxVT ³He 插入件）
- HelioxVT Manual Issue 5、FACTORY_USB（iTC 出厂配置）、X208806 校准证书、FACTORY RESULTS.pdf。
- `test/` 文件夹是 2023-12 安装验收截图（base temperature、He3 各温点、14T 磁体测试、IVC 抽真空前后、52h 保持时间），`test/1222` 里的通道映射确认 **Probe→DB8.T1、VTI→MB1.Temperature、压力→DB5**（与原控制程序映射一致）。
- OXSOFT IDK 1.5.4.43 安装包（安装密码已移除）。手册 p124 明确写着官方 Teslatron (OxSoft) 软件包含 **MercuryiPS driver 源码**——原控制程序“设场失败计数”逻辑的源头大概率就在这个安装包里，可解包查证。

---

## 二、出厂配置解码（J1119 iPS，firmware 2.5.09.000）

### 1. 磁体基本参数（magcfg_tables/Z axis）
| 参数 | 出厂值 | 含义 |
|---|---|---|
| conversion_factor (ATOB) | **8.4237 A/T** | 118.9317 A ÷ 8.4237 ≈ **14.12 T**，确认 14 T 磁体 |
| current_limit | 118.9317 A（PSU.M1+PSU.M2 并联，各 59.4659 A） | 双电源并联输出 |
| inductance | 48.3 H | 控制环阻尼用 |
| voltage_limit / transient | 4.0 V / 3000 ms | 失超检测阈值（手册建议典型 500–3000 ms） |
| **safety_sensor / TLIM** | **MB1.T1 / 6.00 K** | ⭐ 磁体温度安全联锁，见下 |
| switch heater | 21 mA（无液浸泡真空系统典型值 20–25 mA） | 吻合 |
| current_epsilon | 0.1 A | 到达带，见下 |

### 2. 速率限制表（Z axis_rrlimits.cfg）——你要找的"设场限制"本体
```
1: LRMZ(导线速率,加热器OFF)  0–120 A   60 A/min
2: LRMF(导线速率,带场)       0–120 A   60 A/min
3: MRFa(磁体速率,快档)       0–120 A    2 A/min  = 0.2374 T/min
4: MRSl(磁体速率,慢档)       0–120 A    1 A/min  = 0.1187 T/min
```
**你一直在用的 0.237 T/min 正是这台磁体出厂设定的"快档磁体速率极限" 2 A/min**（÷8.4237 = 0.2374）。不是随意选的安全数，就是厂家给这台磁体定的官方磁体励磁速率上限。加热器 OFF 时走导线速率（60 A/min，此时只充电源不充磁体）。

机制（手册 §5.1–5.2）：
- 速率限制是**强制钳速**：请求超限不会拒绝，而是按限速跑，面板 "Rate Limit" 指示灯亮。
- **前面板 Fast/Slow 按钮决定用 MRFa 还是 MRSl 表**。若面板当前在 Slow 档，请求 0.237 T/min 会被钳到 0.119 T/min——表现为"扫得比预期慢一倍"而不是报错。

### 3. 官方明说的"设场不执行/必须等待"机制（手册原文出处）
1. **安全联锁**（§4.4.10–4.4.11）：MB1.T1 > 6.00 K 时 **iPS 拒绝起扫**（"the iPS group will not ramp up"）；已带场则进入自动慢速 rundown。降温/升温过程中若磁体温度贴着 6 K，RTOS 会静默不执行。→ 可远程 `READ:DEV:MB1.T1:TEMP:SIG:TEMP` 排查。
2. **CLMP 钳位态**（§4.1/§5）：失超后约 20 s 自动钳位；钳位态下 RTOS 无效，须先 HOLD（"unclamp … crow-bar relay(s) open"——**物理继电器动作，这就是"必须等一会儿"的官方机制之一**）。
3. **switch heater 合闸检查**（§11.4.3.2）：H1 只在 |I_magnet − I_psu| < 0.1 A 时才执行；且 SCPI 信号表里 `SIG SWHT` 官方备注就是 **"check before set"**。
4. **权限**（§11.3.7）：用户等级 GUEST/NORM/ENG 不够时 SET 返回 **DENIED**。
5. **Remote/Local**（§10.5）：Remote Access 未选中对应接口（虚拟按钮 I/U/G/E）时远程指令无效。
6. **fine trim**（§4.4.20）：斜坡末端进入 ±0.1 A（≈±0.0119 T）到达带后自动回 HOLD + fine trim（每 100 ms 微调 20 µA）——GUI 的到达判定容差 ≥0.012 T 不会误等。

### 4. 对 GUI 有直接用处的发现
- **遗留 `X` 状态命令**（§11.4.2.3）：一条指令返回 13 字符全状态——活动态(Hold/ToSet/ToZero/Clamp)、本地/远程、加热器状态、**Fast/Slow 档**、是否扫场中。比逐条 SCPI 查询高效得多，适合作为每次设场前的状态快照。
- cryosys.cfg：`comms.protocol=SCPI`、port 7020、`general.short_form_polling=ON`、RS232 115200。
- 速率限制表整段连续（0–120 A 全覆盖），不存在手册警告的"空洞默认 0.1 A/min"问题。

---

## 三、结论与待办

1. **0.237 T/min 的出处已从猜测升级为出厂配置实证**（MRFa = 2 A/min）。GUI 现有上限校验与出厂配置一致，无需改动。
2. 上次调研的**方案 A（set_field_verified：写→等→读回→不符有限重试）** 现在有官方依据加成：手册自己写着 SIG SWHT "check before set"、X 命令一行读全状态。如果你同意，我按方案 A 实现，并在设场前用 X 命令读一次活动态/Fast-Slow 档写进日志（只记录事实，不发警告）。
3. 下一步可选：解包 OXSOFT IDK 安装包，把官方 MercuryiPS 驱动的设场失败处理源码挖出来对照（原控制程序失败计数的原型）。
