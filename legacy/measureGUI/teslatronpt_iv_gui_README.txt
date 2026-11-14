I-V 测量 GUI（TeslatronPT I-V Measurement）
===========================================
文件：teslatronpt_iv_gui.py
类  ：IVMeasurementApp

功能：离散温度 × 离散磁场的电流-电压特性测量 I(V)。
     对每一组 (T, B)：设温稳定 → 升场稳定 → 执行分段电流扫描（3×n 段拼接），
     每个电流点做 DC 电学测量（6221 输出 + 2182A 直读）；数据逐点追加 CSV。

界面布局（880×540，半屏）
  - 标题行：程序名 + 连接状态
  - Live 区：单行实时显示 VTI / Probe(Sample) / Probe-VTI / Pressure / Field /
    B setpoint
  - I-V setup：全部测量参数（见下）
  - 底部一行：Start / STOP / Reset + 进度 + 最近 I/V/R
  - Log 区：运行日志（可滚动，拉高窗口自动扩展）

GPIB/VISA 地址（GUI 下拉 + Refresh 自动扫描，也可手动输入）
  - 6221 ：必填（默认 GPIB0::12::INSTR）
  - 2182A：必填（默认 GPIB0::3::INSTR）；I-V 仅用 DC 模式，2182A 始终直连
  - 本 GUI 不含 SR830 地址接入

测量参数（GUI 字段 → 默认值 → 说明）
  - Temperatures → "2, 4, 6, 8, 10"（K，逗号分隔）
      离散温度点；每个温度点设温并稳定后执行一遍完整 I-V 扫描。
  - Fields (T) → "0"（T，逗号分隔）
      离散磁场点；每个 (T,B) 组合各做一遍 I-V 扫描。
  - Current sweep (segmented, 3×n) → 默认 ±100 µA、步长 1 µA 双向两段
      任意分段电流扫描表（三行、列数一致，n 列即 n 段）：
        第 1 行：各段起点（A）
        第 2 行：各段终点（A）
        第 3 行：各段步长（A，须 >0）
      段间按列序拼接成完整扫描；每点标记所属段 branch="segK"。
      经典双向 I-V 填两段：+Imax→−Imax 与 −Imax→+Imax。
  - Point settle → 0.2 s
      设好每个电流点后、读电压前强制等待（DC 模式生效）；
      用于样品/热链路建稳（超导样品建议 0.05–0.5 s，可设 0 关闭）。
  - Zero step / Zero delay → 1e-06 A / 0.05 s
      两段之间把电流缓慢过零时的步长(A)与每步延迟(s)，
      仅用于 conditioning 缓变，不计入数据。
  - 2182A NPLC → 10（50 Hz 下约 200 ms）
      纳伏表积分时间，越大越准越慢。
  - Auto pressure → 勾选（默认）
      依据样品(probe)实际温度自动切换 VTI 交换气气压：
      <10 K → 4 mbar；≥10 K → 10 mbar（±0.5 K 迟滞防抖）。
  - T tolerance / T stable → 0.02 K / 10 s
      温度稳定判据：|T实际 − T目标| ≤ tol 且持续 stable(s)。
  - B tolerance / B stable → 0.002 T / 5 s
      磁场稳定判据（语义同温度）。
  - 电学模式：仅 DC（设计定论，2026-08-29）
      I-V 是单调电流扫描，必须逐点记录指定电流下的响应；
      6221 Delta 是 ±I 交替的电阻/磁阻取向测量，既不必要也不适合 I-V：
      每点需软件重建符号 V(I)=sgn(I)·VΔ/2，且逐点 ARM/INIT 约 1~3 s/点，
      长扫描极慢。故 I-V GUI 只保留 DC：
      6221 输出在该 (T,B) 组合内全程常开（0→首点前开启、末点→0 后关闭），
      每点 set_current → Point settle → 2182A 直读 V，R = V/I。
  - CSV → iv_measurements.csv
      CSV 逐点追加，UTF-8。

扫描时序：
  首段前 0 → 起点（慢扫 conditioning，不保存）
  → 逐段按步长扫描（保存，branch=segK）
  → 末段后 终点 → 0（慢扫 conditioning，不保存）

温控联动（与 R-T / MR 相同）
  - Probe (DB8.T1) = 样品温度；VTI (DB6.T1) 永久跟随：
      T<10 K → VTI = T − 0.2 K；T≥10 K → VTI = T − 1.0 K。
  - 自动气压基于实际 Probe 温度 + hysteresis。

CSV 输出（11 列）：
  timestamp, temperature_setpoint_K, temperature_K, vti_temperature_K,
  pressure_mbar, field_setpoint_T, field_T, current_A, voltage_V,
  branch, temperature_mode
  （注：2026-08-31 起不再自动计算并写出 resistance_ohm 列。超导 I-V 测试中
   R=V/I 在过零点发散、无物理意义，故取消该自动计算，仅保留 V(I) 与 Ic。）

独立程序：
  本程序不 import R-T / MR GUI 代码，只依赖 oxford / keithely6221_2182A
  API 包（骨架按项目约定在文件内复制）。
