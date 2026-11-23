# -*- coding: utf-8 -*-
"""
matrix_switch.py
================
Multi-field M6x24 矩阵开关的 Python 驱动接口。

硬件背景
--------
* 设备型号：Multi-field M6x24 矩阵式开关（前面板铭牌 "Coaxial Matrix - 6X24"）
* 拓扑结构：6 路同轴输出（仪表侧，行）x 24 路输入（样品侧，列）
* 输出通道标签（依设备实际丝印）：A B C D E F，对应内部编号 1..6
* 输入通道编号：1..24
* 切换元件：6x24 个双通道低噪声继电器，实现信号线与屏蔽线同步切换

设备外观（依实拍前面板，见附图）
------------------------------------
* 前面板：6 个 BNC 同轴输出端子集中于 OUTPUT 蓝色标签条区域，
  自上而下丝印 A B C D E F；触屏可本地切换。
* 前面板右侧 6×24 继电器状态点阵指示器（行 A-F，列 1-24），
  实时显示当前闭合位置。`MatrixSwitch.state_matrix()` 返回的
  6×24 布尔矩阵与该点阵布局严格对应。
* 后面板：26 芯多芯输入端子（1-24 信号、25/26 备用）、GPIB/DB9 通信端 D1、接地端、220V 电源。
* 通信端 D1：上位机远程控制。本设备实际为 **GPIB**（VISA 资源名寻址），
  旧版可配 DB9+USB-RS232。

通信接口与协议（依 矩阵开关命令.txt + 说明书 + 实测确认）
----------------------------------------------------------------------
* 物理接口：**GPIB**（VISA 资源名，如 ``GPIB0::23::INSTR``），由 NI-VISA 后端承载；
  亦兼容 USB-RS232（DB9，串口参数 115200/8/N/1，无流控）作为备选后端。
  （通过 VISA 资源 ``?*::(INSTR|SOCKET)`` 寻址，
   初始化按串口或 GPIB 配置 VISA 会话）
* 命令为“整帧绝对状态”——每次发送的都是 24 列的完整连接状态，
  而非增量指令。设备无回读（仅 VISA Write + write buffer）。
* 帧格式（以 CRLF 结尾）：
      [nnnnnnnn,nnnnnnnn,nnnnnnnn]\r\n
  - 三段，每段 8 位十进制字符，取值范围 '0'..'6'
  - 段 1/2/3 分别描述输入列 1-8 / 9-16 / 17-24
  - 第 i 位（1-indexed）的数字 = 该输入端所连的输出行号；'0' = 断开
  - 程序框图中存在 '[' ',' ']' 字符串常量与 4 个 %.0f 格式化节点，
    印证了“数值 -> 整数字符串 -> 拼接”的构造方式

示例（与 矩阵开关命令.txt 一致）：
  断开所有行列        -> [00000000,00000000,00000000]\r\n
  闭合第一行第一列     -> [10000000,00000000,00000000]\r\n
  第二行第四列 +       -> [00023000,00000000,00000000]\r\n
  第三行第五列

依赖
----
* GPIB 模式：``pyvisa`` + 系统 NI-VISA 后端（Windows 常见科研机已装）
* RS232 模式：``pyserial``
* 两者均缺失时，``simulate=True`` 仍可离线运行与测试
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Mapping, Optional, Tuple, Union

try:  # 串口后端可选依赖；GPIB 模式无需
    import serial
    from serial.tools import list_ports
except Exception:  # pragma: no cover
    serial = None
    list_ports = None

# --- 硬件常量 -------------------------------------------------------------
N_ROWS: int = 6          # 输出（行）数：A B C D E F
N_COLS: int = 24         # 输入（列）数
SEG_LEN: int = 8         # 每段的列数
N_SEGS: int = N_COLS // SEG_LEN  # 段数（3）

# 输出通道标签（设备实际丝印）；索引 i 对应行号 i+1
ROW_LABELS = ("A", "B", "C", "D", "E", "F")

# 默认串口参数（来自 矩阵开关命令.txt，仅 RS232 后端使用）
BAUDRATE: int = 115200
BYTESIZE: int = 8
PARITY: str = "N"
STOPBITS: int = 1

DEFAULT_TIMEOUT: float = 2.0
TERMINATOR = b"\r\n"     # 帧结束符；可配置以适配个别固件


class MatrixError(Exception):
    """矩阵开关相关错误（参数非法、状态冲突、通信失败）。"""


def _norm_row(row: Union[int, str]) -> int:
    """将行号（1..6）或标签（A/B/C/V/W/X）归一化为 1..6 整数。"""
    if isinstance(row, str):
        r = row.strip().upper()
        if r not in ROW_LABELS:
            raise MatrixError(f"非法输出标签 {row!r}，应为 {ROW_LABELS}")
        return ROW_LABELS.index(r) + 1
    if not isinstance(row, int):
        raise MatrixError(f"行号必须为 int 或标签字符串，收到 {row!r}")
    if not 1 <= row <= N_ROWS:
        raise MatrixError(f"行号越界：{row}（允许 1..{N_ROWS}）")
    return row


def _norm_col(col: int) -> int:
    """校验输入列号（1..24）并返回。"""
    if not isinstance(col, int) or isinstance(col, bool):
        raise MatrixError(f"列号必须为 int，收到 {col!r}")
    if not 1 <= col <= N_COLS:
        raise MatrixError(f"列号越界：{col}（允许 1..{N_COLS}）")
    return col


# ---------------------------------------------------------------------------
# 纯函数层：帧构造与解析（与传输层无关，可离线单元测试）
# ---------------------------------------------------------------------------
def build_frame(state: Mapping[int, int], terminator: bytes = TERMINATOR) -> bytes:
    """
    由连接状态构造命令帧字节串。

    Parameters
    ----------
    state : Mapping[int, int]
        列 -> 行 的映射；行取 0（断开）或 1..6。未列出的列视为断开。
    terminator : bytes
        帧结束符，默认 CRLF。

    Returns
    -------
    bytes
        形如 b'[10000000,00000000,00000000]\r\n'
    """
    full = {c: 0 for c in range(1, N_COLS + 1)}
    for col, row in state.items():
        col = _norm_col(col)
        if row is None or row == 0:
            full[col] = 0
        else:
            full[col] = _norm_row(row)

    segs = []
    for s in range(N_SEGS):
        cols = range(s * SEG_LEN + 1, s * SEG_LEN + 1 + SEG_LEN)
        segs.append("".join(str(full[c]) for c in cols))

    frame = "[" + ",".join(segs) + "]"
    return frame.encode("ascii") + terminator


def parse_frame(text: str) -> Dict[int, int]:
    """
    解析设备返回的/手写的帧文本为 列 -> 行 映射（不含断开列）。

    Parameters
    ----------
    text : str
        形如 '[00023000,00000000,00000000]'，可带首尾空白或 CRLF。

    Returns
    -------
    Dict[int, int]
        {列: 行}，仅包含已闭合（值非 0）的连接。
    """
    t = text.strip()
    if not (t.startswith("[") and t.endswith("]")):
        t = t.strip("\r\n")
    if not (t.startswith("[") and t.endswith("]")):
        raise MatrixError(f"帧格式非法（缺少方括号）：{text!r}")
    body = t[1:-1]
    parts = body.split(",")
    if len(parts) != N_SEGS:
        raise MatrixError(f"帧应有 {N_SEGS} 段，实得 {len(parts)} 段：{text!r}")
    mapping: Dict[int, int] = {}
    for s, seg in enumerate(parts):
        if len(seg) != SEG_LEN:
            raise MatrixError(f"第 {s+1} 段长度应为 {SEG_LEN}，实得 {len(seg)}：{text!r}")
        for i, ch in enumerate(seg):
            if ch not in "0123456":
                raise MatrixError(f"非法字符 {ch!r}（应为 0..6）：{text!r}")
            val = int(ch)
            if val:
                mapping[s * SEG_LEN + i + 1] = val
    return mapping


# ---------------------------------------------------------------------------
# 设备驱动类
# ---------------------------------------------------------------------------
class MatrixSwitch:
    """
    M6x24 矩阵开关驱动（传输层支持 GPIB / RS232，默认 GPIB）。

    设计要点
    --------
    * **整帧绝对状态**：设备只接受 24 列完整状态帧，因此本类内部维护
      ``_state`` 状态缓存；任何单次 ``connect`` / ``disconnect`` 都会
      在本地更新缓存后，将整帧重新下发。
    * **短路保护**：默认禁止“同一输出行同时连接多个输入列”
      （``allow_row_sharing=False``），避免样品间意外短路；范德堡切换等
      场景可临时开启，但仍需保证同一时刻不冲突。
    * **模拟模式**：``simulate=True`` 时不打开任何硬件连接，仅校验并缓存帧，
      便于离线调试与自动化测试；调用 ``last_frame`` 可查看最近下发帧。

    Examples
    --------
    >>> sw = MatrixSwitch(resource="GPIB0::23::INSTR", simulate=True)
    >>> sw.connect("A", 1)            # 输出A(行1) <- 输入1
    >>> sw.connect(3, 5)              # 输出C(行3) <- 输入5
    >>> sw.last_frame
    b'[10003000,00000000,00000000]\r\n'
    >>> sw.disconnect(1)
    >>> sw.open_all()                 # 全部断开
    """

    def __init__(
        self,
        resource: Optional[str] = None,
        port: Optional[str] = None,
        backend: str = "visa",
        baudrate: int = BAUDRATE,
        timeout: float = DEFAULT_TIMEOUT,
        terminator: bytes = TERMINATOR,
        allow_row_sharing: bool = False,
        simulate: bool = False,
    ) -> None:
        """
        Parameters
        ----------
        resource : str, optional
            VISA 资源名（GPIB 模式必需），如 'GPIB0::23::INSTR'。
            可用 ``list_visa_resources()`` 查询。
        port : str, optional
            RS232 串口号（serial 模式必需），如 'COM3'。
        backend : {'visa', 'serial'}
            传输后端，默认 'visa'（GPIB）。
        baudrate : int
            仅 serial 模式：波特率，默认 115200。
        timeout : float
            通信超时（秒）。
        terminator : bytes
            帧结束符，默认 CRLF。
        allow_row_sharing : bool
            是否允许同一输出行同时连接多个输入列（默认禁止，防短路）。
        simulate : bool
            模拟模式：不触硬件，仅校验/缓存帧。
        """
        if backend not in ("visa", "serial"):
            raise MatrixError(f"backend 必须为 'visa' 或 'serial'，收到 {backend!r}")
        self.backend = backend
        self.resource = resource
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.terminator = terminator
        self.allow_row_sharing = allow_row_sharing
        self.simulate = simulate

        # 列 -> 行 映射；行=0 表示断开
        self._state: Dict[int, int] = {c: 0 for c in range(1, N_COLS + 1)}
        self._instr = None          # VISA 句柄（visa 后端）
        self._ser = None            # Serial 句柄（serial 后端）
        self.last_frame: Optional[bytes] = None
        # 序列化状态缓存变更与帧下发。测量线程（route_4terminal/set_mapping）
        # 与 STOP 线程（open_all）会并发调用；无锁时两个线程同时改写
        # self._state 或向同一 VISA/串口句柄写帧，会导致状态缓存错乱与
        # 总线帧交叉。
        self._lock = threading.RLock()

    # ---- 连接管理 -------------------------------------------------------
    def open(self, sync: bool = True) -> "MatrixSwitch":
        """建立硬件连接（模拟模式跳过）。已连接则直接返回。

        sync=True（默认）时在连接建立后立即把本地缓存状态作为整帧下发一次。
        设备无回读，若其被断电重启或在前面板手动切换过，真实继电器状态
        会与缓存不一致；主动下发一帧可强制硬件与缓存对齐（初始缓存为全 0，
        即"全部断开"的安全态）。传 sync=False 可跳过（例如明确知道设备
        状态且不希望扰动时）。
        """
        with self._lock:
            if self.simulate:
                return self
            if self.backend == "visa":
                if self._instr is not None:
                    return self
                if self.resource is None:
                    raise MatrixError("GPIB/VISA 模式未指定 resource，例如 'GPIB0::23::INSTR'。")
                try:
                    import pyvisa
                except ImportError as e:
                    raise MatrixError(
                        "未找到 pyvisa，请先安装：pip install pyvisa（并确保系统已装 NI-VISA）。"
                    ) from e
                rm = pyvisa.ResourceManager()
                self._instr = rm.open_resource(self.resource)
                # 帧自带终止符，禁用后端追加，保证不同接口行为一致
                self._instr.write_termination = ""
                self._instr.timeout = max(1, int(self.timeout * 1000))  # VISA 以毫秒计
            else:  # serial
                if self._ser is not None and self._ser.is_open:
                    return self
                if serial is None:
                    raise MatrixError("未找到 pyserial，请先安装：pip install pyserial。")
                if self.port is None:
                    raise MatrixError("RS232 模式未指定 port，例如 'COM3'。")
                self._ser = serial.Serial(
                    port=self.port,
                    baudrate=self.baudrate,
                    bytesize=BYTESIZE,
                    parity=PARITY,
                    stopbits=STOPBITS,
                    timeout=self.timeout,
                )
                self._ser.reset_input_buffer()
                self._ser.reset_output_buffer()
            if sync:
                self.apply()  # 强制硬件状态与本地缓存对齐（默认全 0 = 全断开）
            return self

    def close(self) -> None:
        """关闭硬件连接（模拟模式跳过）。"""
        with self._lock:
            if self.backend == "visa":
                if self._instr is not None:
                    try:
                        self._instr.close()
                    except Exception:
                        pass
                self._instr = None
            else:
                if self._ser is not None and self._ser.is_open:
                    self._ser.close()
                self._ser = None

    def __enter__(self) -> "MatrixSwitch":
        return self.open()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # ---- 帧下发 ---------------------------------------------------------
    def _send(self, frame: bytes) -> None:
        with self._lock:
            self.last_frame = frame
            if self.simulate:
                return
            if self.backend == "visa":
                if self._instr is None:
                    self.open()
                self._instr.write(frame.decode("ascii"))
            else:
                if self._ser is None or not self._ser.is_open:
                    self.open()
                self._ser.write(frame)
            # 给继电器动作留一点时间（典型继电器切换 < 5 ms，留余量）
            time.sleep(0.01)

    def apply(self) -> bytes:
        """将当前缓存状态作为整帧下发，返回该帧字节串。"""
        with self._lock:
            frame = build_frame(self._state, self.terminator)
            self._send(frame)
            return frame

    # ---- 单通道操作 -----------------------------------------------------
    def connect(self, row: Union[int, str], col: int) -> bytes:
        """
        闭合“某输出行 <-> 某输入列”的继电器。

        Parameters
        ----------
        row : int 或 str
            输出行号 1..6，或标签 'A'..'X'。
        col : int
            输入列号 1..24。

        Returns
        -------
        bytes
            本次下发的整帧。
        """
        with self._lock:
            if not isinstance(row, (int, str)) or isinstance(row, bool):
                raise MatrixError("row 必须为 int 或标签字符串")
            row = _norm_row(row)
            col = _norm_col(col)

            cur = self._state[col]
            if cur not in (0, row):
                raise MatrixError(
                    f"冲突：列 {col} 当前已连至行 {cur}，无法再连行 {row}（请先断开）。"
                )

            if not self.allow_row_sharing:
                others = [c for c, r in self._state.items() if r == row and c != col]
                if others:
                    raise MatrixError(
                        f"短路风险：行 {row} 已连接列 {others}，"
                        f"禁止多列共享同一输出（如需开启设 allow_row_sharing=True）。"
                    )

            self._state[col] = row
            return self.apply()

    def disconnect(self, col: int) -> bytes:
        """断开指定输入列（置为 0），返回下发帧。"""
        with self._lock:
            col = _norm_col(col)
            self._state[col] = 0
            return self.apply()

    def open_all(self) -> bytes:
        """断开所有连接（发送全 0 帧）。"""
        with self._lock:
            self._state = {c: 0 for c in range(1, N_COLS + 1)}
            return self.apply()

    # ---- 批量操作 -------------------------------------------------------
    def set_mapping(self, mapping: Mapping[int, int], open_others: bool = True) -> bytes:
        """
        批量设置连接状态。

        Parameters
        ----------
        mapping : Mapping[int, int]
            列 -> 行 映射；列取 1..24，行取 1..6（0/None 表示断开）。
        open_others : bool
            True（默认）则未列出的列全部断开；False 则保留既有连接。
        """
        with self._lock:
            if open_others:
                self._state = {c: 0 for c in range(1, N_COLS + 1)}
            for col, row in mapping.items():
                col = _norm_col(col)
                if row is None or row == 0:
                    self._state[col] = 0
                else:
                    self._state[col] = _norm_row(row)
            return self.apply()

    # ---- 状态查询 -------------------------------------------------------
    def get_mapping(self) -> Dict[int, int]:
        """返回当前（列 -> 行）非空连接映射。"""
        with self._lock:
            return {c: r for c, r in self._state.items() if r != 0}

    def state_matrix(self) -> list:
        """
        返回 6 x 24 布尔矩阵 [row][col]，标记列 col 是否连到行 row。

        索引：row 0..5 对应行 1..6；col 0..23 对应列 1..24。
        """
        with self._lock:
            mat = [[False] * N_COLS for _ in range(N_ROWS)]
            for col, row in self._state.items():
                if row:
                    mat[row - 1][col - 1] = True
            return mat

    def as_frame_text(self) -> str:
        """返回当前状态对应的帧文本（不含终止符），便于打印核对。"""
        with self._lock:
            return build_frame(self._state, b"").decode("ascii")

    # ---- 输运实验辅助 ------------------------------------------------
    def route_4wire(self, sample_col: int, rows: Tuple = (1, 2, 3, 4),
                    open_others: bool = True) -> bytes:
        """
        四端法（范德堡/标准电阻测量）路由：将单个样品的 4 个输入引脚
        分别接到 4 个输出通道（默认输出 1..4 = 电流源+、电流源-、电压+、电压-）。

        Parameters
        ----------
        sample_col : int
            样品在矩阵上的起始输入列号（4 个引脚占用连续列 sample_col..sample_col+3）。
        rows : Tuple[int, ...]
            对应的输出行号序列，长度必须等于引脚数。
        """
        sample_col = _norm_col(sample_col)
        if sample_col + len(rows) - 1 > N_COLS:
            raise MatrixError(f"样品占用列越界：{sample_col}..{sample_col+len(rows)-1}")
        mapping = {sample_col + k: row for k, row in enumerate(rows)}
        return self.set_mapping(mapping, open_others=open_others)

    def route_4terminal(self, i_plus: int, i_minus: int, v_plus: int,
                        v_minus: int, rows: Tuple = (1, 2, 3, 4),
                        open_others: bool = True) -> bytes:
        """
        四端法路由（任意列组合）：将电流源 +/- 与电压表 +/- 接到样品
        任意指定的四组输入列（引脚，可每口多 pin）。区别于 route_4wire 的"连续 4 列"限制，
        每项可为单个整数或非空整数列表；同组 pin 电气并联。

        Parameters
        ----------
        i_plus, i_minus : int
            电流源 + / - 所接输入列（1..24）。
        v_plus, v_minus : int
            电压表 + / - 所接输入列（1..24）。
        rows : Tuple[int, ...]
            对应的输出行号序列，长度必须为 4。
            默认 (1, 2, 3, 4) = (电流源+, 电流源-, 电压+, 电压-)。
        open_others : bool
            True（默认）则未列出的列全部断开；False 则保留既有连接。

        Returns
        -------
        bytes
            本次下发的整帧。

        Raises
        ------
        MatrixError
            所有 pin 必须在 1..24 且不重复；rows 长度必须为 4 且两两不同。
        """
        cols = (i_plus, i_minus, v_plus, v_minus)
        groups = []
        for value in cols:
            group = value if isinstance(value, (list, tuple)) else [value]
            if not group or any(type(c) != int for c in group):
                raise MatrixError("每个口需要一个 pin 或非空整数 pin 列表")
            groups.append(tuple(_norm_col(c) for c in group))
        norm_cols = tuple(c for group in groups for c in group)
        if len(set(norm_cols)) != len(norm_cols):
            raise MatrixError("pin 不能重复；同一个 pin 只能连接一个输出口")
        if len(rows) != 4:
            raise MatrixError(f"rows 长度必须为 4，收到 {rows!r}")
        norm_rows = tuple(_norm_row(r) for r in rows)
        if len(set(norm_rows)) != 4:
            raise MatrixError(
                f"四个输出行必须两两不同（防止行共享短路），收到 {norm_rows}"
            )
        mapping = {col: row for group, row in zip(groups, norm_rows) for col in group}  # 列 -> 行
        return self.set_mapping(mapping, open_others=open_others)

    def route_samples(self, samples: Mapping[int, int], rows=(1, 2, 3, 4, 5, 6),
                      open_others: bool = True) -> bytes:
        """
        多样品切换测试：samples = {样品编号: 起始输入列}，
        将每一样品的 4 端依次接到输出行 1..4。

        注意：本调用在同一时刻只会接通最后写入的样品（矩阵为绝对状态），
        多样品轮询应在循环中逐个调用并配合仪表采集。
        """
        mapping = {}
        for _sid, base in samples.items():
            base = _norm_col(base)
            for k, row in enumerate(rows[:4]):
                mapping[base + k] = row
        return self.set_mapping(mapping, open_others=open_others)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def list_visa_resources() -> list:
    """
    列出系统当前可发现的 VISA 资源（GPIB/TCPIP/USB/ASRL 等）。
    需要 pyvisa + 对应后端（如 NI-VISA）。失败时返回空列表并附带原因。
    """
    try:
        import pyvisa
        rm = pyvisa.ResourceManager()
        return list(rm.list_resources())
    except Exception as e:  # pragma: no cover
        return [f"<VISA 不可用: {type(e).__name__} {e}>"]


def list_serial_ports() -> list:
    """列出当前系统的串口设备（仅 RS232 后端需要）。"""
    if list_ports is None:
        return []
    return [{"device": p.device, "description": p.description, "hwid": p.hwid}
            for p in list_ports.comports()]


def discover_resource(prefer: Optional[str] = None) -> Optional[str]:
    """
    简易 VISA 资源发现：优先返回 prefer；否则返回第一个 GPIB 资源，
    若没有则返回第一个任意资源。无可用资源时返回 None（可在模拟模式使用）。
    """
    res = [r for r in list_visa_resources() if isinstance(r, str)]
    if not res:
        return None
    if prefer:
        for r in res:
            if r.upper() == prefer.upper():
                return r
    gpib = [r for r in res if r.upper().startswith("GPIB")]
    return gpib[0] if gpib else res[0]


if __name__ == "__main__":
    # 命令行快速自测（无需硬件）
    import sys
    print("M6x24 矩阵开关驱动自检")
    print("VISA 资源：", list_visa_resources())
    print("串口：", list_serial_ports())

    sw = MatrixSwitch(simulate=True)
    print("\n[1] 示例帧校验（应全部匹配文档）：")
    cases = [
        ({}, b"[00000000,00000000,00000000]\r\n"),
        ({1: 1}, b"[10000000,00000000,00000000]\r\n"),
        ({4: 2, 5: 3}, b"[00023000,00000000,00000000]\r\n"),
    ]
    ok = True
    for st, expected in cases:
        fr = build_frame(st)
        flag = "OK" if fr == expected else "FAIL"
        if fr != expected:
            ok = False
        print(f"  {flag}  build_frame({st}) -> {fr!r}  (期望 {expected!r})")

    print("\n[2] 单通道连接/断开（模拟，GPIB 后端）：")
    sw.connect("A", 1)        # 行1 <- 列1
    sw.connect(3, 5)          # 行3 <- 列5
    print("  当前帧:", sw.last_frame)
    print("  状态映射:", sw.get_mapping())

    print("\n[3] 四端法路由到样品列 1：")
    sw.route_4wire(1)
    print("  帧:", sw.last_frame)

    print("\n[4] 冲突与短路保护：")
    try:
        sw2 = MatrixSwitch(simulate=True)
        sw2.connect(1, 1)
        sw2.connect(2, 1)     # 同一列连两个行 -> 应报错
    except MatrixError as e:
        print("  同列冲突被正确拦截:", e)
    try:
        sw3 = MatrixSwitch(simulate=True)
        sw3.connect(1, 1)
        sw3.connect(1, 9)     # 同一行连两列 -> 默认禁止
    except MatrixError as e:
        print("  行共享短路被拦截:", e)

    print("\n[5] 全部断开：")
    sw.open_all()
    print("  帧:", sw.last_frame)

    print("\n自检完成，结果:", "全部通过" if ok else "存在失败")
    sys.exit(0 if ok else 1)
