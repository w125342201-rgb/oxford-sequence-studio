R-T 测量 GUI（TeslatronPT R-T Measurement）
===========================================
文件：teslatronpt_rt_gui.py
类  ：RTMeasurementApp

功能：离散磁场 × 温度序列的电阻-温度测量 R(T)。磁场外层循环、温度内层循环；
     每点用 6221+2182A 的 DC 或 DELTA 模式测量。可选启用"矩阵开关批量测量"，
     在一个温度点稳定后自动切换样品不同引脚组合，单次降温完成全部 pin 对。

界面布局（880×540，半屏）
  - 标题行：程序名 + 连接状态
  - Live 区：单行实时显示 VTI / Probe(Sample) / Probe-VTI / Pressure / Field /
    B setpoint / RFST
  - Measurement setup：全部测量参数（见下）
  - 底部一行：Start / STOP / Reset + 进度 + 最近 I/V/R
  - Log 区：运行日志（可滚动，拉高窗口自动扩展）

GPIB/VISA 地址（GUI 下拉 + Refresh 自动扫描，也可手动输入）
  - 6221 ：必填（默认 GPIB0::12::INSTR）
  - 2182A：仅在 DC 模式需要（默认 GPIB0::3::INSTR）；
           DELTA 模式下由 6221 经串口/Trigger Link 控制，可留空
  - 本 GUI 不含 SR830 地址接入

测量参数（GUI 字段 → 默认值 → 说明）
  - Delta current |I| → 1e-05 A
      6221 电流幅值；DELTA 模式实际输出 +I / -I 交替。
  - 2182A NPLC → 10（50 Hz 下约 200 ms）
      纳伏表积分时间（工频周期数），越大越准越慢。
  - Delta delay → 0.1 s
      DELTA 模式 HIGH/LOW 电流切换后、2182A 采样前的等待。
  - Auto pressure → 勾选（默认）
      依据样品(probe)实际温度自动切换 VTI 交换气气压：
      <10 K → 4 mbar；≥10 K → 10 mbar（±0.5 K 迟滞防抖）。
  - Discrete fields → "0, 1, 2, 3, 4, 5"（T，逗号分隔）
      离散磁场列表；每个磁场先升场并等待 B 稳定，再跑完整温度序列。
  - Temperature mode → STEP（默认）/ SWEEP
      STEP：逐点设温，进入容差并持续稳定（T tolerance + T stable）后测量；
      SWEEP：启用 iTC 温度 ramp，按速率连续变温、到达目标即采样（无稳定 soak）。
  - STEP Start / Stop / Step → 2.0 / 10.0 / 0.2 K
      等间隔温度序列（可反向，自动补终点）。
  - SWEEP Start / Stop / Sample / Ramp → 2.0 / 10.0 / 0.2 K / 5.0 K/min
      采样点序列与变温速率。
  - T tolerance / T stable → 0.02 K / 10 s
      温度稳定判据：|T实际 − T目标| ≤ tol 且持续 stable(s)。
  - B tolerance / B stable → 0.002 T / 5 s
      磁场稳定判据（语义同温度）。
  - Electrical mode → DELTA（默认）/ DC
      DELTA：6221 控制 2182A（Trigger Link），R = VΔ/(2|I|)，抑制热电势；
      DC：6221 输出 +I、2182A GPIB 直读，R = V/I（交叉验证用）。
  - Data file → rt_measurements.csv
      CSV 逐点追加，UTF-8。

矩阵开关批量测量（Matrix batch，独立区，默认关闭）
  - Enable (forces DELTA)：勾选后强制 DELTA 并禁用 DC 单选（需求：批量仅 delta）。
  - Simulate：离线联调，不连硬件。
  - VISA：矩阵开关地址（默认 GPIB0::20::INSTR）。
  - settle(s) → 0.1 s：切换继电器后的稳定等待。
  - Combos (I+,I-,V+,V-)：每行一个组合，4 个列号（1..24），逗号分隔；
      例：1,6,2,3 表示 I+=列1、I-=列6、V+=列2、V-=列3。
      四列两两不同、组合不得重复。
  - 安全时序：切换前 6221 output OFF（:OUTP? 确认）→ 矩阵路由 → settle →
    run_delta_once（内部重新开流）→ 记录一行（含 pin_combo 列）；
    失败写 MATRIX_ERROR 占位行并继续；STOP 后矩阵全断（open_all）兜底。
  - 行映射固定：行1=I+、行2=I-、行3=V+、行4=V-（matrix.route_4wire 默认）。
  - 详见 matrix_batch_measurement.md。

温控联动（与 I-V / MR 相同）
  - Probe (DB8.T1) = 样品温度；VTI (DB6.T1) 永久跟随：
      T<10 K → VTI = T − 0.2 K；T≥10 K → VTI = T − 1.0 K。
  - 自动气压基于实际 Probe 温度 + hysteresis。

CSV 输出（13 列）：
  timestamp, field_T, temperature_setpoint_K, vti_temperature_K,
  probe_temperature_K, pressure_mbar, current_A, delta_voltage_V,
  resistance_ohm, trace_type, raw_trace, temperature_mode, pin_combo
  - temperature_setpoint_K = 名义设定温度（该点 GUI 下发的 target_t）；
    probe_temperature_K = 该点实时传感器实际读数（两者差值即热平衡偏差
    ΔT = T_actual − T_setpoint，供离线分析）。
  - pin_combo 非矩阵模式为空；旧 12 列文件按 legacy 模式处理并警告。

独立程序：
  本程序不 import I-V / MR GUI 代码，只依赖 oxford / keithely6221_2182A /
  matrix API 包（骨架按项目约定在文件内复制）。
