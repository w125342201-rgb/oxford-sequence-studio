teslatronpt_mr_ac_gui.py — MR 磁阻测量（6221 交流激励 + PXI-4462 数字锁相）
=============================================================================

**纯磁场程序（用户裁决 2026-09-04）：只保留磁场 STEP/SWEEP 扫描；不含
2182A/Delta/DC/矩阵任何选项。** 唯一电学读数是 AC 数字锁相：
6221 输出正弦电流 I_AC·sin(2πft)（I_AC = 上方的 I_AC (A)，默认 10 µA，
f 默认 13 Hz，全程常开），样品交流电压由 PXI-4462 采集 1 s，程序做
**数字锁相**（整周期单 bin FFT），输出 R = V_peak / I_peak 与 X/Y/相位；
6221 phase marker（每周期 0°、Trigger 线 1）→ 4462 PFI0 启动每次采集。

硬件接线（官方规格核实 2026-09-04）
-----------------------------------
  * 6221：GPIB（GUI 地址下拉）。后面板 Trigger 连接器 line3（默认；
    GUI 未开放，代码常量 DEFAULT_MARKER_LINE=3，SOUR:WAVE:PMAR:OLIN）在
    每周期 0° 输出 1 µs phase-marker 脉冲。
  * 该 line → PXI-4462 前面板 **PFI 0**（数字触发输入 BNC）：上升沿启动
    1 s 有限采集，采样窗与激励相位锁定。
  * 样品电压（四线或两端）→ 4462 AI±（term=diff 用差分 AI+/-；若样品与
    机箱共地可 rse）。IEPE 激励自动关闭；耦合默认 dc。
  * PXI-4462 位于 PXI-1036 机架，经 PXI-8360 (MXI-Express) 连主机；
    NI-DAQmx 驱动 + `pip install nidaqmx`。GUI 勾 "DAQ simulate" 则不碰
    硬件（生成 13 Hz+噪声合成波形跑完整锁相路径）。

数字锁相原理
------------
  * 采样 51.2 kS/s × 1.000 s → 窗内含 f(=整数 Hz) 个整周期 → 矩形窗单
    bin DFT 无泄漏；50/60 Hz 电网谐波同为整数 Hz，各自落 bin 不串扰。
  * R = 2|X_k| / (N·I_AC)；X/Y 相对窗起点（=marker 沿）给出。
  * 每点耗时 ≈ 1 s 采集 + 触发等待；SWEEP 沿场 ramp 采样与 STEP 均支持。

GUI 参数
--------
  * 磁场：T 列表 + 3×N 场段表，Field mode = STEP / SWEEP（含段 dwell、
    iPS 中止自愈等 MR 全部磁场安全逻辑）。
  * I_AC (A) pk：交流激励峰值（默认 10 µA）。
  * f (Hz)：激励频率（默认 13；整数 Hz 保证整周期；避开 50/60 Hz 与
    4462 AC 耦合高通 ~3.4 Hz）。
  * DAQ simulate：无硬件跑通流程用。
  * 4462：通道（**空=自动发现并全采 4 路 ai0..ai3 并行**；也可填单个通道
    名如 PXI1Slot2/ai2 只采一路）、term(rse/diff)、coup(dc/ac)、
    range(V)（默认 1；小信号可用 0.316）。
  * **多路同采**：一次 marker 触发采 4 路并各自数字锁相；ai0 为主样品
    （入 voltage_V/resistance_ohm 主列），ai1..ai3 的锁相 R 分别入
    CSV 的 R_ai1_ohm / R_ai2_ohm / R_ai3_ohm 列（接多样品/多接触用）。
  * **二次谐波 2f**：同一 1 s 窗内除基波 f=13 Hz 外还解调 2f=26 Hz
    （同为整数 Hz → 整周期 bin，无泄漏）。每路输出 V2f 峰值与比值
    ratio2 = V2f/V1f（非线性/整流响应的无量纲探针），CSV 每路两列：
    V2_aiN_V 与 ratio2_aiN（N=0..3）。

CSV：与 MR 相同列；AC 模式 voltage_V=V_peak、resistance_ohm=R、
trace_type="LOCKIN"、raw_trace="x,y,phase_deg"、electrical_mode="AC"。

验证
----
  * 锁相数学自测 .workbuddy/tmp/_lockin_selftest.py（11/11：幅度/相位
    恢复、50 Hz 隔离、simulate 路径）。
  * 全流程仿真 .workbuddy/tmp/_mr_ac_sim.py（9/9：STEP 3 点锁相
    R≈3 Ω、marker 线3 命令、wave arm/init/abort 顺序）。
  * 启动同其它程序：带 tkinter/pyvisa/nidaqmx 的解释器前台运行。
