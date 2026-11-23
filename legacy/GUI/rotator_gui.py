r"""旋转杆独立控制 GUI（独立程序，不 import 其他测量程序）。

工作模型（2026-09-08 用户现场确认）：
- 样品杆单向旋转，角度范围 0-360°，到 360° 截止；任意输入按模 360
  自动换算（-90 → 270，450 → 90），换算结果回填输入框并记日志。
- 每次测量开始和结束都必须发送回零指令 [z,1,1501]；到达零点后控制器
  标记当前点为零点并发出蜂鸣（蜂鸣无法查询，以现场听觉为准）。
- 回零后坐标为 0，目标角度有效范围 [0, 360]。
- 回读为控制器步数寄存器（无绝对编码器），符号已按物理标定
  （寄存器 D=1 = 物理正角度），运动中实时更新，速度单位 °/s。
- ⚠ 协议无停止/急停命令：超时、关窗、异常都不会停止电机。

GUI 流程（按上述模型设计）：
    连接 → 回零（等蜂鸣，确认后解锁旋转）→ 测量旋转（0-360）
    → 测量结束回零 → 断开/关窗
关窗或断开时若未回零，GUI 会主动提醒。

启动（必须用 Miniconda Python 前台启动；托管 3.13.12 缺 tkinter）：
    C:\ProgramData\Miniconda3\python.exe GUI\rotator_gui.py
自检（无硬件，内置模拟串口走真实 worker/队列/驱动路径）：
    C:\ProgramData\Miniconda3\python.exe GUI\rotator_gui.py --selftest

约定：全部串口操作由单一 worker 线程串行执行；worker 不碰 Tk，
结果经队列回 UI（与 measureGUI 系列 put((kind, data)) 约定一致）。
"""

import math
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

import serial
import serial.tools.list_ports

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rotater.rotator_interface import Rotator  # noqa: E402

UI_POLL_MS = 100
DEFAULT_SPEED = 0.5
DEFAULT_TOLERANCE = 0.05
DEFAULT_TIMEOUT_S = 120.0
ANGLE_MIN = 0.0
ANGLE_MAX = 360.0
HOME_TOLERANCE = 0.2
HOME_SPEED_DPS = 1501 / 150.13878  # [z,1,1501] 的速度字段换算 ≈ 10 °/s
MOVE_MARGIN_S = 1.5     # 计时等待的额外裕量
HOME_MARGIN_S = 2.0
RECHECK_INTERVAL_S = 1.0  # 到点后静止校验的重查间隔（查询仅在静止时进行）

# ⚠ 查询策略（2026-09-08 用户实测）：本控制器运动中/周期性查询 [?] 会干扰
# 其步数计数（1 s 自动刷新曾把计数器搞串）。因此：
#   - 不做周期性自动刷新；
#   - 运动到位判断 = 运动前静止读一次 → 按距离/速度计时等待 → 到点后
#     静止状态每 RECHECK_INTERVAL_S 校验一次，绝不查询运动中的电机；
#   - 其余查询（读一次/连接握手）都发生在静止时，由用户主动触发。


class _SimTransport:
    """模拟旋转杆：实现驱动所需的最小 transport 接口（仅 --selftest 使用）。

    按 2026-09-08 物理标定与用户确认的工作模型建模：
    发令 D=0 → 正转、D=1 → 反转；回包 D=1 对应正角度；
    收到运动指令后寄存器立即更新；[z] 回零后角度=0。
    """

    SCALE_SEND = 150.13878
    SCALE_READ = 54050 / 360.0

    def __init__(self):
        self._angle = 0.0
        self._buf = bytearray()

    @property
    def in_waiting(self):
        return len(self._buf)

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._buf)
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def write(self, data):
        if data.startswith(b'[?]'):
            counts = int(round(abs(self._angle) * self.SCALE_READ))
            direction = 1 if self._angle >= 0 else 0
            self._buf += b'[w,%d,%06d]\r\n' % (direction, counts)
        elif data.startswith(b'[r,'):
            body = data.strip(b'\r\n[]').decode('ascii')
            _, direction, _speed, position = body.split(',')
            angle = int(position) / self.SCALE_SEND
            self._angle = -angle if direction == '1' else angle
        elif data.startswith(b'[z,'):
            self._angle = 0.0
        return len(data)

    def close(self):
        pass


class _Worker(threading.Thread):
    """串口设备工作线程：串行执行全部 Rotator 操作，只通过队列与 UI 通信。"""

    def __init__(self, ui_queue):
        super().__init__(daemon=True)
        self.ui_queue = ui_queue
        self.cmd_queue = queue.Queue()
        self.rot = None

    def submit(self, kind, payload=None):
        self.cmd_queue.put((kind, payload))

    def stop(self):
        self.cmd_queue.put(None)

    def _close_quietly(self):
        if self.rot is not None:
            try:
                self.rot.close()
            except Exception:
                pass
            self.rot = None

    def run(self):
        try:
            while True:
                item = self.cmd_queue.get()
                if item is None:
                    break
                kind, payload = item
                try:
                    getattr(self, '_do_' + kind)(payload)
                except Exception as exc:
                    # 单命令失败只上报，不允许打死 worker 线程：
                    # 真机瞬态超时/回包异常是常态，线程一死全部操作无声卡死
                    self._post('error', (kind, f'{type(exc).__name__}: {exc}'))
        finally:
            self._close_quietly()

    def _post(self, kind, data=None):
        self.ui_queue.put((kind, data))

    def _do_connect(self, payload):
        port, sim = payload
        if sim:
            self.rot = Rotator(port, angle_limits=(ANGLE_MIN, ANGLE_MAX),
                               transport=_SimTransport())
        else:
            self.rot = Rotator(port, angle_limits=(ANGLE_MIN, ANGLE_MAX))
        angle = self.rot.read_deg()
        self._post('connected', (port, angle))

    def _do_disconnect(self, _payload):
        self._close_quietly()
        self._post('disconnected')

    def _do_read(self, _payload):
        self._post('angle', self.rot.read_deg())

    def _do_move(self, payload):
        target, speed, wait, tolerance, timeout = payload
        cur = self.rot.read_deg()  # 运动前静止读一次，用于计时
        est_s = abs(target - cur) / speed
        self.rot.rotate_to_degree(target, speed)
        self._post('move_sent', (target, cur, est_s))
        if not wait:
            self._post('move_sent_only', target)
            return
        # 计时等待：运动期间不发任何查询，避免干扰控制器计数
        time.sleep(est_s + MOVE_MARGIN_S)
        deadline = time.monotonic() + timeout
        arrived = None
        while time.monotonic() < deadline:
            arrived = self.rot.read_deg()  # 到点后静止校验
            self._post('angle', arrived)
            if abs(arrived - target) <= tolerance:
                self._post('move_done', (target, arrived))
                return
            time.sleep(RECHECK_INTERVAL_S)
        raise TimeoutError(f'未确认到位：target={target}, last={arrived}')

    def _do_home_wait(self, timeout):
        cur = self.rot.read_deg()  # 回零前静止读一次，用于计时
        sleep_s = abs(cur) / HOME_SPEED_DPS + HOME_MARGIN_S
        self._post('home_estimate', (cur, sleep_s))
        self.rot.return_to_zero_clockwise()
        self._post('home_sent')
        time.sleep(sleep_s)  # 回零转动期间不发任何查询
        deadline = time.monotonic() + timeout
        arrived = None
        while time.monotonic() < deadline:
            arrived = self.rot.read_deg()  # 到点后静止校验
            self._post('angle', arrived)
            if abs(arrived) <= HOME_TOLERANCE:
                self._post('home_done', arrived)
                return
            time.sleep(RECHECK_INTERVAL_S)
        raise TimeoutError(f'回零未确认：last={arrived}')


class RotatorGUI:
    def __init__(self, root, sim=False):
        self.root = root
        self.sim = sim
        self.ui_queue = queue.Queue()
        self.worker = _Worker(self.ui_queue)
        self.worker.start()
        self.connected = False
        self.homed = False
        self.current_angle = None
        self.last_arrived = None
        self._pending_read = False
        self._build()
        self.root.title('旋转杆控制 Sample Rotator' + ('  [SIM]' if sim else ''))
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        self.root.after(UI_POLL_MS, self._poll_ui)
        self._refresh_ports()
        self._update_states()

    # ---------- UI 构建 ----------

    def _build(self):
        self.root.minsize(640, 460)
        top = ttk.LabelFrame(self.root, text='1. 连接')
        top.pack(fill='x', padx=8, pady=(8, 4))
        ttk.Label(top, text='串口:').grid(row=0, column=0, padx=4, pady=6)
        self.port_var = tk.StringVar()
        self.port_cb = ttk.Combobox(top, textvariable=self.port_var,
                                    width=12, state='readonly')
        self.port_cb.grid(row=0, column=1, padx=2)
        ttk.Button(top, text='刷新', command=self._refresh_ports,
                   width=6).grid(row=0, column=2, padx=2)
        self.connect_btn = ttk.Button(top, text='连接', command=self._on_connect,
                                      width=8)
        self.connect_btn.grid(row=0, column=3, padx=6)
        self.disconnect_btn = ttk.Button(top, text='断开',
                                         command=self._on_disconnect, width=8,
                                         state='disabled')
        self.disconnect_btn.grid(row=0, column=4)
        self.status_var = tk.StringVar(value='未连接')
        ttk.Label(top, textvariable=self.status_var,
                  foreground='#888').grid(row=0, column=5, padx=12)
        top.columnconfigure(6, weight=1)

        home = ttk.LabelFrame(
            self.root,
            text='2. 回零（每次测量开始和结束必须执行；完成后控制器标记零点并蜂鸣）')
        home.pack(fill='x', padx=8, pady=4)
        self.home_btn = ttk.Button(home, text='回零', command=self._on_home,
                                   width=10, state='disabled')
        self.home_btn.grid(row=0, column=0, padx=6, pady=6)
        self.home_hint_var = tk.StringVar(value='——')
        ttk.Label(home, textvariable=self.home_hint_var,
                  foreground='#555').grid(row=0, column=1, padx=4)
        self.homed_var = tk.BooleanVar(value=False)
        self.homed_cb = ttk.Checkbutton(
            home, text='已听到蜂鸣（零点已标记，解锁旋转）',
            variable=self.homed_var, command=self._on_homed_confirm,
            state='disabled')
        self.homed_cb.grid(row=0, column=2, padx=12)
        home.columnconfigure(3, weight=1)

        angle = ttk.LabelFrame(self.root,
                               text='回读角度（控制器步数寄存器，符号已物理标定）')
        angle.pack(fill='x', padx=8, pady=4)
        self.angle_var = tk.StringVar(value='---')
        ttk.Label(angle, textvariable=self.angle_var,
                  font=('Consolas', 22, 'bold')).pack(side='left', padx=12,
                                                      pady=6)
        self.read_btn = ttk.Button(angle, text='读一次（仅静止时查询）',
                                   command=self._on_read, state='disabled')
        self.read_btn.pack(side='left', padx=4)
        ttk.Label(angle, text='不做周期轮询：运动中查询会干扰控制器计数',
                  foreground='#555').pack(side='left', padx=8)

        move = ttk.LabelFrame(self.root,
                              text='3. 测量旋转（0-360°，到 360° 截止；任意输入自动换算）')
        move.pack(fill='x', padx=8, pady=4)
        ttk.Label(move, text='目标角度 (°):').grid(row=0, column=0, padx=4,
                                                  pady=6)
        self.target_var = tk.StringVar(value='0.0')
        ttk.Entry(move, textvariable=self.target_var, width=9).grid(
            row=0, column=1, padx=2)
        ttk.Label(move, text='速度 (°/s):').grid(row=0, column=2, padx=(10, 2))
        self.speed_var = tk.StringVar(value=str(DEFAULT_SPEED))
        ttk.Entry(move, textvariable=self.speed_var, width=7).grid(
            row=0, column=3, padx=2)
        self.wait_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(move, text='等待回读到位（步数寄存器）',
                        variable=self.wait_var).grid(row=0, column=4, padx=10)
        self.rotate_btn = ttk.Button(move, text='旋转', command=self._on_rotate,
                                     width=8, state='disabled')
        self.rotate_btn.grid(row=0, column=5, padx=6)
        move.columnconfigure(6, weight=1)

        log = ttk.LabelFrame(self.root, text='日志')
        log.pack(fill='both', expand=True, padx=8, pady=4)
        self.log_text = scrolledtext.ScrolledText(log, height=10, width=80,
                                                  state='disabled',
                                                  font=('Consolas', 9))
        self.log_text.pack(fill='both', expand=True, padx=2, pady=2)
        self.log_text.tag_config('error', foreground='#c62828')
        self.log_text.tag_config('ok', foreground='#1b5e20')

        warn = ('⚠ 单向旋转 0-360°；无停止/急停命令，超时/关窗都不会停止电机；'
                '运动中查询会干扰控制器计数（GUI 已改为计时等待）；'
                '回零完成以蜂鸣为准')
        ttk.Label(self.root, text=warn, foreground='#b26a00').pack(
            anchor='w', padx=10, pady=(0, 6))

    # ---------- 状态与日志 ----------

    def _log(self, msg, tag=None):
        stamp = time.strftime('%H:%M:%S')
        self.log_text.config(state='normal')
        self.log_text.insert('end', f'[{stamp}] {msg}\n', tag)
        self.log_text.see('end')
        self.log_text.config(state='disabled')

    def _update_states(self):
        if self.connected:
            self.status_var.set(
                f'已连接 {self._connected_port}'
                + ('（已回零，可测量）' if self.homed else '（须先回零）'))
        else:
            self.status_var.set('未连接')
        state = 'normal' if self.connected else 'disabled'
        for btn in (self.disconnect_btn, self.home_btn, self.homed_cb,
                    self.read_btn):
            btn.config(state=state)
        rotate_state = 'normal' if (self.connected and self.homed) else 'disabled'
        self.rotate_btn.config(state=rotate_state)
        self.connect_btn.config(state='disabled' if self.connected else 'normal')
        self.port_cb.config(state='disabled' if self.connected else 'readonly')

    def _show_angle(self, angle):
        self.current_angle = 0.0 if abs(angle) < 5e-5 else angle
        self.angle_var.set(f'{self.current_angle:+.4f}°')
        self._update_home_hint()

    def _update_home_hint(self):
        if not self.connected:
            self.home_hint_var.set('——')
        elif self.current_angle is None:
            self.home_hint_var.set('回零预计用时未知（先读一次角度）')
        else:
            est = abs(self.current_angle) / HOME_SPEED_DPS
            self.home_hint_var.set(
                f'按当前角度 {self.current_angle:+.2f}° 估算回零 ~{est:.0f} s'
                '（完成以蜂鸣为准）')

    # ---------- 动作（UI 线程） ----------

    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if self.sim:
            ports = ['SIM'] + ports
        self.port_cb['values'] = ports
        if ports and not self.connected:
            self.port_var.set(ports[0])

    def _on_connect(self):
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning('旋转杆', '请选择串口', parent=self.root)
            return
        self._connected_port = port
        self.connect_btn.config(state='disabled')
        self._log(f'连接 {port}（软件限位固定 [0, 360]°）...')
        self.worker.submit('connect', (port, self.sim))

    def _on_disconnect(self):
        if not self._confirm_end_homing('断开前'):
            return
        self.worker.submit('disconnect')
        self.connected = False
        self.homed = False
        self.homed_var.set(False)
        self.current_angle = None
        self.angle_var.set('---')
        self._update_states()
        self._update_home_hint()

    def _on_home(self):
        if not self.connected:
            return
        est_move = 0.0
        if self.current_angle is not None:
            est_move = abs(self.current_angle) / HOME_SPEED_DPS
        timeout = max(60.0, est_move + 60.0)
        self._log(f'发送回零指令（预计 ~{est_move:.0f} s 转动 + 蜂鸣），'
                  '转动期间不查询，结束后静止校验...')
        self.home_btn.config(state='disabled')
        self.worker.submit('home_wait', timeout)

    def _on_homed_confirm(self):
        self.homed = self.homed_var.get()
        if self.homed:
            self._log('零点已标记（手动确认），旋转已解锁', 'ok')
        self._update_states()

    def _on_read(self):
        if not self.connected or self._pending_read:
            return
        self._pending_read = True
        self.worker.submit('read')

    def _on_rotate(self):
        if not (self.connected and self.homed):
            messagebox.showwarning('旋转杆', '请先回零并确认蜂鸣后再旋转',
                                   parent=self.root)
            return
        try:
            target = float(self.target_var.get())
            speed = float(self.speed_var.get())
        except ValueError:
            messagebox.showerror('旋转杆', '目标角度与速度必须为数字',
                                 parent=self.root)
            return
        if not math.isfinite(target):
            messagebox.showerror('旋转杆', '目标角度无效', parent=self.root)
            return
        if not 0 < speed <= 60:
            messagebox.showerror('旋转杆', '速度必须在 0-60 °/s 之间',
                                 parent=self.root)
            return
        raw_target = target
        target = target % ANGLE_MAX  # 任意输入归一化到 [0, 360)：-90 -> 270
        if abs(target - raw_target) > 1e-9:
            self._log(f'输入 {raw_target:g}° 超出 0-360，已自动换算为 '
                      f'{target:g}°')
            self.target_var.set(f'{target:g}')
        wait = self.wait_var.get()
        est = ''
        if self.current_angle is not None:
            est = '，预计 ~%.0f s' % (abs(target - self.current_angle) / speed)
        self._log(f'旋转 -> {target:g}° @ {speed:g} °/s{est}'
                  + ('；等待回读到位（步数寄存器）' if wait
                     else '；不等待，请自行目视确认'))
        self.worker.submit('move', (target, speed, wait,
                                    DEFAULT_TOLERANCE, DEFAULT_TIMEOUT_S))

    def _confirm_end_homing(self, action):
        """测量结束前的回零提醒。返回 False 表示用户选择先回零（中止本次动作）。"""
        if not (self.connected and self.homed):
            return True
        if self.current_angle is not None and abs(self.current_angle) < 0.5:
            return True
        ans = messagebox.askyesnocancel(
            '回零提醒',
            f'{action}建议先回零（每次测量结束必须回零，防止线缆缠绕）。\n\n'
            '是 = 现在回零并等待到位（完成后可再次关闭）\n'
            '否 = 不回零直接继续\n取消 = 返回',
            parent=self.root)
        if ans is None:
            return False
        if ans:
            self._on_home()
            return False
        self._log('⚠ 跳过结束回零（用户确认）', 'error')
        return True

    def _on_close(self):
        if not self._confirm_end_homing('关闭窗口前'):
            return
        self.worker.stop()
        self.root.destroy()

    # ---------- 队列轮询 ----------

    def _poll_ui(self):
        try:
            while True:
                kind, data = self.ui_queue.get_nowait()
                if kind == 'connected':
                    port, angle = data
                    self._log(f'已连接 {port}；寄存器 {angle:+.4f}°'
                              '（每次测量开始前请先回零）')
                    self.connected = True
                    self.homed = False
                    self.homed_var.set(False)
                    self._update_states()
                    self._show_angle(angle)
                elif kind == 'disconnected':
                    self._log('已断开')
                elif kind == 'angle':
                    self._pending_read = False
                    self._show_angle(data)
                elif kind == 'move_sent':
                    target, cur, est_s = data
                    self._log(f'指令已发送（目标 {target:g}°，运动前寄存器 '
                              f'{cur:+.2f}°，计时 ~{est_s:.0f} s，'
                              '运动中不查询）')
                elif kind == 'home_estimate':
                    cur, sleep_s = data
                    self._log(f'回零前寄存器 {cur:+.2f}°，计时 ~{sleep_s:.0f} s'
                              '（回零转动期间不查询）')
                elif kind == 'move_done':
                    target, arrived = data
                    self.last_arrived = arrived
                    self._show_angle(arrived)
                    self._log(f'回读到位：目标 {target:g}°，'
                              f'寄存器 {arrived:+.4f}°（步数寄存器，'
                              '重要实验请目视复核）', 'ok')
                elif kind == 'move_sent_only':
                    self._log('指令已发送；请目视确认运动与到位', 'ok')
                elif kind == 'home_sent':
                    self._log('回零指令已发送，转动中（完成时蜂鸣）...')
                elif kind == 'home_done':
                    arrived = data
                    self._show_angle(arrived)
                    self._log(f'寄存器已归零（{arrived:+.4f}°）。'
                              '听到蜂鸣后请手动勾选"已听到蜂鸣"解锁旋转'
                              '（寄存器归零不代表零点已标记）', 'ok')
                    self._update_states()
                elif kind == 'error':
                    src, msg = data
                    self._log(f'[{src}] {msg}', 'error')
                    if src == 'read':
                        # 释放读数占位，后续读取可重试
                        self._pending_read = False
                        self._log('瞬态通信失败，已继续轮询；若连续失败请'
                                  '检查串口占用/接线', 'error')
                    if src == 'home_wait':
                        self._log('回零等待失败：若杆已实际回零并听到蜂鸣，'
                                  '直接手动勾选"已听到蜂鸣"即可；若寄存器'
                                  '不动，请确认电机上电/使能与联轴器', 'error')
                    if self.connected:
                        self.home_btn.config(state='normal')
        except queue.Empty:
            pass
        self.root.after(UI_POLL_MS, self._poll_ui)


def _selftest():
    """无硬件自检：模拟串口走真实 worker/驱动/队列路径，覆盖完整测量流程。"""
    messagebox.showerror = lambda *a, **k: None
    messagebox.showwarning = lambda *a, **k: None
    root = tk.Tk()
    app = RotatorGUI(root, sim=True)

    def pump(seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            root.update()
            time.sleep(0.01)

    def wait_until(cond, seconds=15.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            root.update()
            if cond():
                return True
            time.sleep(0.01)
        return cond()

    failures = []
    total_checks = 0

    def check(name, cond):
        nonlocal total_checks
        total_checks += 1
        print(('PASS ' if cond else 'FAIL ') + name)
        if not cond:
            failures.append(name)

    pump(0.3)
    check('端口列表含 SIM', 'SIM' in app.port_cb['values'])
    app.port_var.set('SIM')
    app._on_connect()
    check('连接后未回零，旋转锁定',
          wait_until(lambda: app.connected)
          and not app.homed
          and str(app.rotate_btn['state']) == 'disabled')
    check('初始寄存器 0', app.current_angle is not None
          and abs(app.current_angle) < 0.01)

    app._on_home()
    check('回零后寄存器归 0 但旋转仍锁定（等人工确认）',
          wait_until(lambda: app.current_angle is not None
                     and abs(app.current_angle) < 0.2)
          and not app.homed
          and str(app.rotate_btn['state']) == 'disabled')

    app.homed_var.set(True)
    app._on_homed_confirm()
    check('人工勾选蜂鸣确认后解锁旋转',
          app.homed and str(app.rotate_btn['state']) == 'normal')

    app.target_var.set('30.0')
    app.speed_var.set('60')
    app.wait_var.set(True)
    app._on_rotate()
    check('正向旋转到位 ~30°',
          wait_until(lambda: app.last_arrived is not None
                     and abs(app.last_arrived - 30.0) <= DEFAULT_TOLERANCE))

    app.target_var.set('-90.0')
    app._on_rotate()
    check('输入 -90 自动换算为 270 并到位',
          wait_until(lambda: app.current_angle is not None
                     and abs(app.current_angle - 270.0) <= DEFAULT_TOLERANCE)
          and app.target_var.get() == '270')

    app.target_var.set('450.0')
    app._on_rotate()
    check('输入 450 换算为 90 并到位',
          wait_until(lambda: abs((app.last_arrived or 0) - 90.0)
                     <= DEFAULT_TOLERANCE)
          and app.target_var.get() == '90')

    app.target_var.set('350.0')
    app._on_rotate()
    check('350° 可达（范围内）',
          wait_until(lambda: abs((app.last_arrived or 0) - 350.0)
                     <= DEFAULT_TOLERANCE))

    # worker 韧性：单命令异常必须被捕获上报，不得打死线程
    app.worker.submit('nonexistent_cmd', None)
    app.target_var.set('10.0')
    app._on_rotate()
    check('worker 异常后仍能执行后续命令（10° 到位）',
          wait_until(lambda: abs((app.last_arrived or 0) - 10.0)
                     <= DEFAULT_TOLERANCE))
    log_content = app.log_text.get('1.0', 'end')
    check('worker 异常被捕获并记入日志',
          'nonexistent_cmd' in log_content and 'AttributeError' in log_content)

    app._on_home()
    check('结束回零归 0', wait_until(lambda: app.current_angle is not None
                                     and abs(app.current_angle) < 0.2))

    app.worker.stop()
    pump(0.5)
    root.destroy()
    if failures:
        raise SystemExit('SELFTEST FAILED: ' + ', '.join(failures))
    print(f'SELFTEST OK ({total_checks} checks)')


def main():
    if '--selftest' in sys.argv:
        _selftest()
        return
    root = tk.Tk()
    RotatorGUI(root)
    root.mainloop()


if __name__ == '__main__':
    main()
