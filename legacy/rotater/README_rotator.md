# 旋转杆 Python 接口使用说明

对应文件：`rotator_interface.py`。依据设备手册与用户提供的控制流程整理。

本文已纳入补充的 Read Deg 全部分支，以及“必须先运行 Initialize 才能控制”的实际使用说明。

**接入顺序：创建连接并初始化 → 按需完成机械归零 → 读取／设置角度 → 等待到位 → 执行测量 → 关闭连接。**

**工作模式（2026-09-08 用户确认）**：样品杆**单向旋转，角度范围 0-360°，到 360° 截止**；每次测量开始和结束**必须发送回零指令**，到达零点后控制器标记当前点为零点并发出蜂鸣（蜂鸣无法查询，以现场听觉为准）；回零后坐标从 0 重新计起，目标角度有效范围 [0, 360]。回零是独立指令 `[z,1,1501]`（速度 ≈10 °/s），不在普通旋转命令范围内，也不受软件限位约束。

正负角度换算已由补充分支确认。指令编码、角度解析、分片接收、软件限位和到位轮询已完成模拟测试。

**2026-09-08 实机标定（COM3，FTDI；两次有监督目视运动 +30°/−30°）**：
- 实际串口为 **COM3**；发令约定经目视确认：**`D=0` → 物理正转，`D=1` → 物理负转**（原有约定正确；曾依据寄存器回读改 `D=1` 对应非负，已作废回退）。
- **回读 = 控制器步数寄存器，无绝对编码器**（复位完成以蜂鸣为准）。其解码符号与物理方向相反，`decode_angle` 已按物理标定翻转：**寄存器 `D=1` 对应物理正角度**（与原实现截图读出分支相反，以目视为准）。
- 寄存器运动中**实时更新**，爬增速率 = 设定速度；**速度单位 °/s 实测确认**（30°@2°/s 用时 ~15 s）；标度 150.13878 确认（30° → 4504 counts）。
- ⚠ 曾观察到指令被接受但寄存器长时间不动的情形（疑似电机未执行/未上电）。寄存器不动时先确认电机上电/使能与联轴器，勿误判为停在零位。
- 到位判断基于步数寄存器（`wait_until_angle`），符号已标定、可用的到达判据，但仍非绝对编码器——重要实验前后建议各读一次并目视复核。
- ⚠ **查询时机约束（2026-09-08 用户实测）**：运动中/周期性查询 `[?]` 会**干扰控制器步数计数**（GUI 的 1 s 自动刷新曾把计数器搞串）。查询只应在静止时进行；运动到位判断采用"运动前静止读一次 → 按距离/速度计时等待 → 到点后单次校验"（`GUI/rotator_gui.py` 已按此实现），不要对运动中的电机使用 `wait_until_angle` 轮询。

## 1. 安装与导入

使用 Python 3.8 或更新版本。唯一外部依赖是 `pyserial`，不需要 NI-VISA 或 PyVISA。

```bash
python -m pip install pyserial
python -m serial.tools.list_ports
```

将 `rotator_interface.py` 放在主程序同一目录，或放进项目可导入的驱动包。

```python
from rotator_interface import Rotator, ProtocolError, encode_move, decode_angle
```

同一串口由一个程序、一个驱动实例持有。连接 Python 前关闭占用串口的其他程序或串口助手。

## 2. 初始化、归零与第一次使用

### 2.1 Initialize 在 Python 中对应什么？

原控制程序需要先完成串口初始化，再把 VISA 会话传给后续调用。Python 中相应操作已经包含在构造函数里：

```python
rot = Rotator("COM4")  # 打开串口并完成通信参数配置
```

不需要再调用一个 `initialize()` 方法，也不需要先运行其他控制程序。协议中没有独立握手字符串；用户所说的“先握手才能控制”对应先运行 Initialize。

| 原控制流程 | Python 对应 | 何时执行 |
|---|---|---|
| Initialize | `Rotator("COM4")` | 每次新建串口连接时执行一次 |
| VISA 会话沿线传递 | 后续方法使用同一个 `rot` 实例 | 整个测量期间复用 |
| Return To Zero ClockWise | `rot.return_to_zero_clockwise()` | 电机与样品杆连接后，按原说明需要机械归零时 |
| Read Deg | `rot.read_deg()` | 初始化和必要的归零完成后 |
| Rotate to Degree | `rot.rotate_to_degree(target, speed)` | 设置新的目标角度时 |
| 释放通信资源 | `rot.close()` 或退出 `with` | 测量结束时 |

串口重新打开时会重新配置参数；是否需要机械归零取决于设备是否仍有有效机械参考，不应简单绑定到每次打开串口。驱动不检测归零状态，也不自动触发归零。

### 2.2 机械归零是独立操作

按原说明，电机和样品杆连接后应先完成机械归零，然后才读取或设置角度。可以向下按控制器摇杆，也可以在建立 Python 连接后主动调用：

```python
rot.return_to_zero_clockwise()  # 会实际运动；仅发送指令，不等待机械寻零结束
```

通过控制器或实际运动状态确认归零完成，再运行后续读取／旋转操作。给定控制流程 没有提供专门的“寻零完成”查询，故示例不自动插入一个固定等待时间来假定归零完成。

### 2.3 完整的单次转动示例

以下假定已经完成机械归零。`COM4`、目标角度、速度及限位都是示例，应替换成实际设置。

```python
from rotator_interface import Rotator

with Rotator("COM4", angle_limits=(-90, 90), speed_limits=(0.1, 2.0)) as rot:
    initial_angle = rot.read_deg()
    print("初始角度：", initial_angle)

    rot.rotate_to_degree(target=30.0, speed=1.0)
    actual_angle = rot.wait_until_angle(
        30.0, tolerance=0.05, consecutive=3, interval=0.2, timeout=120.0
    )
    print("到位角度：", actual_angle)
```

`rotate_to_degree` 设置控制器坐标中的目标位置，不是相对当前角度再转动指定增量。它仅等待串口写操作完成；需要等待到位时必须单独调用 `wait_until_angle`。

## 3. 接口参考

### 创建连接

```python
Rotator(
    port,
    baudrate=115200,
    bytesize=8,
    parity="N",
    stopbits=1,
    timeout=2.0,
    angle_limits=None,
    speed_limits=None,
    xonxoff=False,
    rtscts=False,
    dsrdtr=False,
    transport=None,
)
```

除了 `port`，其余参数均为关键字参数。

| 参数 | 含义 |
|---|---|
| `port` | 如 Windows 的 `COM4`，或系统实际串口路径 |
| `baudrate/bytesize/parity` | 框图确认的配置为 `115200/8/"N"` |
| `stopbits`、流控参数 | 按原有实现SA 未接线端默认值迁移为 1 停止位、无流控 |
| `timeout` | 单次查询接收超时及串口写超时设置，单位秒，默认 2.0；不是机械运动超时 |
| `angle_limits` | `(最小角度, 最大角度)`，单位 °，边界包含；`None` 不设软件角度限位 |
| `speed_limits` | `(最小速度, 最大速度)`，按 °/s 使用，边界包含；`None` 不设软件速度范围 |
| `transport` | 测试用串口替身；实机使用时省略 |

实际串口读取设置为非阻塞，由驱动循环等待完整帧。不要把终止符关闭理解成发送时不带换行：发送指令仍带 `\r\n`。

### 方法和返回值

| 方法 | 返回值 | 行为 |
|---|---|---|
| `read_deg(timeout=None)` | `float`，单位 ° | 发起查询并解析角度；`timeout` 可覆盖本次查询等待时间 |
| `query_raw(timeout=None)` | `bytes` | 发起查询，返回通过格式校验的回包；不是无条件返回任意串口字节 |
| `return_to_zero_clockwise()` | `int`，已写字节数 | 发送机械归零命令，会触发运动，不等待完成 |
| `rotate_to_degree(target, speed)` | `int`，已写字节数 | 检查软件范围并发送目标位置命令，不等待完成 |
| `wait_until_angle(target, tolerance=0.05, consecutive=3, interval=0.2, timeout=120.0)` | `float`，最后一次到位角度 | 连续若干次读数满足误差要求后返回；除 `target` 外均为关键字参数 |
| `close()` | `None` | 关闭串口；`with` 退出时自动调用 |

`wait_until_angle` 中误差单位为 °，`interval` 和 `timeout` 单位为秒；实际查询周期包含通信耗时。一次查询超时或无效回包会立即向上抛异常，不会静默重试到整个运动超时结束。总等待时间也可能包含串口写入耗时，不是硬实时截止时间。

到位判断是 `abs(实际角度 - 目标角度) <= tolerance`，不作模 360 运算，不把 `+180°` 和 `-180°` 当成同一控制器位置。没有控制器 busy 位或机械归零完成标志，因此到位判断不能替代硬件完成状态，尤其不要据此直接认定机械寻零流程已经结束。

### 调试属性和纯函数

| 名称 | 用途 |
|---|---|
| `rot.last_reply` | 最近一次查询收到的字节；格式错误或超时后仍可查看部分回包 |
| `rot.last_discarded` | 最近查询前取走的已有字节，可检查旧回包或命令回显 |
| `encode_move(target, speed)` | 只生成指令字节，不连接设备；只检查字段容量，不检查实例的软件限位 |
| `decode_angle(raw)` | 只解析单个角度帧，允许首尾 CR/LF |

```python
from rotator_interface import encode_move, decode_angle

assert encode_move(90, 1) == b"[r,0,0150,013512]\r\n"
assert encode_move(-90, 10) == b"[r,1,1501,013512]\r\n"
assert decode_angle(b"[w,1,054050]\r\n") == -360.0
```

## 4. 接入已有测量程序

由主程序创建并持有一个 `Rotator` 实例，通过参数传给测量函数。不要在每个角度点重新开关串口。对于长期运行的程序，生命周期可写成：

```python
from rotator_interface import Rotator

rot = Rotator("COM4", angle_limits=(-90, 90))
try:
    # 此处开始时，应已完成必要的机械归零
    # 将 rot 传入已有测量函数，在整个序列中复用
    print(rot.read_deg())
finally:
    rot.close()
```

推荐顺序：设置温度/磁场并等待稳定 → 设置角度 → 等待到位 → 根据需要等待热稳定 → 执行电学测量 → 保存实际角度及数据。

下面的函数可直接放进主程序。`measure_one_point` 是你已有测量函数的回调，无参数，返回包含电压、电流、电阻等结果的字典。`on_record` 接收一条记录并写入你现有的 CSV 或数据库。

```python
import time

def scan_angles(rot, angles, measure_one_point, on_record,
                *, speed=1.0, settle_s=1.0, move_timeout_s=120.0):
    for target in angles:
        rot.rotate_to_degree(target, speed)
        arrived = rot.wait_until_angle(
            target, tolerance=0.05, consecutive=3,
            interval=0.2, timeout=move_timeout_s,
        )
        time.sleep(settle_s)  # 示例值；按转动后的温度恢复情况调整
        angle_before = rot.read_deg()
        electrical_data = measure_one_point()
        angle_after = rot.read_deg()
        record = {
            "timestamp_unix_s": time.time(),
            "angle_target_deg": float(target),
            "angle_arrived_deg": arrived,
            "angle_before_deg": angle_before,
            "angle_after_deg": angle_after,
            "electrical": electrical_data,
        }
        on_record(record)
```

上述记录包含嵌套的 `electrical` 字典；如果使用 CSV，在 `on_record` 中将电学字段展开。温度、磁场设定值及实际读数也应由你的主程序加入记录。只在整条记录完整时提交；发生异常后终止当前序列，不把没有确认到位的测量当成有效角度点。

长时间 I–V 或微分谱测量可保留测量前后两次角度，用于判断漂移。`settle_s` 只是固定等待，不代表已经确认 VTI 温度稳定；有温度稳定判据时使用已有温控等待函数。

## 5. GUI、线程和异步程序

`read_deg`、`wait_until_angle` 和串口写操作均为同步调用。Tkinter/PyQt 界面中放到专用工作线程执行，并通过队列或信号更新界面，避免阻塞 GUI 主线程。

实例内部的锁只串行化单次串口操作，**不会锁住“发运动命令 → 等待到位 → 测量”的整个过程**。因此整个序列应由一个设备工作线程或任务队列独占执行。后台角度刷新也交由同一队列，不要让其他线程在等待过程中插入新目标。

`asyncio` 程序同样应使用单一设备工作者串行执行这些同步方法。取消等待任务、关闭窗口或关闭串口均不等同停止电机；当前驱动没有从原实现恢复出的停止命令。

## 6. 异常处理与排查

### 没有反应时，按这个顺序检查

1. 确认主程序已经成功执行 `Rotator(实际串口)`，而不是只导入了模块。`import` 不会连接设备。
2. 确认端口与 原控制程序使用的设备一致，且 其他程序／串口助手已释放端口。
3. 确认没有覆盖掉默认的 `115200、8N1、无流控` 配置。原控制程序标签上的 9600 不是实际接入值。
4. 确认电机和样品杆已按原说明完成机械归零，然后先运行 `read_deg()`。
5. 如果查询失败，记录异常、`last_reply` 和 `last_discarded`。读取成功后再检查运动参数和目标角度；当前目标与当前位置相同时，不发生可见转动是正常情况。

### 异常类型

| 异常 | 常见原因与处理 |
|---|---|
| `ProtocolError` | 回包结构、方向位、数字字段不匹配，或出现回显/多帧；记录 `last_reply`，与实际串口数据核对。它是 `ValueError` 子类，应先捕获 |
| `TimeoutError` | 未收到完整角度帧，或到位等待超时；区分通信问题和仍在运动，停止后续测量 |
| `ValueError` | 参数非有限数、速度无效、超出软件范围或字段容量；修正调用参数 |
| `serial.SerialException` | 串口不存在、被占用、设备断开等 |
| `serial.SerialTimeoutException` / `OSError` | 写超时、写入不完整或底层 I/O 异常；不自动重发运动命令 |

异常记录示例，接收一个已经连接的驱动实例：

```python
import logging
import serial
from rotator_interface import ProtocolError

logger = logging.getLogger(__name__)

def move_and_confirm(rot, target, speed):
    try:
        rot.rotate_to_degree(target, speed)
        return rot.wait_until_angle(target)
    except (ProtocolError, TimeoutError, serial.SerialException, OSError):
        logger.exception(
            "旋转/通信失败 target=%s speed=%s reply=%r previous=%r",
            target, speed, rot.last_reply, rot.last_discarded,
        )
        raise  # 交给主程序结束本次序列；不要返回 0 度或继续测量
```

查询前驱动会取走当时已存在的字节，保存到 `last_discarded`，减少旧数据干扰。协议没有事务编号，不能保证识别所有迟到回包。若上一次查询超时，不应立刻高速连续重试，应先检查收发数据与设备状态。

格式错误时优先查看 `last_reply`，不要反复调用 `query_raw()` 试图读取同一份错误数据——它会发送新的查询。

## 7. 坐标、归零和实验边界

机械归零与普通 `rotate_to_degree(0, speed)` 是不同操作。前者调用寻零命令，后者仅转至已有坐标系的零位置。机械归零不受 `angle_limits` 约束。

驱动读写的是控制器角度，不知道样品面内/面外方向、晶轴方向或磁场方向。如实验角度零点对应控制器 `theta0`，可在主程序定义：

```python
# sign 应通过实测设为 +1 或 -1
controller_target = theta0 + sign * experiment_angle
experiment_actual = (controller_actual - theta0) / sign
```

软件角度范围应针对 `controller_target` 设置。驱动不会自动选最短旋转路径，也不会自动折返或展开跨圈角度。

`close()`、等待超时和 Python 异常都不会发送停止指令。接口未提供磁场/温度联锁，主程序应沿用现有实验允许的旋转条件。协议允许的数值宽度不是样品杆允许的机械行程。

## 8. 协议备查

所有发送字符串使用 ASCII 编码，尾部为真正的 CR LF 两个字节，即 Python 中的 `b"\r\n"`，不是四个可见字符。

| 功能 | 格式 |
|---|---|
| 查询 | `[?]\r\n` |
| 角度回包主体 | `[w,D,NNNNNN]`，12 字节 |
| 顺时针归零 | `[z,1,1501]\r\n` |
| 转至目标位置 | `[r,D,SSSS,NNNNNN]\r\n` |

运动编码：`D=0` 对应非负目标，`D=1` 对应负目标（原有约定；2026-09-08 曾依据不可靠的寄存器回读改动，已作废回退）；`S=round(speed*150.13878)`，四位补零；`N=round(abs(target)*150.13878)`，六位补零。驱动拒绝超过字段宽度的值，速度换算后必须至少为 1。

读回角度：寄存器 `D=1` 时 `angle=+N*360/54050`，`D=0` 时 `angle=-N*360/54050`（2026-09-08 物理标定，与原实现截图读出分支相反）。每个读回计数约为 `0.00666°`，这是编码分辨率，不代表机械精度。

取整使用 Python `round`，与 [NI Round To Nearest](https://www.ni.com/docs/en-US/bundle/labview-api-ref/page/functions/round-to-nearest.html) 的最近整数、半整数取偶数规则对应。

Read Deg 中的 `[w,` 用于校验接收到的字符串开头，不会发送 `[w,...]` 命令。

## 9. 与原 Read Deg 操作 的行为差异

补充分支确认：原实现发送查询并等待 20 ms 后，只有串口可读字节数**恰好等于 12**才读取；回包格式检查失败时也不执行角度计算。这些空分支不更新角度指示器，可能保留旧显示，而且没有在图中新增协议错误。

Python 版本有意采用以下行为，便于自动测量识别失败：

| 情况 | Python 行为 |
|---|---|
| 回包分多次到达 | 在查询超时内拼接，等待完整主体 |
| 回包带 CR/LF | 接受首尾 CR/LF，不要求串口缓存总字节数恰好为 12 |
| 主体不足 12 字节直到超时 | 抛 `TimeoutError`，部分字节保存在 `last_reply` |
| 主体结构错误、未知方向位、非六位数字或粘连多帧 | 抛 `ProtocolError` |
| 读取失败 | 不返回旧角度，不返回伪造的 0° |

方向位为 `1` 时原实现选 `+360`，否则选 `−360`，最终都除以 `−54050`。Python 仅接受已知的 `0/1` 方向位，比原实现的“不是 1”分支更严格。

**读出符号差异（2026-09-08 物理标定）**：按原实现分支，`D=1` 解码为负角度；但实机目视标定表明寄存器 `D=1` 对应物理正角度（发令 `D=0` 正转后寄存器记 `D=1`）。`decode_angle` 已按物理方向翻转，与原实现显示约定相反——若需复现原实现显示值，把读数取负即可。

## 10. 首次实机核对后即可纳入测量序列

在设备允许的角度和条件下，核对三项：当前角度与原控制程序一致；正、负小角度目标及回读一致；给定速度下的转动用时合理。无需为了验证符号或一圈换算常数实际转动一整圈。

协议字段与数值换算已整理完毕。仍需实机确认的是通信表现、速度单位与机械行为，而不是再额外寻找 Initialize 之前的握手命令。
