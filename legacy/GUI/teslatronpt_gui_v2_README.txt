TeslatronPT GUI v2

机器拓扑（三台独立仪器，模块号已在实际工控机上核实）：
  iTC-VTI   TCPIP0::192.168.1.113::7020::SOCKET
    VTI 浴槽温度（背景）     DB6.T1
    交换气气压                DB3.P1
  iTC-Probe TCPIP0::192.168.1.114::7020::SOCKET
    Probe（样品）温度         DB8.T1
    样品精细控温回路 TSET    发到本机
  iPS       TCPIP0::192.168.1.115::7020::SOCKET
    Magnet    GRPZ

物理背景与联动控温：
  样品杆插在 VTI 中；VTI 提供背景温度与交换气气压，probe 温控仪（114）
  精细控温。样品温度以 probe（DB8.T1）为准。
  用户设定样品(=probe)温度 T 后，两台温控仪同时工作：
    probe TSET = T          （iTC-Probe 114）
    VTI   TSET = T - offset （iTC-VTI 113）
      offset = 1.0 K  (T >= 10 K)
      offset = 0.2 K  (T <  10 K)
  样品(probe)实际温度跨越 10 K 时，自动重新对位 VTI 偏移量并切换气压
  （±0.5 K 迟滞防抖）。

正确 Mercury ramp-rate 映射：
  RCST = current ramp rate (A/min)
  RFST = field ramp rate (T/min)

磁场（iPS 115）：
  RFST  -> field ramp rate
  FSET  -> target field
  RTOS  -> ramp to target

扫场速度限制：
  上限 0.237 T/min（≈39.5 Oe/s）
  GUI 严格限制为 0 < rate <= 0.237 T/min。

温度（联动，见上）：
  SET:DEV:DB8.T1:TEMP:LOOP:TSET:<K>      （iTC-Probe 114）
  SET:DEV:DB6.T1:TEMP:LOOP:TSET:<T-offset>（iTC-VTI 113）

温度斜坡（变温速率，回路级、只能远程设置）：
  SET:DEV:DB8.T1:TEMP:LOOP:RSET:<K/min>   （probe, 114）
  SET:DEV:DB8.T1:TEMP:LOOP:RENA:ON|OFF    （probe, 114）
  SET:DEV:DB6.T1:TEMP:LOOP:RSET:<K/min>   （VTI, 113）
  SET:DEV:DB6.T1:TEMP:LOOP:RENA:ON|OFF    （VTI, 113）
  GUI 只有一个 "Temp ramp rate" 输入：Apply 时自动把同一 RSET/RENA 同时发给
  probe(114) 与 VTI(113) 两回路，保持联动 ΔT 恒定。
  RENA=ON 后，TSET 改动按 RSET 速率斜坡；OFF 时阶跃生效。

压力（iTC-VTI 113）：
  SET:DEV:DB3.P1:TEMP:LOOP:TSET:<mbar>   （针阀控温回路挂在 TEMP 块，PRES 仅回读）

经验规则（按样品=probe 温度跨越 10 K，±0.5 K 迟滞自动切换）：
  T < 10 K   -> 4 mbar   (VTI 比 probe 低 0.2 K)
  T >= 10 K  -> 10 mbar  (VTI 比 probe 低 1.0 K)

注意：
  该程序直接控制真实 TeslatronPT。
  首次使用建议先用小磁场、小扫速测试，并核对三台仪器的 IDN 输出。

============================================================
样品杆旋转控制（GUI/rotator_gui.py，2026-09-08 实机标定）
========================================================

程序与驱动：
  GUI    GUI/rotator_gui.py（独立程序，不 import 其他测量程序）
  驱动   rotater/rotator_interface.py（包导入 from rotater.rotator_interface）
  协议细节全文档 rotater/README_rotator.md
  启动   C:\ProgramData\Miniconda3\python.exe GUI\rotator_gui.py
         （Tk GUI 必须用 Miniconda Python；托管 3.13.12 缺 tkinter）
  串口   COM3（FTDI USB-RS232，115200 8N1 无流控，实机确认）

设备工作模型（用户现场确认）：
  单向旋转，角度范围 0-360°，到 360° 截止。
  每次测量开始和结束必须发送回零指令 [z,1,1501]（≈10 °/s）；
  到达零点后控制器标记当前点为零点并发出蜂鸣（蜂鸣无法查询，以听觉为准）；
  回零后坐标从 0 重新计起。协议无停止/急停命令，超时/关窗都不会停止电机。

协议要点（全部实机标定）：
  查询 [?] -> 回包 [w,D,NNNNNN]：控制器步数寄存器，无绝对编码器。
  发令 D=0 -> 物理正转，D=1 -> 物理负转（原实现约定，目视确认）。
  寄存器解码符号与原实现截图相反：D=1 = 物理正角度（decode_angle 已翻转）。
  标度 150.13878（30° -> 4504 counts）；速度单位 °/s（30°@2°/s ≈ 15 s 实测）。
  GUI 输入任意角度自动模 360 换算（-90 -> 270，450 -> 90）。

查询时机（关键教训，2026-09-08 用户实测）：
  运动中/周期性查询 [?] 会干扰控制器步数计数（1 s 自动刷新曾把计数器搞串）。
  GUI 因此不做任何周期轮询，到位判断改为计时制：
    运动前静止读一次 -> 按距离/速度计时等待（+1.5 s 裕量，转动中零查询）
    -> 到点后静止状态每 1 s 校验一次，直到容差内或超时。
  不要对运动中的电机使用驱动的 wait_until_angle 轮询。

GUI 流程（工作流编码进按钮状态，不靠人记规则）：
  连接（软件限位固定 [0,360]）-> 回零 -> 寄存器归 0 后仍保持锁定，
  必须人工勾选"已听到蜂鸣"才解锁旋转（寄存器归零不代表零点已标记）->
  测量旋转 -> 断开/关窗时若 |角度|>0.5° 弹窗提醒结束回零。

工程教训（本轮踩坑记录）：
  1. "回读值变化" != "物理运动发生"：无绝对编码器的设备，到位/方向判据
     必须落在现场目视上；依据回读推断方向位、又拿回读当验证，是循环论证。
  2. worker 线程必须逐命令捕获异常：一次瞬态串口超时曾把整个 worker 打死，
     GUI 无声卡死（堆栈只在控制台）。异常只上报日志，线程不死。
  3. 模拟串口永不抛异常 -> 自检全绿真机炸。自检必须注入错误路径
     （如喂未知命令验证 worker 韧性）。
  4. 遗留观察：回零期间寄存器是否实时跟踪未验证；若回零校验超时但蜂鸣
     已响，直接人工勾选即可。
