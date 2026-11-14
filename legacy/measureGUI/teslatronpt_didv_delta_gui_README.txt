teslatronpt_didv_delta_gui.py — I-V + dV/dI (6221 DCON, Delta 连线，无 SR830)
============================================================================

与 teslatronpt_didv_SR830_gui.py（需 SR830 锁相）互补的独立程序：只用
Keithley 6221 + 2182A 的官方 Delta 连线（6221 经 GPIB 控制，2182A 由
6221 经 RS-232 + Trigger Link 托管），在电流轴每个点上同时给出：

  * 常规 Delta 读数：Vdelta = V(+|I|) − V(−|I|)，R = Vdelta / (2|I|)
  * 6221 原生 Differential Conductance (DCON) 微分读数：
    每段整段跑一次官方 DCON 扫（START/STEP/STOP = 分段表），引擎对每个
    偏置点施加 ±dI 小信号、用 3 采样滑动窗算 dV，输出
        dR = dV/dI （ohm）  与  dG = dI/dV （S）

官方依据：Model 6220/6221 User's Manual 第 5 章 "Delta, Pulse Delta and
Differential Conductance"（Figure 5-10 波形/公式、Table 5-4）、SCPI 表
B-6；命令序列对照该章官方示例。2026-09-03 用户拍板：段级 DCON 扫 +
固定 dI + 全列输出。

启动
----
与其它测量程序一致，用带 tkinter/pyvisa 的解释器前台启动，例如：
    C:\ProgramData\Miniconda3\python.exe teslatronpt_didv_delta_gui.py

连线
----
  * 6221  → GPIB（GUI 地址下拉，默认 GPIB0::14::INSTR）
  * 2182A → 前面板 SHIFT+RS-232 ON（19200 / XON/XOFF / CR），再连 RS-232
            到 6221 + Trigger Link。DELTA 模式下 2182A 的 GPIB 地址可留空。
  * 样品四线接 6221 输出与 2182A 输入（同 Delta 标准接法，见图 5-2B）。

GUI 参数（本程序 DELTA only；纯 DC I-V 用 teslatronpt_iv_gui.py）
------------------------------------------------------------------
  * 电流轴：单段 Start / Stop / Step（A，科学计数法）一条线；
    每段先一次 DCON 段扫（不落盘），随后逐点 Delta-R 并即时写 CSV；
    每行含 I、Vdelta、R_delta、dV_dcon、dR(=dV/dI)、dG(=dI/dV)。
  * dI (A)：微分摆幅（固定值）。太小被电压噪声淹没，太大不再是微分——
    建议先试 ~1% 量程再调；超导转变研究常取量程的 0.1–1%。
  * Delta dly (s)：每个 delta 电平的稳定时间（Delta 与 DCON 共用）。
  * 温度/磁场：与 I-V 程序相同（(T,B) 组合网格，Mercury 控制）。
  * 电流轴：3×n 分段表（行=起点/终点/步长）。

已知语义 / 注意
--------------
  * I = 0 点无 Delta 摆幅 → Vdelta / R_delta 留空，但 DCON 微分照常有值。
  * 每段至少 2 个偏置点（官方 DCON 只有多点扫示例；单点段会明确报错）。
  * 降序段（START > STOP）：2026-09-04 真机确认 6221 DCON 固件拒绝反向
    sweep（ARM 期报 +416 "Step size too small"，笼统配置错）；driver 已内
    置自动翻转——降序段以升序下发、读数反向映射回原序（微分是局部量，
    物理零损失），日志会提示 "auto-flipped"。
  * DCON 引擎自控电流输出（每级按 Delta dly 停留），DELTA 模式没有 DC
    模式的 0→首点缓变 conditioning。
  * 每段 DCON 之间、DCON 与 Delta 之间由 6221 *RST 重建引擎状态，段多时
    每段有约数秒切换开销。

CSV 列（15）
-----------
timestamp, temperature_setpoint_K, temperature_K, vti_temperature_K,
pressure_mbar, field_setpoint_T, field_T, current_A, voltage_V(=Vdelta),
resistance_ohm(=Vdelta/(2|I|)), dV_dcon_V, dR_ohm(=dV/dI), dG_S(=dI/dV),
branch, temperature_mode

代码位置
--------
  * GUI：measureGUI/teslatronpt_didv_delta_gui.py（独立，不改动 I-V/SR830 版）
  * 6221 DCON 封装：keithely6221_2182A/keithley_6221_2182a_driver.py
    → Keithley6221.run_dcon_segment()（官方 SCPI 序列，读原始 dV）
  * 仿真：.workbuddy/tmp/_didv_delta_sim.py（假 6221 session 全流程，
    10/10 PASS：SCPI 接受、物理式 R/R/dR/dG 一致、seam 去重、0 点语义）
