磁阻测量 GUI（TeslatronPT Magnetoresistance R-B）
=================================================
文件：teslatronpt_mr_gui.py
类  ：MRMeasurementApp

功能：离散温度下分段扫描磁场、逐点测量电阻 R(B)。
     对每个离散温度点：设温并稳定（STEP 语义），然后执行一遍完整磁场扫描；
     每个磁场点按 Field mode 判据（STEP 稳定 / SWEEP 到达）后做 DELTA 或
     DC 电学测量。

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
  - Temperatures → "2, 5, 10, 20"（K，逗号分隔）
      离散温度点（外循环）；每个点设温并稳定后执行一遍完整磁场扫描。
  - Bias current |I| → 1e-05 A
      6221 电流幅值；DELTA 模式实际输出 +I / -I 交替。
  - 2182A NPLC → 10（50 Hz 下约 200 ms）
      纳伏表积分时间，越大越准越慢。
  - Delta delay → 0.1 s
      DELTA 模式 HIGH/LOW 电流切换后、2182A 采样前的等待。
  - Field ramp → 留空（= 磁体当前 RFST，iPS 不改动）
      磁场升场速率 T/min；硬上限 0.237 T/min，程序强制校验。
  - B settle → 1.0 s
      仅 SWEEP 模式生效：到达容差后、电学采样前的额外等待；可设 0 关闭。
  - Field mode → STEP（默认）/ SWEEP
      STEP：逐点等 B tolerance + B stable soak 后采样（高精度）；
      SWEEP：B tolerance 到达即采样 + B settle（快速扫描）。
  - Field sweep (segmented, 3×n) → 默认 ±1 T 完整回线
      任意分段磁场扫描表（三行、列数一致，n 列即 n 段）：
        第 1 行：各段起点（T）
        第 2 行：各段终点（T）
        第 3 行：各段步长（T，须 >0）
      段间按列序拼接成完整扫描；相邻段共享的边界磁场点只测量一次（去重）。
      每点标记所属段 branch="segK"。
      默认（4 段）：0 → +1 → 0 → −1 → 0，步长 0.1 T（共 41 点）。
  - T tolerance / T stable → 0.02 K / 10 s
      温度稳定判据：|T实际 − T目标| ≤ tol 且持续 stable(s)。
  - B tolerance → 0.002 T：磁场"到达/稳定"判据（两种模式均用）。
  - B stable → 10 s（仅 STEP 模式）：进入容差后须持续 B stable(s) 才采样。
  - Electrical mode → DELTA（默认）/ DC
      DELTA：6221+2182A，HIGH=+I / LOW=−I / COUNT=1，
          V_delta = V(+I) − V(−I)；R = VΔ / (2|I|)，抑制热电势；
      DC：6221 输出 +I、2182A GPIB 直读 V；R = V / I。
  - Data file → mr_measurements.csv
      CSV 逐点追加，UTF-8。

温控/气压联动（与 I-V / R-T 相同）
  - Probe (DB8.T1) = 样品温度；VTI (DB6.T1) 永久跟随：
      T<10 K → VTI = T − 0.2 K；T≥10 K → VTI = T − 1.0 K。
  - 自动气压基于实际 Probe 温度 + ±0.5 K 迟滞：<10 K→4 mbar；≥10 K→10 mbar。

CSV 输出（14 列）：
  timestamp, temperature_setpoint_K, temperature_K, vti_temperature_K,
  pressure_mbar, field_setpoint_T, field_T, current_A, voltage_V,
  resistance_ohm, trace_type, raw_trace, electrical_mode, branch

独立程序：
  本程序不 import I-V / R-T GUI 代码，只依赖 oxford / keithely6221_2182A
  API 包（骨架：连接/轮询/复位/队列按项目约定在文件内复制）。

矩阵开关批测（可选，R-T 同款；2026-09-04 加入）
------------------------------------------------
* 勾选 "Enable matrix" 后自动强制 Elec=DELTA + Field=STEP（多通道轮询
  需要磁场在每个 B 点静止；SWEEP/DC 与其物理不兼容，被禁用）。
* "Simulate" 勾选 = 用 MatrixSwitch 官方 simulate 模式（不连硬件，先跑通
  流程用）。Combos 每行一组四线接法：I+列, I-列, V+列, V-列（1..24）。
* 每个 (T, B) 点：依次 output_off → route_4terminal → settle → Delta 全
  reset 测量（reset_before=True/configure_2182=True，每 combo 全复位，防
  9.91E+37 sentinel），全部 combo 测完写一条宽表行（每 combo 一列
  R_<I+>-<I->-<V+>-<V->）；失败的 combo 该列留空并记日志，不中断整场。
* 宽表 CSV 头与普通 14 列不同；若目标文件已有不同表头自动转存带时间戳
  新文件（不破坏旧数据）。推荐矩阵 run 使用独立文件路径。
* 切换矩阵在 6221 输出关断后执行（绝不带电流切继电器）；结束/出错都会
  强制 output off + 电流归零。
* 仿真验证：.workbuddy/tmp/_mr_matrix_sim.py（7/7 PASS：3 B 点 x 2
  combo → 3 宽行、R 值正确、每 combo 全复位、失败跳过留空）。
