# 变更日志（Changelog）

> 本文件按日期归档本仓库对**实验控制逻辑 / 测量程序 / 底层驱动**的实质性改动。
> 涉及文件、核验结论、修复明细、验证方式与现场待验证项均记录于此；
> 部署相关见 `DEPLOY.md`，接口说明见各模块 README 与 `README.md`。

---

## 2026-08-30 · 全程序包系统性审查修复（严重 5 / 中等 12 / 建议 15，共 32 项）

**范围**：全部 14 个 Python 源文件。审查报告与修复清单：`review_20260830/code_review_report.md`、
`review_20260830/fix_log_20260830.md`。三测量程序**互不 import** 的硬约定保持不变，
所有相似骨架改动均按 N 处同步执行（本轮同步点：`_spawn_connect` 参数快照、`_run` 运行参数快照、
等待超时、`_env_snapshot` 容错、日志截断、`_wait_poll_idle` 12 s、DC 单次配置、逐点重试）。

### 严重项修复（S1–S5）

| # | 问题 | 修复 |
|---|------|------|
| S1 | R-T/MR 运行中切换电测模式/Matrix → 重连并关闭在用仪器会话（竞态，6221 输出失控、磁体脱管） | 模式切换/Matrix 使能回调检测 `measurement_running`：运行中**回退控件状态并拒绝**；`_connecting` 重入保护；连接参数改为 `_spawn_connect` 在主线程快照后传入（顺带消除 worker 线程 Tk 访问） |
| S2 | I-V 的 STOP 不 HOLD 磁体（R-T/MR 均 HOLD） | I-V `stop_measurement` 应急线程补 `pt.hold()`，三程序 STOP 语义统一为"断电 + HOLD 磁体（+ 开矩阵）" |
| S3 | rotator `rotate_to(wait=True)` 全程持 RLock，急停无法抢占 | 等待阶段不再持锁（仅单次查询持锁）+ `_abort_event`：`stop_rotation` 先置位再取锁，等待循环下一拍即退出（mock 验证：抢占延迟 0.00 s） |
| S4 | 两控制 GUI 热耦合判定 `abs(diff)`，VTI 反向过热误判 OK | 改方向敏感判定：`diff<0`（VTI 比样品热）单独给出"反向失控"强告警 |
| S5 | SR830 `write_terminator/read_terminator` 属性名错误，终止符从未生效 | 改 `write_termination/read_termination` + 显式 `timeout_ms` |

### 中等项修复（M1–M12）

- **M1** 磁场上限常量化：`MAX_FIELD_T=20.0`（oxford API + 两个控制 GUI + 三测量程序统一引用；**现场待办：按本机磁体额定值下调**）。
- **M2** 三测量程序 T/B 稳定等待全部加超时兜底（温度 60 min 上限；磁场按 `|ΔB|/RFST×1.5+soak+120 s` 估算、下限 10 min、上限 60 min），超时抛 `TimeoutError` 并附最后读数。
- **M3** 每点环境快照改 `_env_snapshot()`：瞬时失败写 NaN 占位 + 日志告警，连续 10 次失败才中止（整夜测量不再因单次网络抖动中断）。
- **M4** `run_delta_once` 超时路径先 best-effort `abort()+disarm_delta()` 再抛错。
- **M5** 三测量程序启动时在主线程快照全部运行参数到 `self._run`，worker 只读快照（消除 worker 线程 Tk 访问 + 运行中改参数生效的隐患）。
- **M6** `MatrixSwitch.open(sync=True)`：连接建立后自动下发缓存帧对齐硬件（默认全 0 安全态）。
- **M7** `examples.py`：例 3 改走 6221 Trace Buffer + `R=VΔ/(2I)`（原 `R=2V/I` 与驱动约定矛盾，结果大 4 倍）；例 4 改 `field()` 轮询到位后读数（原固定 sleep 5 s 远小于实际扫场时间）；例 2/4 改用 `connect_vti=False, connect_probe=False` 磁体专用会话。
- **M8** rotator 等待超时估算改用 `|目标−当前位置|/速度`。
- **M9** rotator 超时异常消息明确"电机仍在运动"。
- **M10** Mercury `_readline` 改缓冲式读取（不再丢弃同行多余字节；oxford API + 两个 GUI 内嵌副本三处同步），重试逻辑注释明确仅服务幂等命令。
- **M11** `set_sample_temperature` 先校验 `vti_target` 再下发（杜绝 probe 已改、VTI 失败的部分应用）。
- **M12** 三份 Mercury 传输副本本轮已同步（缓冲读）；后续协议层改动继续按"三处同步"执行并记录于本文件。

### 建议项修复（O1–O15）

O1 R-T 删除重复的 `enforce_thermal_link` 调用；O2 R-T/MR DC 模式 2182A 配置移到 run 开始一次（原每点 `:CONF:VOLT`）；O3 逐点读数/Delta 加 1 次重试；O4 R-T `_sequence` 改 `for...else: raise`（与 I-V/MR 对齐，消除静默巨跳）；O5 CSV header 写入改 `with` 块；O6 `TeslatronPT` 支持 `connect_vti/probe/ips` 可选连接 + `_require` 守卫；O7 6221 驱动层 `set_current`/`setup_delta` 加 ±105 mA 限幅；O8 SR830 删重复 `set_filter_slope`、SENS/TCON 索引校验、RLock；O9 IV 每组合前复查温度、MR 每场点前漂移复查（>3·tol 且 >0.1 K 重新回稳）；O10 I-V 非连续段间改缓升过渡（不再瞬时跳变）；O11 `_wait_poll_idle` 3 s→12 s；O12 R-T 矩阵批量进度按组合计数；O13 rotator 先校验后下发；O14 控制 GUI 共享状态补注释；O15 全部 GUI 日志限长（600 行截断至 500 行）。

### 验证

- 全 15 个文件 `py_compile` 通过；`matrix/test_matrix_switch.py` 29 项单元测试通过；矩阵自检全过。
- oxford API 离线检查：缓冲 `_readline` 保留剩余字节、`parse_value` 两种响应格式、INVALID 捕获、ThermalRule、部分连接守卫、`vti_target` 预校验——全过。
- rotator mock 验证：急停 0.00 s 抢占等待中的旋转。
- SR830/Keithley/Matrix/Rotator 模块离线导入与结构断言全过。
- **现场待验证**：真实硬件上的 STOP/HOLD 行为、等待超时参数合理性、`MAX_FIELD_T` 按磁体额定值下调。

---

## 2026-08-29 · 三测量程序审查修复 + I-V 模式定论（三轮）

**范围**：`measureGUI/` 三个独立测量程序（I-V / R-T / MR）、`keithely6221_2182A/`、
`matrix/` 两个底层驱动；以及三份 GUI README、本文件。
三个测量程序**互不 import**（项目硬约定），所有相似骨架改动均按 N 处同步执行。

### 第一轮：并发 / 时序 / 温控审查（外部审查报告，8 项指控逐项核验）

核验结论：**7 项成立、1 项部分成立**；另发现 3 个报告未提及的同源缺陷一并修复。

| # | 指控 | 核验 | 修复落点 |
|---|------|------|---------|
| 1 | VISA/TCP 无互斥锁、poll 与测量并发 | 部分成立：Oxford TCP 实为每命令 RLock；真缺陷在 Keithley VISA 与 MatrixSwitch（STOP 与测量线程并发） | 6221/2182A 与 MatrixSwitch 全部会话/状态操作加 `threading.RLock`；三 GUI 加 `poll_busy` + `_wait_poll_idle` |
| 2a | I-V DELTA 负电流 HIGH<LOW | 成立（实为 `setup_delta` 直接抛 ValueError，整条 DELTA 扫描失败） | 本轮先改 `HIGH=\|I\|/LOW=-\|I\|` + `V(I)=sgn(I)·Vδ/2`；**随后被第二轮"移除 DELTA"取代** |
| 2b | R-T/MR DC 无 settle + 逐点开关输出 | 成立 | 新增 `dc_settle_var`（DC stl，默认 0.5 s）；输出由 worker 全程常开、finally 关断 |
| 2c | I-V DELTA 逐点 Arm 耗时 | 成立（约 1~3 s/点） | 先加 UI 提示；随后随"移除 DELTA"一并删除 |
| 3a | auto-pressure 迟滞状态循环内重置 | 成立（R-T 磁场×温度双层循环内、MR 温度循环内） | `regime=None` 移出循环（跨点延续 ±0.5 K 迟滞） |
| 3b | SWEEP 逐点 TSET 破坏连续 ramp | 成立；另发现 `±tol` 判定 1 s 轮询下**过冲挂死** | R-T SWEEP 改为每场次先到 start 再一次性下发 stop 连续 ramp；`_wait_temperature_reached`/`_wait_field_reached` 改**方向穿越判定** |
| 4 | Matrix + SWEEP 温度失配 | 成立 | Matrix 启用时**强制 DELTA+STEP**（UI 禁用 SWEEP 单选 + start 硬校验） |
| 5 | STOP 主线程阻塞 I/O | 成立（abort/hold/open_all 各可阻塞 10 s；Reset 已在线程） | R-T/MR `stop_measurement` 移入 daemon 线程；I-V 本就异步 |

额外修复（报告未提，同源缺陷）：
- R-T/MR 的 SWEEP 到达判定挂死风险（见 3b）。
- R-T 测量结束 finally 未关 6221 输出 → 补安全关断。

### 第二轮：I-V 仅保留 DC 模式（用户定论）

I-V 为单调电流扫描，**不需要也不应使用 DELTA 模式**（±I 交替为电阻/磁阻取向语义；
逐点 ARM 慢、需符号重建）。`teslatronpt_iv_gui.py` 全面移除 DELTA（10 处）：
- 删 `electrical_mode_var` / `delta_delay_var` / `_measure_delta_point` /
  `_on_electrical_mode_changed` / UI 电学模式单选行与 "Delay(s)" 参数 /
  `_reset_instruments` 的 2182A 串口复位分支 / `_connect_worker`、`start_measurement`、
  `_measurement_worker` 的模式分支。
- `_set_current_and_read` 仅 DC：`set_current → Point settle → dmm.read()`；
  6221 输出按 (T,B) 组合全程常开（conditioning 前开、回零后关）。
- 保留 NPLC（2182A 积分时间）；UI 模式行位置改为 DC-only 灰色提示。
- `teslatronpt_iv_gui_README.txt` 同步。

### 第三轮：一致性 re-check（全部通过）

`D:\AItest\recheck_consistency.py`（AST 静态校验，已沉淀为用户级 skill
`api-gui-consistency-recheck`）：
- 模式语义（I-V 仅 DC / R-T·MR 双模式）、GUI→API/驱动方法存在性（12 组）、
  线程安全基线（poll_busy / `_wait_poll_idle` / daemon STOP / `_poll_worker` 早退）、
  驱动 RLock、regime 循环外、CSV 表头与 README 一致。
- 初跑 1 项 FAIL 为**断言写错**（把 R-T 的 `_measure_dc` 命名套到 I-V），修正后 ALL PASS。

### 第四轮：5 项新指控（全部属实并修复）

| # | 指控 | 修复 |
|---|------|------|
| 1 | R-T `_sleep_stop_aware` 负 sleep 崩溃（deadline 越过后 `min(0.05, 负值)` → `ValueError`） | `time.sleep(max(0.0, min(0.05, ...)))` 下限截断（仅 R-T 有该方法） |
| 2 | I-V 内存集中缓存：STOP/异常丢全部数据；时间戳为落盘瞬间、环境量单次快照 | 新增 `_append_iv_point`（每点独立 `datetime.now()` + 实时 `pt.readings()` 快照，writer_lock 逐点追加）；删除 `_save_points` |
| 3 | `_wait_poll_idle()` 主线程同步阻塞（最长 3 s 冻结 UI） | 三程序统一移入 `_measurement_worker` 首行（worker 线程执行） |
| 4 | R-T 列冗余 + 缺名义设定温度（`temperature_K` 与 `probe_temperature_K` 同为实际读数） | `RTPoint.temperature_k`→`temperature_setpoint_k`；`_record_rt_point` 增 `target_t` 参数（3 处调用点传值）；CSV 表头 `temperature_K`→`temperature_setpoint_K`；日志显 `T_set`/`T_act` |
| 5 | I-V 分段接缝点重复测量（双分支回线共享端点测两次） | `_sweep_segments` 加 `abs(current − points[-1][1]) < 1e-12` 去重（对齐 MR 磁场扫描） |

### 验证

- 全部改动文件 `py_compile` 通过（managed Python 3.13）。
- 静态断言：`D:\AItest\recheck_consistency.py` 扩展至 7 节（含本轮修复语义），ALL PASS。
- 动态行为验证：`D:\AItest\verify_fixes_round2.py`（无硬件离线）——
  接缝去重 5 点、I-V 12 列实时写盘、R-T 设定温度列=target_t/实际列=读数、
  `_sleep_stop_aware` sleep 入参恒 ∈ [0, 0.05]。
- UI 实例化构建冒烟（`C:\Python314`，无头 withdraw）通过。

### 现场待验证（真实硬件）

1. I-V DC-only 全扫描（含负电流段，确认符号正确、输出全程常开时序）。
2. R-T SWEEP 连续斜坡实际温升曲线（每场次 start→stop 一次性下发）。
3. R-T 矩阵批量 STEP 模式下多通道轮询（SWEEP 已被禁止）。
4. DELTA 模式（R-T/MR）`HIGH=|I|/LOW=-|I|` 在各极性下的表现。

---

## 历史（2026-08-29 之前的沉淀）

- `DEPLOY.md`：工控机部署指南（环境、安装、冒烟、故障排查、硬件拓扑）。
- `README.md`：硬件拓扑与指令映射。
- `measureGUI/代码核查与优化建议报告.md`：早期静态审查报告（问题清单）。
- `measureGUI/matrix_batch_measurement.md`：R-T 矩阵批量测量设计说明。
- `matrix/test_matrix_switch.py`：矩阵开关驱动单元测试（29 项）。

### 早期审查报告（代码核查与优化建议报告.md）闭环对照（2026-08-29）

报告 26 个编号缺陷 + 复查遗留 3 项风险 + 6 条优化建议，对照当前代码：

| 条目 | 状态 | 说明 |
|------|------|------|
| 1 import 扁平名 | ✅ 闭环 | 包限定 import + sys.path 垫片，无本地副本 |
| 2 electrical_mode_var 缺失 | ✅ 闭环（方案演进） | 原为补变量+单选；2026-08-29 I-V 移除 DELTA 后直接删除（仅 DC） |
| 3 I-V DC 输出未开启 | ✅ 闭环 | 每 (T,B) 组合 conditioning 前 `output_on()`（I-V L1203） |
| 4 驱动缺 `DELT:MATH VOLT` | ✅ 闭环 | `setup_delta` 显式写 VOLT；R-T/MR 沿用 |
| 5 R-T visa_hint_var 未定义 | ✅ 闭环 | `__init__` 已定义并绑定 |
| 6 I-V 自动气压缺失 | ✅ 闭环 | `enforce_thermal_link` 已接入（hysteresis 跨点延续，2026-08-29 增强） |
| 7 I-V 2182A 配置无守卫 | ✅ 闭环（方案演进） | I-V 仅 DC，2182A 恒连恒配 |
| 8 DELTA 过 I=0 抛错 | ✅ 闭环（方案演进） | I-V 移除 DELTA 后不适用；R-T/MR DELTA 用固定 \|I\| 无此问题 |
| 9 SWEEP 非连续扫温 | ✅ 闭环（增强） | 到达即采样已落地；2026-08-29 再改为每场次一次性 TSET + 方向穿越判定 |
| 10 `delta_voltage_V` 语义 | ◐ 部分闭环 | MR 有 `electrical_mode` 列；R-T 以 `trace_type`（DC_DIRECT/DELTA/CALC1）区分；I-V 仅 DC 无需 |
| 11 I-V 重复 NPLC 控件 | ✅ 闭环 | 已删重复块 |
| 12 R-T 模块级 monkeypatch | ✅ 闭环 | `last`/`finished` 并入类内 `_process_queue` |
| 13 R-T 未用 import | ✅ 闭环 | 已删 `os`/`math` |
| 14 I-V docstring DELTA 冲突 | ✅ 闭环（方案演进） | 移除 DELTA 后 docstring 重写为仅 DC |
| 15 I-V 环境量单次快照/时间戳失真 | ✅ 闭环 | 2026-08-29 第四轮：`_append_iv_point` 每点实时 `readings()` + 独立时间戳 |
| 16 复位机制 | ✅ 闭环 | 独立 Reset 按钮手动触发（不自动），不复位 Oxford |
| 17-19 I-V 扫场增强（settle/说明栏/分段表） | ✅ 闭环 | 均已落地（分段表解析、量程推导、conditioning） |
| 20 多 (T,B) 组合 DC 输出关闭 | ✅ 闭环 | 每组合 `output_on()` 幂等 |
| 21 复位跨线程访问 Tk | ✅ 闭环 | 全部改 `put(("log", ...))` 队列路由 |
| 22 `_sequence` 超限静默截断 | ✅ 闭环 | for-else 抛 ValueError |
| 23 文档/死代码清理 | ✅ 闭环 | 已删 IVPoint/dataclass/重复 Path |
| 24 R-T 模块级缺 pyvisa import | ✅ 闭环 | 模块级 `import pyvisa` |
| 25 R-T 模式切换不重连/旧会话泄漏 | ✅ 闭环（方案演进） | `_close_instruments` 已实现；I-V 移除 DELTA 后无模式切换 |
| 26 冗余清理 | ✅ 闭环 | 已删 |
| 复查风险 1：STOP 与测量并发访问 6221 VISA | ✅ 闭环 | 2026-08-29 第一轮：6221/2182A/MatrixSwitch 全部加 RLock（仪器级写锁） |
| 复查风险 2：无 Reconnect 按钮 | ⬜ 可选 | 初始连接失败可点 Refresh 或重开；次要 UX |
| 复查风险 3：CSV 无 electrical mode 列 | ◐ 部分闭环 | 见条目 10 |
| 建议 1：Ic 自动判定 | ⬜ 可选 | 需配合 (T,B) 批量；不影响上机正确性 |
| 建议 2：DC 读前 settle | ✅ 闭环 | R-T/MR 新增 `dc_settle_var`；I-V 原有点 settle |
| 建议 3：R-T 转变区加密采样 | ⬜ 可选 | 可用 SWEEP 高采样率近似 |
| 建议 4：VTI-Probe 温差警戒 | ⬜ 可选 | 未加阈值报警 |
| 建议 5：固定量程保持 | ✅ 闭环 | 保持 |
| 建议 6：停测后电流归零回读确认 | ⬜ 可选 | `output_off()+set_current(0)` 已有，未加回读打印 |

**结论**：26 个编号缺陷全部闭环（8 条经"移除 DELTA"方案演进）；复查遗留 3 项风险中
**1 项已于 2026-08-29 第一轮闭环**（仪器级 RLock）；剩余 4 项优化建议 + 1 项 UX
（Reconnect）与 1 项列语义（electrical mode，MR 已有、R-T 以 trace_type 区分）为
**可选项**，不影响上机正确性。
