Oxford TeslatronPT Python API

依据：最新提交的 teslatronpt_gui_v2.py 作为上层需求与机器拓扑的唯一来源。

机器：
  iTC-VTI   192.168.1.10:7020
    DB6.T1 = VTI
    DB3.P1 = exchange-gas pressure

  iTC-Probe 192.168.1.11:7020
    DB8.T1 = sample/probe

  iPS       192.168.1.12:7020
    GRPZ = magnet

温度：
  sample/probe TSET = T
  VTI TSET = T - 0.2 K (T<10 K)
  VTI TSET = T - 1.0 K (T>=10 K)

自动气压：
  以实际 probe 温度判断；
  4 mbar / 10 mbar；
  10 K 附近使用 ±0.5 K hysteresis，避免反复 R-T 扫描时频繁切换。

温度 ramp：
  API 已统一为一个 rate + 一个 enable；
  两个 iTC 同时设置相同 RSET 和 RENA。

磁场：
  RFST = T/min
  FSET = T
  RTOS = ramp to target
  HOLD = hold
  RTOZ = ramp to zero
  当前实验室 GUI 使用的最大 field ramp = 0.237 T/min。

典型用法：
  from oxford_teslatronpt_api import TeslatronPT

  with TeslatronPT() as pt:
      print(pt.identify_all())
      print(pt.readings())

      pt.configure_temperature_ramp(rate_k_per_min=5.0, enabled=True)
      pt.set_sample_temperature(8.0)

      pt.set_pressure(4.0)

      pt.set_field(2.0, ramp_rate_t_per_min=0.1)

注意：
  API 直接控制真实设备。首次测试应先确认 IDN 和读数，再进行小幅度设定。
