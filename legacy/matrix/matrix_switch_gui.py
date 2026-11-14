# -*- coding: utf-8 -*-
"""
matrix_switch_gui.py
=====================
M6x24 矩阵开关（Multi-field 6×24 同轴矩阵）独立图形界面。

放置位置：与驱动 ``matrix_switch.py`` 同目录（``matrix/``）。

特点
----
* 6×24 状态点阵（仿前面板继电器指示器）：绿色 = 已闭合，灰色 = 断开。
  左键点击任一单元格即可切换该 (行,列) 连接；同列只能连一条行（绝对状态帧）。
* 连接面板：GPIB/串口/模拟三种后端，资源下拉可一键刷新。
* 手动精确控制：行/列微调框 + 连接/断开/全断开/重发当前帧。
* 四端法路由：直接指定 I+/I−/V+/V− 四引脚列与对应输出行。
* 帧文本实时显示（设备无回读，以本地缓存为准）。
* 测量类操作在后台线程执行，避免阻塞 Tk；日志经队列回传主线程。

运行：在本目录前台启动 ``python matrix_switch_gui.py``（Tk 界面必须前台）。
"""

import os
import queue
import sys
import threading

# 保证无论从哪个工作目录启动都能 import 到同目录驱动
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tkinter as tk
from tkinter import ttk, messagebox

from matrix_switch import (
    MatrixError,
    MatrixSwitch,
    ROW_LABELS,
    N_ROWS,
    N_COLS,
    list_serial_ports,
    list_visa_resources,
)

# 颜色常量
COLOR_ON = "#3a7d44"      # 已闭合（绿）
COLOR_OFF = "#dcdcdc"     # 断开（灰）
COLOR_HEAD = "#eef2f7"


class MatrixSwitchGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("M6x24 矩阵开关控制")

        self.sw: "MatrixSwitch | None" = None
        self.connected = False

        # 后台线程 -> 主线程 的消息队列
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.cells = []  # cells[row][col] -> tk.Label

        self._build_ui()
        self._queue_after = self.root.after(100, self._process_queue)
        # 关闭窗口时断开硬件
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)

        self._build_conn_panel(outer)
        self._build_grid(outer)
        self._build_manual_panel(outer)
        self._build_routing_panel(outer)
        self._build_log(outer)

    def _build_conn_panel(self, parent: ttk.Frame) -> None:
        f = ttk.LabelFrame(parent, text="连接", padding=6)
        f.pack(fill="x", pady=4)

        ttk.Label(f, text="后端:").grid(row=0, column=0, sticky="e")
        self.backend_var = tk.StringVar(value="visa")
        self.backend_cb = ttk.Combobox(
            f, textvariable=self.backend_var, values=["visa", "serial"],
            width=8, state="readonly",
        )
        self.backend_cb.grid(row=0, column=1, padx=3, sticky="w")
        self.backend_cb.bind("<<ComboboxSelected>>", self._on_backend_change)

        ttk.Label(f, text="资源:").grid(row=0, column=2, sticky="e")
        self.resource_var = tk.StringVar(value="")
        self.resource_cb = ttk.Combobox(f, textvariable=self.resource_var, width=22)
        self.resource_cb.grid(row=0, column=3, padx=3, sticky="w")

        self.refresh_btn = ttk.Button(
            f, text="刷新资源", command=self._refresh_resources
        )
        self.refresh_btn.grid(row=0, column=4, padx=3)

        self.sim_var = tk.BooleanVar(value=False)
        self.sim_chk = ttk.Checkbutton(f, text="模拟模式", variable=self.sim_var)
        self.sim_chk.grid(row=0, column=5, padx=6)

        self.connect_btn = ttk.Button(f, text="连接", command=self._connect)
        self.connect_btn.grid(row=0, column=6, padx=3)
        self.disconnect_btn = ttk.Button(
            f, text="断开", command=self._disconnect
        )
        self.disconnect_btn.grid(row=0, column=7, padx=3)

        self.status_var = tk.StringVar(value="○ 未连接")
        ttk.Label(f, textvariable=self.status_var, foreground="#444").grid(
            row=1, column=0, columnspan=8, sticky="w", pady=2
        )
        self._refresh_resources()

    def _build_grid(self, parent: ttk.Frame) -> None:
        f = ttk.LabelFrame(parent, text="状态点阵（行 A–F × 列 1–24，左键切换）", padding=6)
        f.pack(fill="x", pady=4)

        # 列号表头（每 2 列标注一次，避免拥挤）
        head = ttk.Frame(f)
        head.grid(row=0, column=1, sticky="w")
        ttk.Label(head, text="", width=3).pack(side="left")
        for c in range(N_COLS):
            lbl = tk.Label(
                head, text=str(c + 1), width=2,
                font=("Consolas", 7), bg=COLOR_HEAD,
            )
            lbl.pack(side="left")

        self.cells = [[None] * N_COLS for _ in range(N_ROWS)]
        for r in range(N_ROWS):
            rowf = ttk.Frame(f)
            rowf.grid(row=r + 1, column=1, sticky="w", pady=1)
            ttk.Label(rowf, text=ROW_LABELS[r], width=3, anchor="e").pack(side="left")
            for c in range(N_COLS):
                cell = tk.Label(
                    rowf, text="", width=2, height=1,
                    bg=COLOR_OFF, relief="raised", borderwidth=1,
                    font=("Consolas", 7),
                )
                cell.pack(side="left", padx=0, pady=0)
                cell.bind("<Button-1>", self._make_cell_callback(r, c))
                self.cells[r][c] = cell

    def _build_manual_panel(self, parent: ttk.Frame) -> None:
        f = ttk.LabelFrame(parent, text="手动精确控制", padding=6)
        f.pack(fill="x", pady=4)

        ttk.Label(f, text="行:").grid(row=0, column=0, sticky="e")
        self.mrow_var = tk.StringVar(value="A")
        ttk.Combobox(
            f, textvariable=self.mrow_var, values=list(ROW_LABELS),
            width=4, state="readonly",
        ).grid(row=0, column=1, padx=2)

        ttk.Label(f, text="列:").grid(row=0, column=2, sticky="e")
        self.mcol_var = tk.IntVar(value=1)
        ttk.Spinbox(
            f, from_=1, to=N_COLS, textvariable=self.mcol_var, width=5
        ).grid(row=0, column=3, padx=2)

        ttk.Button(f, text="连接", command=self._manual_connect).grid(
            row=0, column=4, padx=3)
        ttk.Button(f, text="断开", command=self._manual_disconnect).grid(
            row=0, column=5, padx=3)

        ttk.Button(f, text="全断开", command=self._open_all).grid(
            row=0, column=6, padx=8)
        ttk.Button(f, text="重发当前帧", command=self._apply).grid(
            row=0, column=7, padx=3)

        self.frame_var = tk.StringVar(value="[00000000,00000000,00000000]")
        ttk.Label(f, text="最近帧:").grid(row=1, column=0, sticky="e")
        ttk.Label(
            f, textvariable=self.frame_var, font=("Consolas", 10)
        ).grid(row=1, column=1, columnspan=7, sticky="w")

    def _build_routing_panel(self, parent: ttk.Frame) -> None:
        f = ttk.LabelFrame(parent, text="四端法路由（I+/I−/V+/V− ↔ 输出行）", padding=6)
        f.pack(fill="x", pady=4)

        pin_names = ["I+列", "I−列", "V+列", "V−列"]
        row_names = ["I+行", "I−行", "V+行", "V−行"]
        self.route_cols = []
        self.route_rows = []
        for k in range(4):
            ttk.Label(f, text=pin_names[k] + ":").grid(row=0, column=k * 2, sticky="e")
            cv = tk.IntVar(value=k + 1)
            ttk.Spinbox(f, from_=1, to=N_COLS, textvariable=cv, width=5).grid(
                row=0, column=k * 2 + 1, padx=2)
            self.route_cols.append(cv)

            ttk.Label(f, text=row_names[k] + ":").grid(row=1, column=k * 2, sticky="e")
            rv = tk.StringVar(value=ROW_LABELS[k])
            ttk.Combobox(f, textvariable=rv, values=list(ROW_LABELS),
                         width=4, state="readonly").grid(
                row=1, column=k * 2 + 1, padx=2)
            self.route_rows.append(rv)

        ttk.Button(f, text="执行四端法路由", command=self._route_4terminal).grid(
            row=0, column=8, rowspan=2, padx=8)

    def _build_log(self, parent: ttk.Frame) -> None:
        f = ttk.LabelFrame(parent, text="日志", padding=4)
        f.pack(fill="both", expand=True, pady=4)
        self.log_widget = tk.Text(f, height=8, wrap="word")
        self.log_widget.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(f, command=self.log_widget.yview)
        sb.pack(side="right", fill="y")
        self.log_widget.configure(yscrollcommand=sb.set)

    # -------------------------------------------------------------- 回调
    def _make_cell_callback(self, r: int, c: int):
        def cb(_evt):
            self._toggle(r, c)
        return cb

    def _on_backend_change(self, _evt=None) -> None:
        self._refresh_resources()

    def _refresh_resources(self) -> None:
        backend = self.backend_var.get()
        if backend == "visa":
            items = [r for r in list_visa_resources()
                     if isinstance(r, str) and "不可用" not in r]
        else:
            items = [p["device"] for p in list_serial_ports()]
        if not items:
            items = [""]
        self.resource_cb["values"] = items
        if self.resource_var.get() not in items and items[0]:
            self.resource_var.set(items[0])

    # ------------------------------------------------------- 硬件操作（线程）
    def _worker(self, fn, *args) -> None:
        def run():
            try:
                fn(*args)
            except MatrixError as e:
                self.q.put(("log", f"命令错误: {e}"))
            except Exception as e:  # noqa: BLE001
                self.q.put(("log", f"ERROR: {type(e).__name__}: {e}"))
            finally:
                self.q.put(("refresh", None))
        threading.Thread(target=run, daemon=True).start()

    def _connect(self) -> None:
        self._worker(self._do_connect)

    def _do_connect(self) -> None:
        backend = self.backend_var.get()
        res = self.resource_var.get().strip()
        sim = self.sim_var.get()
        self.sw = MatrixSwitch(
            resource=res or None, backend=backend, simulate=sim
        )
        self.sw.open()  # sync=True：连接即把缓存帧下发一次，对齐硬件
        self.connected = True
        label = self.sw.resource or ("SIMULATE" if sim else "未指定资源")
        self.q.put(("log", f"已连接: {label} ({backend})"))
        self.q.put(("status", f"● {label} ({backend})"))

    def _disconnect(self) -> None:
        self._worker(self._do_disconnect)

    def _do_disconnect(self) -> None:
        if self.sw is not None:
            self.sw.close()
        self.sw = None
        self.connected = False
        self.q.put(("log", "已断开连接。"))
        self.q.put(("status", "○ 未连接"))

    def _toggle(self, r: int, c: int) -> None:
        if self.sw is None:
            self.q.put(("log", "请先连接矩阵开关。"))
            return
        self._worker(self._do_toggle, r, c)

    def _do_toggle(self, r: int, c: int) -> None:
        row = r + 1
        col = c + 1
        mat = self.sw.state_matrix()
        if mat[r][c]:
            self.sw.disconnect(col)
            self.q.put(("log", f"断开 列{col}"))
        else:
            if self.sw.get_mapping().get(col):
                self.sw.disconnect(col)
            self.sw.connect(row, col)
            self.q.put(("log", f"连接 行{ROW_LABELS[r]}({row}) ↔ 列{col}"))

    def _manual_connect(self) -> None:
        if self.sw is None:
            self.q.put(("log", "请先连接矩阵开关。"))
            return
        self._worker(self._do_manual_connect)

    def _do_manual_connect(self) -> None:
        row = self.mrow_var.get()
        col = int(self.mcol_var.get())
        if self.sw.get_mapping().get(col):
            self.sw.disconnect(col)
        self.sw.connect(row, col)
        self.q.put(("log", f"连接 行{row} ↔ 列{col}"))

    def _manual_disconnect(self) -> None:
        if self.sw is None:
            self.q.put(("log", "请先连接矩阵开关。"))
            return
        self._worker(self._do_manual_disconnect)

    def _do_manual_disconnect(self) -> None:
        col = int(self.mcol_var.get())
        self.sw.disconnect(col)
        self.q.put(("log", f"断开 列{col}"))

    def _open_all(self) -> None:
        if self.sw is None:
            self.q.put(("log", "请先连接矩阵开关。"))
            return
        self._worker(self._do_open_all)

    def _do_open_all(self) -> None:
        self.sw.open_all()
        self.q.put(("log", "全部断开。"))

    def _apply(self) -> None:
        if self.sw is None:
            self.q.put(("log", "请先连接矩阵开关。"))
            return
        self._worker(self._do_apply)

    def _do_apply(self) -> None:
        self.sw.apply()
        self.q.put(("log", "已重发当前状态帧。"))

    def _route_4terminal(self) -> None:
        if self.sw is None:
            self.q.put(("log", "请先连接矩阵开关。"))
            return
        self._worker(self._do_route_4terminal)

    def _do_route_4terminal(self) -> None:
        cols = tuple(int(v.get()) for v in self.route_cols)
        rows = tuple(self.route_rows[k].get() for k in range(4))
        self.sw.route_4terminal(*cols, rows=rows)
        self.q.put(("log",
                     f"四端法: I+列{cols[0]}/I−列{cols[1]}/V+列{cols[2]}/V−列{cols[3]}"
                     f" ↔ 行{rows}"))

    # ----------------------------------------------------------- 队列处理
    def _process_queue(self) -> None:
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self._log(data)
                elif kind == "status":
                    self.status_var.set(data)
                elif kind == "refresh":
                    self._refresh_grid()
        except queue.Empty:
            pass
        self._queue_after = self.root.after(100, self._process_queue)

    def _refresh_grid(self) -> None:
        if self.sw is None:
            for r in range(N_ROWS):
                for c in range(N_COLS):
                    self.cells[r][c].configure(bg=COLOR_OFF)
            self.frame_var.set("[00000000,00000000,00000000]")
            return
        mat = self.sw.state_matrix()
        for r in range(N_ROWS):
            for c in range(N_COLS):
                self.cells[r][c].configure(
                    bg=COLOR_ON if mat[r][c] else COLOR_OFF
                )
        self.frame_var.set(self.sw.as_frame_text())

    def _log(self, msg: str) -> None:
        self.log_widget.insert("end", msg + "\n")
        self.log_widget.see("end")

    def _on_close(self) -> None:
        if self.sw is not None:
            try:
                self.sw.close()
            except Exception:
                pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    # 中文字体回退；科研机上通常可用
    try:
        default_font = ("Microsoft YaHei", 9)
        root.option_add("*Font", default_font)
    except Exception:
        pass
    MatrixSwitchGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
