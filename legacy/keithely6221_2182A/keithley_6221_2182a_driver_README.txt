Keithley 6221 + 2182A 完整驱动

默认实验参数：
  2182A NPLC = 10 PLC
      50 Hz 市电下约 200 ms 单次 integration
  6221 Delta DELAY = 100 ms
  6221 Delta COUNT = 1
  6221 Trace points = 1
  AC Phase Marker = ON

6221 Delta流程：
  TRAC:CLE
  TRAC:POIN 1
  TRAC:FEED CALC1
  TRAC:FEED:CONT NEXT
  SOUR:DELT:HIGH
  SOUR:DELT:LOW
  SOUR:DELT:DELay
  SOUR:DELT:COUNt 1
  SOUR:DELT:CABort ON
  SOUR:DELT:CSWitch OFF
  SOUR:DELT:ARM
  INIT:IMM

运行结束后检查：
  TRAC:POIN:ACT?      应为 1
  TRAC:DATA:TYPE?    应出现 Delta 数据类型，而不是 NONE
  TRAC:DATA?          获取原始 Trace Buffer 数据

2182A：
  固定 voltage range，避免 Auto Range；
  NPLC 默认 10；
  由 6221 通过 SYST:COMM:SER:SEND 配置 2182A；
  正常的 6221-controlled Delta 不让 2182A 自己启动 standalone Delta。

AC：
  Phase Marker 默认保留 ON。
