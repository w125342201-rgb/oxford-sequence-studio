r"""旋转杆串口驱动：依据设备手册与用户提供的控制流程整理，未实机验证。

安装：python -m pip install pyserial

协议（ASCII，发送帧均以 CR LF 结束）
  查询：[?]\r\n
  回包：[w,D,NNNNNN]（12 字节主体，允许尾随 CR LF）
        控制器步数寄存器（无绝对编码器，复位完成以蜂鸣为准）。
        2026-09-08 物理标定（两次有监督目视运动，+30° 与 −30°）：
        发令 D=0 → 物理正转（原实现约定正确）；寄存器 D=1 对应物理正角度，
        与原实现截图的读出分支相反，decode_angle 已按物理方向翻转。
        寄存器运动中实时更新，速率=设定速度（°/s，已实测确认）；
        标度 150.13878 经 30°→4504 counts 确认。
        ⚠ 曾观察到指令被接受但寄存器长时间不动的情形（电机未执行？），
        寄存器不动时先确认电机上电/使能，勿误判为已到位在零位。
  顺时针机械归零：[z,1,1501]\r\n
  转至目标角度：[r,D,SSSS,NNNNNN]\r\n
        D=0 if target>=0 else 1（维持原有约定。2026-09-08 曾依据回读改为
        D=1 对应非负，后经用户现场确认作废：样品杆复位有蜂鸣、无绝对编码器，
        [?] 回读是控制器寄存器而非真实角度，不能作为方向位证据。）
        S=round(speed*150.13878)，N=round(abs(target)*150.13878)
        速度按 °/s 使用（由相同角度换算系数推定，需实测用时核对）。
        S 四位补零，N 六位补零。round 为最近整数、半整数取偶数。
        参考 NI 文档「Round To Nearest」：最近整数、半整数取偶数。

初始化：Rotator("COM4") 即打开串口并配置通信参数，对应初始化步骤。
无需单独调用 initialize() 或先运行其他控制程序；协议中没有独立握手字符串。
同一个实例在整个测量序列中复用；导入模块不会连接设备或发送命令。
115200、8、无校验、读取终止符关闭，依据设备实测。
停止位=1、无流控按 VISA Configure Serial Port 未接线端的默认设置迁移；
Python 超时默认 2 s，接收轮询可等待分片，不照搬原实现仅等待 20 ms 的做法。
原实现的 [w, 是回包格式校验，不是发送指令。
原实现在字节数不等于 12 或格式校验失败时跳过角度更新；本驱动等待完整帧，
失败抛出 TimeoutError/ProtocolError，不将旧角度或 0 度当作新读数返回。
读取和发送换算常数分别保留 54050/360 和 150.13878，不擅自统一。

用法（必须先按原说明完成机械归零；打开端口本身不归零）：
    with Rotator('COM4', angle_limits=(-180, 180)) as rot:
        print(rot.read_deg())
        rot.rotate_to_degree(30, speed=1)  # 发送后立即返回
        rot.wait_until_angle(30, timeout=60)  # 按读回角度判断到位

需要机械归零时主动调用 rot.return_to_zero_clockwise()；它会实际运动。
发送成功不代表运动完成；本协议没有从所给控制流程 恢复出忙碌/停止命令。
wait_until_angle 仅检查连续角度读数，不是控制器 busy 位，也不是紧急停止。
等待超时不会停止电机；close 也仅关闭串口。不要在运动命令超时后自动重发。
角度限位默认不设；angle_limits/speed_limits 由使用者按样品杆设定，
协议字段容量不是机械允许范围。机械归零不受目标角度软件限位控制。
本驱动以控制器坐标为准，不自动把机械零位映射成样品面内/面外角度。

接口：Rotator(...) / read_deg() / query_raw() /
return_to_zero_clockwise() / rotate_to_degree(target, speed) /
wait_until_angle(target, ...) / close()。
纯函数 encode_move 和 decode_angle 可脱离设备检查协议。
"""

import math
import re
import threading
import time
from typing import Optional, Tuple

__version__ = "1.1.0"
__all__ = ["Rotator", "ProtocolError", "encode_move", "decode_angle"]

# 发送与读取分别保留原实现的两个换算常数。

SCALE = 150.13878
COUNTS_PER_TURN = 54050
QUERY = b'[?]\r\n'
HOME_CLOCKWISE = b'[z,1,1501]\r\n'
REPLY_LENGTH = 12
MAX_REPLY_BYTES = 4096
POLL_INTERVAL = 0.002
_REPLY = re.compile(rb'\[w,([01]),([0-9]{6})\]')


class ProtocolError(ValueError):
    """回包结构错误；不会把错误当作零度。"""


def _finite(value, name: str) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f'{name} 必须为有限数')
    return value


def decode_angle(raw: bytes) -> float:
    """严格校验一个角度帧，允许首尾 CR/LF，不接受未知方向位。"""
    if not isinstance(raw, bytes):
        raise TypeError('raw 必须为 bytes')
    match = _REPLY.fullmatch(raw.strip(b'\r\n'))
    if match is None:
        raise ProtocolError(f'无效角度回包：{raw!r}')
    direction, counts = match.groups()
    # 2026-09-08 物理标定（两次有监督目视运动）：D=1 对应物理正角度，
    # 与原实现截图的读出分支相反，以现场目视为准；详见文件头说明。
    multiplier = -360 if direction == b'1' else 360
    return int(counts) * multiplier / (-COUNTS_PER_TURN)


def encode_move(target: float, speed: float) -> bytes:
    """生成转至目标角度指令；仅验证字段容量，不代表机械可达。"""
    target = _finite(target, 'target')
    speed = _finite(speed, 'speed')
    if speed <= 0:
        raise ValueError('speed 必须为正数')
    if speed > 10000 / SCALE or abs(target) > 1000000 / SCALE:
        raise ValueError('参数超过协议字段容量')
    velocity = round(speed * SCALE)
    position = round(abs(target) * SCALE)
    if not 1 <= velocity <= 9999:
        raise ValueError('速度换算后必须在 0001..9999 之间')
    if not 0 <= position <= 999999:
        raise ValueError('目标角度换算后超过六位字段容量')
    direction = 0 if target >= 0 else 1
    return f'[r,{direction},{velocity:04d},{position:06d}]\r\n'.encode('ascii')


def _limits(bounds, name: str) -> Optional[Tuple[float, float]]:
    if bounds is None:
        return None
    if len(bounds) != 2:
        raise ValueError(f'{name} 必须为 (min,max)')
    lo, hi = (_finite(x, name) for x in bounds)
    if lo > hi:
        raise ValueError(f'{name} 下限大于上限')
    return lo, hi


class Rotator:
    """一个持续打开的旋转杆串口会话。

    构造时完成通信初始化，不自动机械归零。port 为实际串口名；timeout
    为通信超时（秒）；angle_limits/speed_limits 为可选的闭区间元组。
    角度单位为度，速度按度/秒实现（2026-09-08 实测确认：30°@2°/s 用时 ~15 s，
    寄存器爬增速率 2°/s）。

    实例锁只保护单次串口操作。整段运动、等待和电学测量应由同一个
    设备工作线程串行执行，避免其他线程在等待过程中插入新目标。
    transport 为测试注入接口，需提供 in_waiting、read、write、close，
    其中 read 应非阻塞。实际设备调用时省略此参数。
    """

    def __init__(self, port: str, *, baudrate=115200, bytesize=8, parity='N',
                 stopbits=1, timeout=2.0, angle_limits=None, speed_limits=None,
                 xonxoff=False, rtscts=False, dsrdtr=False, transport=None) -> None:
        self.timeout = _finite(timeout, 'timeout')
        if self.timeout <= 0:
            raise ValueError('timeout 必须大于零')
        self.angle_limits = _limits(angle_limits, 'angle_limits')
        self.speed_limits = _limits(speed_limits, 'speed_limits')
        self._lock = threading.RLock()
        self.last_reply = b''
        self.last_discarded = b''
        if transport is None:
            import serial
            transport = serial.Serial(
                port=port, baudrate=baudrate, bytesize=bytesize, parity=parity,
                stopbits=stopbits, timeout=0, write_timeout=self.timeout,
                xonxoff=xonxoff, rtscts=rtscts, dsrdtr=dsrdtr)
        self._serial = transport

    def _write(self, command: bytes) -> int:
        count = self._serial.write(command)
        if count != len(command):
            raise IOError(f'指令写入不完整：{count}/{len(command)}；不会自动重发')
        return count

    def query_raw(self, *, timeout: Optional[float] = None) -> bytes:
        """发起一次新查询，拼接分片直至取得 12 字节主体；返回原始回包。

        查询前取走已存在的旧字节，存入 last_discarded，避免沿用上次角度。
        超时时已接收内容保存在 last_reply。不要让其他程序并发占用串口。
        """
        duration = self.timeout if timeout is None else _finite(timeout, 'timeout')
        if duration <= 0:
            raise ValueError('timeout 必须大于零')
        with self._lock:
            self.last_discarded = self._serial.read(self._serial.in_waiting)
            self.last_reply = b''
            self._write(QUERY)
            deadline = time.monotonic() + duration
            received = bytearray()
            while time.monotonic() < deadline:
                count = self._serial.in_waiting
                if count:
                    received.extend(self._serial.read(count))
                    self.last_reply = bytes(received)
                    if len(received) > MAX_REPLY_BYTES:
                        raise ProtocolError('查询回包超过 4096 字节')
                    body = bytes(received).strip(b'\r\n')
                    if len(body) >= REPLY_LENGTH:
                        decode_angle(body)
                        return bytes(received)
                time.sleep(POLL_INTERVAL)
            raise TimeoutError(f'角度回包超时：{bytes(received)!r}')

    def read_deg(self, *, timeout: Optional[float] = None) -> float:
        """返回本次查询的控制器角度（度）；通信/格式失败直接抛异常。

        timeout=None 使用构造时的通信超时；不会返回上次成功的缓存值。
        """
        return decode_angle(self.query_raw(timeout=timeout))

    def return_to_zero_clockwise(self) -> int:
        """发送顺时针机械寻零命令，返回写入字节数。

        会实际运动；不等同设置已有坐标的 0 度，不受 angle_limits 限制。
        不等待寻零结束、不自动重复发送；所给控制流程 无寻零完成状态查询。
        """
        with self._lock:
            return self._write(HOME_CLOCKWISE)

    def rotate_to_degree(self, target: float, speed: float) -> int:
        """以给定速度转至控制器目标坐标，返回已写字节数。

        target 是目标位置（度），不是相对增量；speed 为正数。
        发送后不等待到位、不作模 360 处理、不自动选择最短路径。
        超范围抛 ValueError；写入失败向上抛异常，不自动重发。
        """
        target, speed = _finite(target, 'target'), _finite(speed, 'speed')
        for name, value, bounds in [('target', target, self.angle_limits),
                                    ('speed', speed, self.speed_limits)]:
            if bounds is not None and not bounds[0] <= value <= bounds[1]:
                raise ValueError(f'{name} 超出设定范围 {bounds}')
        command = encode_move(target, speed)
        with self._lock:
            return self._write(command)

    def wait_until_angle(self, target: float, *, tolerance=0.05,
                         consecutive=3, interval=0.2, timeout=120.0) -> float:
        """连续读数满足误差后返回；超时仅抛异常，不会停止电机。

        不对角度作模 360 处理，不把 +180 和 -180 自动视作相同坐标。
        tolerance 单位为度；interval、timeout 单位为秒。
        单次查询失败立即抛出，不静默重试到总超时结束。
        连续读数满足条件不等同控制器 busy 位或机械寻零完成状态。
        ⚠ 2026-09-08 用户实测：本控制器在运动中被查询 [?] 会干扰其步数
        计数（GUI 的 1 s 周期轮询曾把计数器搞串）。查询应限于静止时；
        运动到位判断建议采用"运动前静止读一次 → 按距离/速度计时等待 →
        到点后单次校验"，避免运动中轮询。
        """
        target = _finite(target, 'target')
        tolerance = _finite(tolerance, 'tolerance')
        interval = _finite(interval, 'interval')
        timeout = _finite(timeout, 'timeout')
        if tolerance <= 0 or interval <= 0 or timeout <= 0:
            raise ValueError('tolerance、interval、timeout 必须为正数')
        if isinstance(consecutive, bool) or not isinstance(consecutive, int) or consecutive < 1:
            raise ValueError('consecutive 必须为正整数')
        deadline = time.monotonic() + timeout
        matched = 0
        angle = None
        while (remaining := deadline - time.monotonic()) > 0:
            angle = self.read_deg(timeout=min(remaining, self.timeout))
            matched = matched + 1 if abs(angle - target) <= tolerance else 0
            if matched >= consecutive:
                return angle
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
        raise TimeoutError(f'未确认到位：target={target}, last={angle}；未发送停止指令')

    def close(self) -> None:
        """关闭串口；不发送停止命令。"""
        with self._lock:
            self._serial.close()

    def __enter__(self) -> "Rotator":
        return self

    def __exit__(self, *_) -> None:
        self.close()
