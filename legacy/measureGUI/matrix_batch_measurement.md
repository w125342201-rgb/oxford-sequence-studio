# R-T 测量 · 矩阵开关批量测量功能实现说明

> 对应需求：R-T 测量中每个温度等待点稳定后，通过矩阵开关自动切换样品各引脚组合，
> 单次降温完成所有 pin 对测量；启用后仅保留 DELTA 模式；切换前必须切断电流、稳定后再测。
> 实施日期：2026-08-29。已通过离线验证（详见文末），真实硬件验证步骤见 §7。

---

## 1. 功能概述

在 `measureGUI/teslatronpt_rt_gui.py` 中新增 "Matrix switch batch measurement" 配置区。
勾选启用后，测量主循环（磁场 × 温度）在**每个温度点稳定之后**，按用户配置的组合列表依次：

1. **切断电流**（6221 `OUTPUT OFF` + `:OUTP?` 确认 + 电流归零）；
2. **切换矩阵**：将 I+/I−/V+/V− 四个仪表行路由到该组合指定的 4 个样品引脚列；
3. **等待稳定**（可配置 settle 时间）；
4. **Delta 单次测量**（`run_delta_once`），记录一行数据；
5. 下一个组合，直至该温度点全部完成，然后进入下一温度点。

每组合一行 CSV，新增 `pin_combo` 列标识当前组合（如 `1,6,2,3`）。

## 2. 配置数据结构设计

### 组合输入格式（GUI 多行文本框，每行一个组合）

```
I+列, I-列, V+列, V-列
```

示例：

```
1,6,2,3
1,6,4,5
7,8,9,10
```

- 列号范围 1..24（矩阵输入列，样品引脚）。
- 容忍空格、全角逗号 `，`、分号 `;`。
- **校验规则**：每行恰好 4 个整数；四列两两不同；组合两两不重复（重复会报错，避免白白加倍测量时长）。

### 行映射常量（固定）

```python
MATRIX_ROWS = (1, 2, 3, 4)   # 行1=I+, 行2=I-, 行3=V+, 行4=V-
```

与 `matrix.route_4wire` 的默认语义一致；行 5/6 备用。

### GUI 新增字段

| 控件           | 变量                    | 默认值                   | 说明                             |
| ------------ | --------------------- | --------------------- | ------------------------------ |
| 启用复选框        | `matrix_enabled_var`  | False                 | 勾选后**强制 DELTA 并禁用 DC 单选**（需求3） |
| Simulate 复选框 | `matrix_simulate_var` | False                 | 离线联调，不连硬件                      |
| 矩阵 VISA 地址   | `visa_matrix_var`     | `GPIB0::23::INSTR`    | 并入 "Refresh VISA resources" 扫描 |
| 组合文本框        | `matrix_combos_text`  | `1,6,2,3` / `1,6,4,5` | tk.Text，每行一个组合                 |
| 切换稳定时间       | `matrix_settle_var`   | 0.1 s                 | 继电器稳定等待                        |

### 数据记录

- `RTPoint` dataclass 末尾新增 `pin_combo: str = ""`。
- CSV 表头追加 `pin_combo` 列（13 列）；非矩阵模式写空串。
- **旧文件兼容**：启动时检测已有文件首行表头；若为旧 12 列（无 `pin_combo`），记 log 警告并以 legacy 模式继续写 12 列（新矩阵模式建议换新文件名）。

## 3. 关键接口定义

### 驱动层新增

| 对象                             | 签名                                                                              | 职责                                                           |
| ------------------------------ | ------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| `Keithley6221.output_state`    | `() -> bool`                                                                    | 查询 `:OUTP?`；ON→True / OFF→False（切换前确认电流已断）                   |
| `MatrixSwitch.route_4terminal` | `(i_plus, i_minus, v_plus, v_minus, rows=(1,2,3,4), open_others=True) -> bytes` | 任意 4 列四端路由；校验四列/四行两两不同（防短路）；内部转 列→行 mapping 复用 `set_mapping` |

### GUI 层新增

| 方法                                              | 职责                                                                           |
| ----------------------------------------------- | ---------------------------------------------------------------------------- |
| `RTMeasurementApp._parse_combos(text)`          | 组合文本解析 + 校验（越界/非整数/列重复/组合重复）                                                 |
| `RTMeasurementApp._switch_to_combo(combo)`      | **安全时序核心**：`output_off` → `:OUTP?` 确认（重试一次）→ `route_4terminal` → settle 分段等待 |
| `RTMeasurementApp._sleep_stop_aware(sec)`       | 分段 sleep，逐段检查 stop_event，STOP 响应不卡顿                                          |
| `RTMeasurementApp._safe_output_off()`           | 电流安全关闭兜底（`output_off` + `set_current(0)`），异常仅记 log                           |
| `RTMeasurementApp._record_rt_point(...)`        | 统一数据记录：读现场快照 → RTPoint → CSV 追加 → GUI 刷新 → log（单点/批量共用）                      |
| `RTMeasurementApp._on_matrix_enabled_changed()` | 勾选时强制 DELTA、禁用 DC 单选，并触发重连以接入矩阵                                              |

### GUI 层修改

- `_measurement_worker(fields, temps, combos=None)`：温度稳定后按组合循环测量；
  `elec_mode` 统一解析（批量时强制 `"DELTA"`）。
- `_connect_worker`：批量启用时创建 `MatrixSwitch`、`open()` 后 `open_all()` 同步安全态；
  **连接失败报错不降级**（避免静默丢失批量语义、浪费降温周期）。
- `stop_measurement` / `shutdown` / `_close_instruments`：矩阵关闭与 `open_all()` 安全兜底。
- `start_measurement`：启动前解析/校验组合并作为第三参数传入测量线程。

## 4. 电流安全时序（需求4）

```
┌──────────────────────────────────────────────────────────────┐
│  温度点稳定（_wait_temperature_stable 通过）                    │
│  └─ for combo in combos:                                      │
│      1. src.output_off()              ← 先切断电流             │
│         if src.output_state(): retry; 仍 ON 则报错             │
│         src.set_current(0.0)                                  │
│      2. sw.route_4terminal(*combo)    ← 切换矩阵（无电流冲击）   │
│      3. sleep(settle)                 ← 继电器稳定（stop 感知） │
│      4. run_delta_once()              ← delta 内部 ARM+INIT    │
│                                           重新开启输出          │
│      5. 记录一行（pin_combo 标识）                              │
│  └─ _safe_output_off()                ← 该温度点结束兜底关电流  │
│  STOP / 异常：_safe_output_off() + sw.open_all()               │
└──────────────────────────────────────────────────────────────┘
```

**关键点**：Delta 模式下 6221 通过 `:SOUR:DELT:ARM` + `:INIT:IMM` 内部管理 +I/−I 交替输出，
"重新开电流"由 `run_delta_once` 承担——**不要在 delta 之前显式 `output_on`**（会与 6221
内部源程序冲突）。切换前必须 `output_off` 兜底，测量返回后输出已空闲，仍以 `output_off` 收尾。

## 5. 与现有驱动协同

| 组件                        | 协同方式                                                                                |
| ------------------------- | ----------------------------------------------------------------------------------- |
| `oxford/`（TeslatronPT）    | 不变：磁场外层循环、probe/VTI 联动设温、auto-pressure、稳定判定均保持原逻辑                                   |
| `keithely6221_2182A`      | 复用 `run_delta_once`（DELTA 下 2182A 经 6221 串口控制，无需独立 GPIB）；新增 `output_state` 用于切换前确认  |
| `matrix/matrix_switch.py` | 新增 `route_4terminal` 提供任意 4 列路由；`set_mapping` 的 mapping 方向为 **列→行**；设备无回读，状态以本地缓存为准 |

## 6. 修改文件清单

| 文件                                                 | 改动                                                                                   |
| -------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `matrix/matrix_switch.py`                          | 新增 `route_4terminal`（约 20 行，`route_4wire` 之后）                                        |
| `matrix/test_matrix_switch.py`                     | 新增 6 个用例（帧格式、重复列/行、越界、open_others 语义）                                                |
| `keithely6221_2182A/keithley_6221_2182a_driver.py` | `Keithley6221` 新增 `output_state`                                                     |
| `measureGUI/teslatronpt_rt_gui.py`                 | import、常量、`RTPoint.pin_combo`、UI 区、连接/关闭/停止路径、组合解析与时序辅助、CSV 13 列 + legacy 兼容、主循环组合循环 |

## 7. 验证

### 已完成（离线）

- `py_compile` 三个修改文件通过；
- `matrix/test_matrix_switch.py` 29 项全过（含新增 6 项）；
- 离线验证脚本 13 项全过（`D:\AItest\verify_matrix_batch.py`）：
  `_parse_combos` 各校验分支、`route_4terminal(1,6,2,3)` 帧 `[13400200,00000000,00000000]`、
  `RTPoint.pin_combo` 默认/覆写、`output_state` 存在性、GUI 新增方法存在性。

### 留待现场（真实硬件，按序执行）

1. 矩阵单通道手动 `route` + 万用表导通确认；
2. 6221 `:OUTP?` 查询确认；
3. 固定单组合手动 delta 测量；
4. GUI：1 磁场 × 1 温度 × 2 组合试运行（勾选 Simulate 先离线验证 log 时序）；
5. 完整降温批量运行。

### 已知注意点

- 每组合新增 settle + delta 时间，进度消息体现组合维度（`Pin combo i/N`）；
- 组合失败写 `MATRIX_ERROR` 占位行（raw=异常信息）后继续，不中断整个降温；
- 矩阵连接失败**不静默降级**——宁可不起测（报错提示可改用 Simulate 或取消勾选）。
