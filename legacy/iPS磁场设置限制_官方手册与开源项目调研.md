# iPS 磁场设置限制 — 官方手册与开源项目调研报告

日期：2026-09-03
背景：用户在独立 GUI 中复现"设场有时必须等一会儿才成功，频繁发指令没用"；原控制系统中专有"磁场设置失败次数"（重试计数）。本报告只列**有出处的证据**，不做无依据推断。

---

## 一、官方手册证据（Oxford Instruments iPS Handbook）

证据文件：`MercuryiPS Handbook 59-UMC0072 (Issue 01, Jan 2012)`（已存档 `.workbuddy/tmp/MercuryiPS_Handbook_59-UMC0072.pdf`）；
UMC0071 Issue 14 (Mar 2016) 章节经 ManualsLib/ManualsHive 检索核对。

### 1. 官方明确承认通信会偶发损坏，要求程序内置重试（§7.4.10, p.110）

> "the GPIB control program must assume that **data can be occasionally corrupted**."
> 所有读写指令必须内置 Timeout；超时后必须做故障恢复流程。

→ **官方白纸黑字要求**：控制程序必须假设指令/数据偶发丢失损坏，并设计超时+恢复。这就是 Oxford 自己软件体系里"设场失败重试计数"的文档根源。

### 2. 一问一答铁律：读完回执才能发下一条（§7.4.2, p.107）

> "If a command produces a response message, the controller must read the response **before sending a further command**."

iPS 是串行单线程处理：前一条命令的回执未被读走前发新命令 → 数据丢失/错乱。**密集连发指令只会持续失败**——与你"频繁发出指令并没有用"的复现完全吻合。

### 3. 最常见的指令失败原因：LOCAL 模式（§7.2.1, p.106；§10.4.1.1, p.125）

> "The most common reason for a command error is attempting to execute a command while the iPS is in **LOCAL mode**."

- **C0（Local and Locked）是 iPS 上电默认状态**。掉电重启后必须重新进入远程态，否则 SET 全部失败。
- 状态机：C0=Local+Locked（默认）/ C1=Remote+Locked / C2=Local+Unlocked / C3=Remote+Unlocked。

### 4. 前面板交互会暂存/延迟远程命令（§7.2, p.105；§10.4.1.1 C3, p.125）

> "If a GUI button is tapped when the command is received, the response **may be delayed until the button is released**."
> "If a GUI button is tapped in the C3 state, the instrument **does not respond to any remote commands until the button is released**. Instead these commands are **stored and performed when the button is released**. Computer programs should either be written to **tolerate this delay**..."

→ **"必须等一会儿才能设场成功"的官方解释之一**：iPS 触屏/前面板被占用时，命令被排队，松开后才执行。程序必须容忍该延迟，而不是连发。

### 5. SCPI 应答与错误表（§10.3, p.116；Table 10-1, p.125）

| 应答 | 含义 |
|---|---|
| `STAT:SET:...`（回显设定值） | SET 成功 |
| `<VERB>:INVALID` / `<VERB>:<NOUN>:INVALID` | 指令/名词无法解释 |
| `NOT_FOUND` | UID 不存在 |
| `N/A` | 该设备不支持此功能 |
| **`DENIED`** | **无权限修改该参数（权限/模式问题）** |

→ 每条 SET 指令**可能失败并带回执错误**；程序必须解析回执而不是只看 TCP 是否成功。

### 6. PSU 信号范围与语义（§10.3.5.2, p.119–120）

| 信号 | 范围/语义 |
|---|---|
| `FSET` | ±CLIM/ATOB |
| `RFST` | 0.00–50.00 T/min；**"Ramp rates are limited by the rate limit table."**（原文注） |
| `ACTN` | HOLD / RTOS / RTOZ / CLMP；**CLMP 仅当 CURR=0 时可用** |
| `SWHT` | [ON \| OFF]（if CURR=PCUR） |

### 7. 速率限制表会钳制请求速率（UMC0071 §5.1–5.3）

- iPS 预编程 4 类限速（LRMZ/LRMF/MRFa/MRSl），按电流分段；
- 请求速率超限 → **实际速率被钳到表值**（不是拒绝），并点亮 Home 界面 "Rate Limit" 指示；
- **限速表覆盖不全时，空洞区间默认 0.1 A/min，"will cause operational problems"**（原文）。
→ 若设场后"爬得极慢/像没动"，先查 iPS 触屏上的限速表与 Rate Limit 指示。

---

## 二、开源项目证据

### 1. QCoDeS `OxfordMercuryiPS`（微软官方收录驱动，同 `SET:DEV:GRPZ:PSU` 协议族、同 7020 端口）

- `ask()` 解析应答中的 `INVALID` → **证明 SET 可能返回 INVALID**；
- 源码 TODO 自认：*"we could use the opportunity to check that we did set/achieve the intended value"* → **设后不校验是已知欠账**；
- **CLAMP 态下发 RTOS 直接 raise**：*"Can not ramp to target value; power supply is clamped. **Unclamp first by setting ramp status to HOLD**."* → CLMP 态必须先 HOLD 再 RTOS，直接重发 RTOS 无效；
- **RFST==0 时拒绝励磁**：*"Can not ramp {worker}; ramp rate set to zero!"* → 与本项目已修的"零速率根因"完全一致（官方驱动也防这一条）；
- 阻塞等待范式：`while ramp_status()=="TO SET": sleep(0.1)`；示例用 `while mips.is_ramping(): sleep(1)`；
- VISA timeout 默认 **5 s** → 官方开源按"每条指令秒级耗时"设计，不是毫秒级。

### 2. ISIS（英国 STFC RAL 国家实验室）EPICS IPS 驱动

设场是一个**状态机**，每步都有等待与校验：

```
At Field → 等供电电压稳定(STABILITY_VOLTAGE) → 等 switch heater 加热完成(heater_ontime_ok)
→ Set PSU output → 校验 field_setpoint_readback == magnet_field && psu_field == magnet_field
→ 多处 timeout 300s 等待重试
```

- Troubleshooting 原文：*"Magnet won't go to field which has been set — Activity is **Clamped**"*；
- IOC 重启后恢复方式：*"simply **resend the setpoint**"*。

→ 国家实验室级驱动的做法：**写 → 等待 → 读回校验 → 不符重试**，而不是发完就当成功。

### 3. NI 论坛实测案例（Oxford IPS120 + 上位机重试，多轮验证的解法）

- 实测故障：*"The actual 'B rate' or 'B stop' received by the power supply is either the number we input, or **10 times smaller** than what we input, or **zero**"*（随机发生）；
- 社区验证修复：**写 setpoint → 等 2 s → 读回 → 比对（容差 0.01）→ 不一致则重写**，循环直到一致；并指出*"Read 前必须加 2 秒等待，否则读回值是错的"*。

→ 这正是原控制系统中"磁场设置失败次数"（有限次重试）的工程出处。

### 4. 其他旁证

- **Labber 官方驱动库**（Oxford Mercury iPS）：switch heater 变更后阻塞等待 `SwitchHeaterDelay`；"不等够就去驱磁体 **可能 quench**"。
- **Durham 大学物理系**：Oxford 官方 Mercury 驱动 *"has reliability issues... over many repeat readings the instrument **stops responding**"*。
- **Oxford OXSOFT 软件手册**（PSI 存档 TESLTRON.DOC）：官方 Teslatron 控制软件即基于"低层驱动 + 重试容错"结构。

---

## 三、对你复现现象的解释（全部有出处）

| 现象 | 官方解释 | 出处 |
|---|---|---|
| 频繁发指令没用 | 一问一答串行处理，回执未读走/上一条未完成时新命令被丢弃；连发只会持续失败 | §7.4.2；NI 论坛实测 |
| 必须等一会儿才成功 | ① 前面板/触屏占用时命令被**暂存**到松开为止；② 设定值在 iPS 内部下一控制周期才生效，立即读回看到旧值 | §7.2 p.105；§10.4.1.1 C3；QCoDeS TODO |
| 有时怎么发都不动 | ① LOCAL(C0 上电默认) → SET 全失败；② CLAMP 态 → RTOS 无效，须先 HOLD；③ `DENIED`（权限） | §7.2.1；§10.4.1.1；QCoDeS `_ramp_status_setter`；Table 10-1 |
| 场爬得极慢像没设上 | 限速表把请求速率钳到表值（且表有空洞时默认 0.1 A/min） | UMC0071 §5.1–5.3 |
| 偶发设定值错乱（如变 0） | 官方承认链路数据偶发损坏；IPS120 实测出现设定值被吞/缩小 10 倍/变 0 | §7.4.10；NI 论坛 |

**与既有根因修复的关系**：本项目已修的"零 RFST 复用"（强制显式非零速率）与 QCoDeS 的 RFST==0 检查一致，继续保留。当前 `set_field` = RFST→FSET→RTOS 三连发、逐条 `check_valid`（INVALID 会抛异常），发令语法与 v2 一致——语法层没有问题；缺的是**官方要求的传输层容错**：写后等待+读回校验+有限重试。

---

## 四、修复方案建议（待拍板，未改代码）

### 方案 A（推荐，完全对齐官方开源模式）：`set_field_verified`

在 `oxford_teslatronpt_api.py` 增加（不动现有 `set_field`，GUI 逐段调用）：

1. SET RFST → 等 `settle_s`(≈1 s) → 读回 RFST 比对（容差）；
2. SET FSET → 等 `settle_s` → 读回 FSET 比对；
3. 读 `ACTN`：若为 CLMP → 先发 HOLD（官方 QCoDeS 做法）再继续；
4. 发 RTOS → 等 `settle_s` → 读回 ACTN 确认已离开 HOLD；
5. 任一步不符 → **固定间隔等待后重试**（不连发），最多 `max_attempts`（即原控制程序的"设场失败次数"，建议 3–5 次）；每次失败写一条**记录日志**（第几次、期望 vs 读回），不是"警告等用户处理"。

### 方案 B（最小改动）

仅在 `set_field` 三条指令之间加 0.2–0.5 s 间隔，并在发完后读回 FSET/ACTN 各一次写日志（不自动重试）。风险最低，但对"指令被吞"场景的救援能力有限。

### 说明

用户此前"不要 verify/resend、不要猜电源"的裁决针对的是**无实测依据的防御补丁**；本次每一条均有官方手册/官方收录开源驱动背书，且与用户独立 GUI 的复现吻合。是否引入方案 A 由用户决定。

---

## 附：证据文件与链接

- MercuryiPS Handbook 59-UMC0072 Issue 01（完整 PDF，已存档）：`.workbuddy/tmp/MercuryiPS_Handbook_59-UMC0072.pdf`
  （原始链接：mymercurysupport.com `59_UMC0072_01.pdf`）
- MercuryiPS Operator's Manual UMC0071 Issue 14（Rate Limits §5.1–5.3、Remote Operation §9）：manualslib/manualshive 分页检索
- QCoDeS OxfordMercuryiPS 源码：microsoft.github.io/Qcodes `_modules/.../MercuryiPS_VISA.html`
- ISIS EPICS IPS 驱动文档：isiscomputinggroup.github.io/ibex_developers_manual `Oxford-Instruments-IPS.html`
- NI 论坛 IPS120 设定值错乱案例：forums.ni.com （DBL error / decimal point thread）
- Labber Oxford Mercury iPS 驱动：github.com/Labber-software/Drivers
- Oxford OXSOFT 手册（PSI 存档）：psi.ch `OXSOFT.pdf`
