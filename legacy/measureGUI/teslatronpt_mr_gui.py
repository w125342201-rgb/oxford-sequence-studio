# -*- coding: utf-8 -*-
"""
TeslatronPT Magnetoresistance R-B measurement GUI
=================================================

Independent program (no import of I-V / R-T GUI code). Depends only on the
shared API/driver packages:

    oxford.oxford_teslatronpt_api   -> TeslatronPT (iTC-VTI / iTC-Probe / iPS)
    keithely6221_2182A.driver        -> Keithley6221 / Keithley2182A

What it does
------------
For each discrete sample temperature (outer loop): set temperature, wait until
stable, then perform ONE complete magnetic-field loop (inner loop) and measure
R(B) at every field point.

The field loop is built from a 3 x N segmented scan table
(rows: segment starts / stops / steps). Adjacent segments share a boundary
field; that shared point is measured only once (de-duplicated).

Two field modes:
    STEP  : per point  -> set FSET, wait until field stable, then measure.
    SWEEP : per segment -> set FSET = segment END once, then sample each point
            in the segment as the magnet ramps through it (fast).

Electric modes:
    DELTA : 6221 + 2182A, HIGH=+I / LOW=-I, R = V_delta / (2 |I|).
    DC    : 6221 sources +I, 2182A reads V directly, R = V / I.

Field control — single, simple rule
------------------------------------
Every field command goes through API set_field_verified(): an EXPLICIT,
non-zero ramp rate (capped at 0.237 T/min = the factory MRFa limit,
2 A/min), then write -> wait -> read back FSET -> retry a limited number
of times if the set did not stick, and confirm ACTN reads RTOS after the
start command. All checks are officially documented iPS behaviour
("check before set", ch.11); no hardware guessing, no watchdogs.

CSV columns (14):
    timestamp, temperature_setpoint_K, temperature_K, vti_temperature_K,
    pressure_mbar, field_setpoint_T, field_T, current_A, voltage_V,
    resistance_ohm, trace_type, raw_trace, electrical_mode, branch
"""

from __future__ import annotations

import csv
import re
import queue
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pyvisa

# Make the project-root packages importable when launched from measureGUI/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oxford.oxford_teslatronpt_api import (
    TeslatronPT,
    TeslatronReadings,
    MercuryCommunicationError,
    MercuryInvalidResponseError,
    MAX_FIELD_T,
    MAX_FIELD_RAMP_T_PER_MIN,
)
from keithely6221_2182A.keithley_6221_2182a_driver import (
    Keithley6221,
    Keithley2182A,
    DEFAULT_NPLC,
    DEFAULT_DELTA_DELAY,
    DEFAULT_DELTA_COUNT,
    DEFAULT_VOLTAGE_RANGE,
    DEFAULT_COMPLIANCE_V,
    MAX_SOURCE_CURRENT_A,
)
from matrix.matrix_switch import MatrixSwitch


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_CURRENT_A = 10e-6
DEFAULT_TEMPERATURES = "2, 5, 10, 20"
# 3 x N default field loop: 0 -> +1 -> 0 -> -1 -> 0  (step 0.1 T, 41 points)
DEFAULT_SEGMENTS = "0, 1, 0, -1\n1, 0, -1, 0\n0.1, 0.1, 0.1, 0.1"

# When the field ramp rate is left blank, use this explicit, valid rate.
DEFAULT_FIELD_RAMP_T_PER_MIN = MAX_FIELD_RAMP_T_PER_MIN  # 0.237

DEFAULT_B_TOL_T = 0.002
DEFAULT_B_STABLE_S = 10.0
DEFAULT_B_SETTLE_S = 1.0
# Fixed dwell at the START of every field segment, before the ramp is
# commanded (default off). Lets the magnet cool between direction reversals:
# a 0 -> -5 ramp issued right after a long 5 -> 0 arrival can trip the
# magnet over-temperature interlock ~0.3 T into the ramp (user-confirmed
# 2026-09-03 root cause), so holding at the segment start first prevents it.
DEFAULT_SEG_DWELL_S = 0.0
DEFAULT_T_TOL_K = 0.02
DEFAULT_T_STABLE_S = 10.0
DEFAULT_DC_SETTLE_S = 0.5
# 2182A voltage range for DC direct readout: fixed uV ranges or
# "auto" (VOLT:RANG:AUTO ON - safe for single-ended DC readings)
DC_VRANGE_CHOICES = ("0.01", "0.1", "1", "10", "100", "auto")
DEFAULT_DC_VRANGE = "0.1"
DEFAULT_POLL_S = 1.0
DEFAULT_OUTPUT = "mr_measurements.csv"

# Matrix-switch batch measurement (same convention as the R-T program):
# four 6221/2182A terminals are routed per pin combo (I+, I-, V+, V-).
DEFAULT_MATRIX_SETTLE_S = 0.1
MATRIX_ROWS = (1, 2, 3, 4)          # row order: I+, I-, V+, V-
DEFAULT_MATRIX_COMBOS = "1,6,2,3\n1,6,4,5"
DEFAULT_MATRIX_OUTPUT = "mr_matrix_measurements.csv"

# Blocking stability waits have no fixed time cap: the run keeps polling
# (with live progress) until the target is reached or the user presses
# STOP - a slow cryostat/magnet must not be cut off mid-approach. The iPS
# abort detector (_RampAbortedByIPS) still fails fast on a controller abort.
ENV_SNAPSHOT_MAX_CONSEC_FAILURES = 10

# Auto-retry count when the iPS aborts a ramp mid-flight (field auto-returns
# to HOLD while still short of the target - reproduced 2026-09-03: a 0 -> -5
# ramp issued right after a long 5 -> 0 arrival was aborted by the iPS after
# ~76 s at -0.3 T; restarting from a settled HOLD always succeeds). Total
# attempts = RAMP_ABORT_RETRIES + 1, then the run fails with a clear error.
RAMP_ABORT_RETRIES = 1

# iPS hardware arrival band (current_epsilon 0.1 A / 8.4237 A/T -> 0.012 T)
# plus margin. When the controller auto-returns to HOLD with the field parked
# closer than this to the target it is a NORMAL end-of-ramp auto-HOLD inside
# the fine-trim zone - NOT an abort (the trim then closes the residual, or the
# crossing/overshoot clauses record the point). The abort detection therefore
# only fires when the ramp is parked clearly short of the target (a genuine
# 2026-09-03 abort stalled ~0.1 T short of the next point; 0.02 T separates
# that cleanly from any legitimate arrival).
IPS_ARRIVAL_BAND_T = 0.02


class _RampAbortedByIPS(Exception):
    """The iPS auto-returned to HOLD while the field was still short of the
    ramp target (the controller aborted the ramp itself - NOT a timeout, NOT
    a lost command: set_field_verified had already confirmed RTOS)."""

    def __init__(self, target, actual, action):
        super().__init__(
            f"iPS aborted the ramp (ACTN={action}) at {actual:.6f} T, "
            f"still short of target {target:g} T.")
        self.target = target
        self.actual = actual
        self.action = action


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class MRPoint:
    timestamp: str
    temperature_setpoint_k: float
    temperature_k: float
    vti_temperature_k: float
    pressure_mbar: float
    field_setpoint_t: float
    field_t: float
    current_a: float
    voltage_v: float
    resistance_ohm: float
    trace_type: str
    raw_trace: str
    electrical_mode: str
    branch: str


@dataclass
class Branch:
    index: int          # 1-based segment number (shown as segN)
    start: float        # FSET target at the start of the segment (absolute;
                        # kept even though points[0] may be trimmed on chained
                        # segments, where it equals the previous segment's END)
    end: float          # FSET target at the far end of the segment
    points: list        # field points within this segment (boundary dedup'd)
    direction: int      # +1 ramping up, -1 ramping down, 0 single point


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class MRMeasurementApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("TeslatronPT Magnetoresistance (R-B)")
        self.geometry("900x560")

        # --- instrument handles (all set in the connect worker) ---
        self.pt = None
        self.src = None
        self.dmm = None
        self.rm = None

        # --- state ---
        self.device_ready = False          # Mercury controllers connected
        self.gpib_ready = False            # GPIB electrical instruments up
        self._connecting = False
        self.measurement_running = False
        self.stop_event = threading.Event()
        self.poll_busy = False
        self._run = {}
        self._env_failures = 0
        self.output_path = Path(DEFAULT_OUTPUT)
        # Matrix batch state: last-applied checkbox (rollback guard) + switch.
        self._applied_matrix_enabled = False
        self.sw = None

        # --- queue / thread plumbing ---
        self.queue = queue.Queue()
        self._queue_after = None
        self._poll_after = None
        self.log_widget = None

        # --- UI vars ---
        self.status_var = tk.StringVar(value="Connecting...")
        self.progress_var = tk.StringVar(value="Idle")

        self.visa_6221_var = tk.StringVar(value="GPIB0::14::INSTR")
        self.visa_2182_var = tk.StringVar(value="GPIB0::7::INSTR")
        self.visa_hint_var = tk.StringVar(
            value="DELTA：仅用 6221（2182A 经串口控制，地址可留空）；DC：2182A 必填。"
        )

        self.temperatures_var = tk.StringVar(value=DEFAULT_TEMPERATURES)
        self.current_var = tk.StringVar(value=f"{DEFAULT_CURRENT_A:.3g}")
        self.nplc_var = tk.StringVar(value=f"{DEFAULT_NPLC:g}")
        self.delta_delay_var = tk.StringVar(value=f"{DEFAULT_DELTA_DELAY:g}")

        self.field_ramp_var = tk.StringVar(value="")  # blank -> default 0.237
        self.b_tol_var = tk.StringVar(value=f"{DEFAULT_B_TOL_T:g}")
        self.b_stable_var = tk.StringVar(value=f"{DEFAULT_B_STABLE_S:g}")
        self.b_settle_var = tk.StringVar(value=f"{DEFAULT_B_SETTLE_S:g}")
        self.seg_dwell_var = tk.StringVar(value=f"{DEFAULT_SEG_DWELL_S:g}")
        self.t_tol_var = tk.StringVar(value=f"{DEFAULT_T_TOL_K:g}")
        self.t_stable_var = tk.StringVar(value=f"{DEFAULT_T_STABLE_S:g}")
        self.dc_settle_var = tk.StringVar(value=f"{DEFAULT_DC_SETTLE_S:g}")
        self.dc_vrange_var = tk.StringVar(value=DEFAULT_DC_VRANGE)

        self.field_mode_var = tk.StringVar(value="STEP")
        self.electrical_mode_var = tk.StringVar(value="DELTA")
        self.auto_pressure_var = tk.BooleanVar(value=True)
        # Disable the 6221 front panel during DELTA runs (622x Reference
        # Manual Table 13-1 + §10: higher speed, no up-to-1 ms step-to-step
        # timing jitter; must be disabled BEFORE a test is armed; *RST does
        # NOT restore it - the worker restores it in its finally block).
        self.disable_frontpanel_var = tk.BooleanVar(value=True)
        self.output_var = tk.StringVar(value=str(self.output_path))

        # Matrix-switch batch measurement (R-T style, optional).
        self.matrix_enabled_var = tk.BooleanVar(value=False)
        self.matrix_simulate_var = tk.BooleanVar(value=False)
        self.visa_matrix_var = tk.StringVar(value="GPIB0::20::INSTR")
        self.matrix_settle_var = tk.StringVar(
            value=f"{DEFAULT_MATRIX_SETTLE_S:g}")
        self.segments_var = tk.StringVar(value=DEFAULT_SEGMENTS)

        self.vti_var = tk.StringVar(value="--")
        self.probe_var = tk.StringVar(value="--")
        self.diff_var = tk.StringVar(value="--")
        self.pressure_var = tk.StringVar(value="--")
        self.field_var = tk.StringVar(value="--")
        self.target_field_var = tk.StringVar(value="--")
        self.ramp_field_var = tk.StringVar(value="--")

        self._build_ui()
        self.refresh_visa_resources()
        self._spawn_connect()

        self._queue_after = self.after(150, self._process_queue)
        self._poll_after = self.after(1000, self._poll_readings)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        outer = ttk.Frame(self, padding=4)
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer)
        head.pack(fill="x", pady=(0, 4))
        ttk.Label(head, text="TeslatronPT R-B (Magnetoresistance)",
                  font=("Segoe UI", 13, "bold")).pack(side="left")
        ttk.Label(head, textvariable=self.status_var,
                  foreground="#666666").pack(side="left", padx=12)

        live = ttk.LabelFrame(outer, text="Live", padding=2)
        live.pack(fill="x", pady=2)
        for name, var, unit in [
            ("VTI", self.vti_var, "K"),
            ("Probe/Sample", self.probe_var, "K"),
            ("Probe-VTI", self.diff_var, "K"),
            ("Pressure", self.pressure_var, "mbar"),
            ("Field", self.field_var, "T"),
            ("B setpoint", self.target_field_var, "T"),
            ("RFST", self.ramp_field_var, "T/min"),
        ]:
            cell = ttk.Frame(live)
            cell.pack(side="left", padx=8)
            ttk.Label(cell, text=name, font=("Segoe UI", 8),
                      foreground="#888888").pack(side="left")
            ttk.Label(cell, textvariable=var,
                      font=("Segoe UI", 10, "bold")).pack(side="left", padx=(3, 0))
            ttk.Label(cell, text=f" {unit}", font=("Segoe UI", 8)).pack(side="left")

        setup = ttk.LabelFrame(outer, text="Measurement setup", padding=3)
        setup.pack(fill="x", pady=3)

        def row_label(r, c, text):
            ttk.Label(setup, text=text).grid(row=r, column=c, sticky="w",
                                             padx=4, pady=2)

        row_label(0, 0, "Temps (K)")
        ttk.Entry(setup, textvariable=self.temperatures_var, width=22).grid(
            row=0, column=1, columnspan=2, sticky="w", padx=2)

        row_label(0, 3, "I |A|")
        ttk.Entry(setup, textvariable=self.current_var, width=10).grid(
            row=0, column=4, padx=2)
        row_label(0, 5, "NPLC")
        ttk.Entry(setup, textvariable=self.nplc_var, width=6).grid(
            row=0, column=6, padx=2)
        row_label(0, 7, "delay(s)")
        ttk.Entry(setup, textvariable=self.delta_delay_var, width=6).grid(
            row=0, column=8, padx=2)

        row_label(1, 0, "Field ramp")
        ttk.Entry(setup, textvariable=self.field_ramp_var, width=10).grid(
            row=1, column=1, padx=2)
        ttk.Label(setup, text="T/min (blank=0.237)").grid(
            row=1, column=2, sticky="w", padx=2)
        row_label(1, 3, "B tol")
        ttk.Entry(setup, textvariable=self.b_tol_var, width=8).grid(
            row=1, column=4, padx=2)
        row_label(1, 5, "B stable")
        ttk.Entry(setup, textvariable=self.b_stable_var, width=6).grid(
            row=1, column=6, padx=2)
        row_label(1, 7, "B settle")
        ttk.Entry(setup, textvariable=self.b_settle_var, width=6).grid(
            row=1, column=8, padx=2)
        row_label(1, 9, "Seg dwell")
        ttk.Entry(setup, textvariable=self.seg_dwell_var, width=6).grid(
            row=1, column=10, padx=2)

        row_label(2, 0, "T tol")
        ttk.Entry(setup, textvariable=self.t_tol_var, width=8).grid(
            row=2, column=1, padx=2)
        row_label(2, 3, "T stable")
        ttk.Entry(setup, textvariable=self.t_stable_var, width=6).grid(
            row=2, column=4, padx=2)
        row_label(2, 5, "DC settle")
        ttk.Entry(setup, textvariable=self.dc_settle_var, width=6).grid(
            row=2, column=6, padx=2)
        ttk.Label(setup, text="V range").grid(
            row=2, column=7, sticky="w", padx=(8, 2))
        ttk.Combobox(setup, textvariable=self.dc_vrange_var, width=6,
                     values=list(DC_VRANGE_CHOICES), state="readonly").grid(
            row=2, column=8, padx=2)
        ttk.Checkbutton(setup, text="Auto pressure",
                        variable=self.auto_pressure_var).grid(
            row=2, column=7, columnspan=2, sticky="w", padx=6)

        row_label(3, 0, "Field mode")
        self.field_mode_combo = ttk.Combobox(
            setup, textvariable=self.field_mode_var,
            values=["STEP", "SWEEP"], width=8, state="readonly")
        self.field_mode_combo.grid(row=3, column=1, padx=2)
        row_label(3, 3, "Elec mode")
        self.elec_mode_combo = ttk.Combobox(
            setup, textvariable=self.electrical_mode_var,
            values=["DELTA", "DC"], width=8, state="readonly")
        self.elec_mode_combo.grid(row=3, column=4, padx=2)
        ttk.Checkbutton(
            setup, text="6221 面板 OFF（DELTA 提速/消抖）",
            variable=self.disable_frontpanel_var,
        ).grid(row=3, column=5, columnspan=3, sticky="w", padx=6)

        # 6221 / 2182A addresses (operator picks from the dropdown; the
        # defaults below are this GPIB bus's real addresses).
        row_label(4, 0, "6221")
        self.addr_6221 = ttk.Combobox(setup, textvariable=self.visa_6221_var,
                                       values=[], width=14)
        self.addr_6221.grid(row=4, column=1, padx=2)
        row_label(4, 3, "2182A")
        self.addr_2182 = ttk.Combobox(setup, textvariable=self.visa_2182_var,
                                       values=[], width=14)
        self.addr_2182.grid(row=4, column=4, padx=2)
        ttk.Label(setup, textvariable=self.visa_hint_var,
                  font=("Segoe UI", 8), foreground="#888888").grid(
            row=4, column=5, columnspan=4, sticky="w", padx=4)

        # Segmented field scan (3 x N)
        seg = ttk.LabelFrame(outer, text="Field sweep (3 x N): row1=starts, "
                                          "row2=stops, row3=steps (T)", padding=3)
        seg.pack(fill="x", pady=3)
        self.segments_text = tk.Text(seg, height=3, width=70,
                                     font=("Consolas", 10))
        self.segments_text.grid(row=0, column=0, padx=2, pady=2)
        self.segments_text.insert("1.0", self.segments_var.get())
        ttk.Button(seg, text="Refresh",
                   command=self.refresh_visa_resources).grid(
            row=0, column=1, padx=4, sticky="n")

        # Matrix-switch batch measurement (optional; same as R-T).
        matrixf = ttk.LabelFrame(
            outer, text="Matrix batch (auto pin switching; forces DELTA + STEP)",
            padding=3)
        matrixf.pack(fill="x", pady=3)
        self.matrix_enabled_cb = ttk.Checkbutton(
            matrixf, text="Enable matrix",
            variable=self.matrix_enabled_var,
            command=self._on_matrix_enabled_changed)
        self.matrix_enabled_cb.grid(row=0, column=0, sticky="w", padx=4)
        ttk.Checkbutton(
            matrixf, text="Simulate",
            variable=self.matrix_simulate_var).grid(
            row=0, column=1, padx=4, sticky="w")
        ttk.Label(matrixf, text="VISA").grid(
            row=0, column=2, padx=(8, 2))
        self.addr_matrix = ttk.Combobox(
            matrixf, textvariable=self.visa_matrix_var, values=[], width=20)
        self.addr_matrix.grid(row=0, column=3, padx=2, sticky="w")
        ttk.Label(matrixf, text="settle(s)").grid(
            row=0, column=4, padx=(10, 2))
        ttk.Entry(matrixf, textvariable=self.matrix_settle_var, width=6).grid(
            row=0, column=5, padx=2)
        ttk.Label(matrixf, text="Combos(I+,I-,V+,V-):").grid(
            row=1, column=0, sticky="w", padx=4)
        self.matrix_combos_text = tk.Text(matrixf, height=2, width=52,
                                          font=("Consolas", 9))
        self.matrix_combos_text.grid(
            row=1, column=1, columnspan=5, sticky="w", padx=4, pady=(0, 2))
        self.matrix_combos_text.insert("1.0", DEFAULT_MATRIX_COMBOS)

        # Bottom controls
        ctrl = ttk.Frame(outer)
        ctrl.pack(fill="x", pady=(4, 2))
        self.start_btn = ttk.Button(ctrl, text="Start",
                                    command=self.start_measurement)
        self.start_btn.pack(side="left", padx=4)
        ttk.Button(ctrl, text="STOP", command=self.stop_measurement).pack(
            side="left", padx=4)
        ttk.Button(ctrl, text="Reset instruments",
                    command=self.reset_instruments).pack(side="left", padx=4)
        ttk.Button(ctrl, text="Browse...",
                   command=self.choose_output).pack(side="left", padx=4)
        ttk.Entry(ctrl, textvariable=self.output_var, width=26).pack(
            side="left", padx=4)
        ttk.Label(ctrl, textvariable=self.progress_var,
                  foreground="#224488").pack(side="left", padx=8)

        log = ttk.LabelFrame(outer, text="Log", padding=2)
        log.pack(fill="both", expand=True, pady=2)
        self.log_widget = tk.Text(log, height=12, width=110,
                                  font=("Consolas", 9))
        self.log_widget.pack(side="left", fill="both", expand=True)
        ttk.Scrollbar(log, command=self.log_widget.yview).pack(
            side="right", fill="y")
        self.log_widget["yscrollcommand"] = None

    def _on_matrix_enabled_changed(self):
        """Matrix batch forces DELTA + STEP (same rule as the R-T program).

        Multi-channel polling is physically incompatible with a field that is
        continuously ramping (SWEEP) and with the DC electrical readout:
        every B point must be settled (STEP) and the Delta measurement is the
        one the matrix batch drives. Unchecking restores both selectors.
        """
        if self.measurement_running:
            self.matrix_enabled_var.set(self._applied_matrix_enabled)
            messagebox.showinfo(
                "Busy",
                "Measurement in progress - the matrix batch setting cannot "
                "be changed now. Stop the measurement first.")
            return
        enabled = self.matrix_enabled_var.get()
        if enabled:
            if self.electrical_mode_var.get().upper() != "DELTA":
                self.electrical_mode_var.set("DELTA")
            if self.field_mode_var.get().upper() != "STEP":
                self.field_mode_var.set("STEP")
            self.elec_mode_combo.state(["disabled"])
            self.field_mode_combo.state(["disabled"])
        else:
            self.elec_mode_combo.state(["!disabled"])
            self.field_mode_combo.state(["!disabled"])
        self._applied_matrix_enabled = enabled

    # ------------------------------------------------------------------
    # Thread-safe messaging
    # ------------------------------------------------------------------

    def put(self, item):
        """item = (kind, message)."""
        self.queue.put(item)

    def _process_queue(self):
        try:
            while True:
                kind, msg = self.queue.get_nowait()
                if kind == "status":
                    self.status_var.set(msg)
                elif kind == "log":
                    self.log(msg)
                elif kind == "progress":
                    self.progress_var.set(msg)
                elif kind == "error":
                    self.log(f"ERROR: {msg}")
                    messagebox.showerror("Error", msg)
                # "live" updates are applied directly in the poll worker
        except queue.Empty:
            pass
        self._queue_after = self.after(150, self._process_queue)

    def log(self, message):
        if self.log_widget is None:
            return
        self.log_widget.insert(
            "end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
        lines = int(self.log_widget.index("end-1c").split(".")[0])
        if lines > 600:
            self.log_widget.delete("1.0", f"{lines - 500}.0")
        self.log_widget.see("end")

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def refresh_visa_resources(self):
        """Enumerate VISA resources into the address dropdowns ONLY.

        Deliberately does NOT touch the current textbox values: the GPIB
        address is the operator's choice (rule), so this never auto-selects
        or overrides what is typed/selected. Defaults already carry this
        bus's real addresses (6221=GPIB0::14, 2182A=GPIB0::7); pick another
        entry from a dropdown if the bus layout changes. No instrument is
        opened here - connection happens only when Connect is pressed.
        """
        try:
            rm = pyvisa.ResourceManager()
            resources = list(rm.list_resources())
            rm.close()
            self.visa_resources = resources
            for combo in (self.addr_6221, self.addr_2182, self.addr_matrix):
                combo["values"] = resources
            self.put(("log", "VISA resources (enumerated only): "
                              f"{resources if resources else 'none found'}"))
        except Exception as exc:
            self.put(("log", f"VISA scan failed: {exc}"))

    def _spawn_connect(self):
        # Startup auto-connect: ONLY the Oxford Mercury controllers, so the
        # live T/B/p readings refresh right away. The GPIB electrical
        # instruments (6221/2182A) are connected lazily when Start (or
        # Reset instruments) is pressed, using the addresses the operator
        # picked in the dropdowns.
        if self._connecting:
            return
        self._connecting = True
        threading.Thread(
            target=self._connect_worker,
            kwargs=dict(elec_mode=self.electrical_mode_var.get().upper()),
            daemon=True,
        ).start()

    def _connect_worker(self, elec_mode):
        self._close_instruments()
        self.device_ready = False
        try:
            self.pt = TeslatronPT()
            ids = {
                "iTC-VTI": self.pt.idn_vti(),
                "iTC-Probe": self.pt.idn_probe(),
                "iPS": self.pt.idn_ips(),
            }
            self.device_ready = True
            self.gpib_ready = False
            self.put(("status", f"Mercury connected ({elec_mode})"))
            for k, v in ids.items():
                self.put(("log", f"{k}: {v}"))
            self.put(("log",
                      "GPIB instruments (6221/2182A) connect when Start is "
                      "pressed - pick their addresses in the dropdowns."))
        except Exception as exc:
            self.put(("error", f"Mercury connection failed: {exc}"))
        finally:
            self._connecting = False

    def _ensure_gpib(self, elec_mode, address_6221, address_2182,
                     matrix_enabled=False, addr_matrix="",
                     matrix_simulate=False):
        """Connect the GPIB electrical instruments if not already up.

        Called at the start of a measurement and by Reset instruments -
        never at startup. Addresses are the operator's choices captured on
        the UI thread. Raises on failure so the caller aborts with a clear
        message; the live Mercury polling is unaffected.
        """
        if self.gpib_ready and self.src is not None:
            return
        # Clean up any half-open handles from an earlier failed attempt.
        for inst in (self.src, self.dmm, self.sw):
            if inst is not None:
                try:
                    inst.close()
                except Exception:
                    pass
        self.src = None
        self.dmm = None
        self.sw = None
        if self.rm is None:
            self.rm = pyvisa.ResourceManager()
        if not address_6221:
            raise ValueError("6221 VISA/GPIB address is empty.")
        self.src = Keithley6221(address_6221, self.rm)
        self.put(("log", f"6221: {self.src.idn()}"))
        if elec_mode == "DC":
            if not address_2182:
                raise ValueError("DC mode requires a 2182A address.")
            self.dmm = Keithley2182A(address_2182, self.rm)
            self.put(("log", f"2182A: {self.dmm.idn()}"))
        else:
            self.dmm = None

        # Matrix switch: required when batch measurement is enabled. A lost
        # matrix would silently destroy the batch semantics, so a connection
        # failure is fatal (no silent fallback).
        if matrix_enabled:
            self.sw = MatrixSwitch(
                resource=addr_matrix or None,
                backend="visa",
                simulate=matrix_simulate,
            )
            # open() syncs the hardware to the cached all-open state;
            # open_all() is belt-and-braces for older caches.
            self.sw.open()
            self.sw.open_all()
            self.put(("log",
                      "Matrix switch connected: all channels opened "
                      "(safe state)."))
        else:
            self.sw = None
        self.gpib_ready = True

    def _close_instruments(self):
        for inst, name in ((self.src, "6221"), (self.dmm, "2182A"),
                           (self.pt, "TeslatronPT"), (self.sw, "MatrixSwitch")):
            try:
                if inst is not None:
                    inst.close()
            except Exception:
                pass
        self.src = None
        self.dmm = None
        self.pt = None
        self.sw = None
        self.device_ready = False
        self.gpib_ready = False

    # ------------------------------------------------------------------
    # Live polling
    # ------------------------------------------------------------------

    def _poll_readings(self):
        if (self.device_ready and self.pt is not None
                and not self.measurement_running
                and not self.stop_event.is_set()
                and not self.poll_busy):
            self.poll_busy = True
            threading.Thread(target=self._poll_worker, daemon=True).start()
        self._poll_after = self.after(1000, self._poll_readings)

    def _poll_worker(self):
        try:
            if self.pt is not None and self.device_ready:
                r = self.pt.readings()
                self.vti_var.set(f"{r.vti_temperature_k:.3f}")
                self.probe_var.set(f"{r.probe_temperature_k:.3f}")
                self.diff_var.set(
                    f"{r.probe_temperature_k - r.vti_temperature_k:.3f}")
                self.pressure_var.set(f"{r.pressure_mbar:.2f}")
                self.field_var.set(f"{r.field_t:.4f}")
                self.target_field_var.set(f"{r.target_field_t:.4f}")
                self.ramp_field_var.set(f"{r.field_ramp_rate_t_per_min:.4f}")
        except Exception:
            pass
        finally:
            self.poll_busy = False

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _float(raw, what):
        try:
            v = float(str(raw).strip())
        except Exception:
            raise ValueError(f"{what} must be a number (got {raw!r}).")
        return v

    @staticmethod
    def _parse_list(raw):
        vals = []
        for tok in re.split(r"[,;\s]+", raw.strip()):
            tok = tok.strip()
            if tok:
                vals.append(float(tok))
        if not vals:
            raise ValueError("List cannot be empty.")
        return vals

    @staticmethod
    def _sequence(start, stop, step):
        """Inclusive linspace from start to stop with given positive step."""
        if step <= 0:
            raise ValueError("Step must be > 0.")
        if start == stop:
            return [start]
        direction = 1 if stop > start else -1
        delta = step * direction
        out = []
        x = start
        for _ in range(2_000_000):
            if direction > 0 and x > stop + 1e-12:
                break
            if direction < 0 and x < stop - 1e-12:
                break
            out.append(round(x, 12))
            x += delta
        else:
            raise ValueError("Segment too long (>2,000,000 points).")
        if not out or abs(out[-1] - stop) > 1e-10:
            out.append(stop)
        return out

    @staticmethod
    def _parse_segments(text):
        lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
        if len(lines) != 3:
            raise ValueError(
                "Field sweep must have exactly 3 rows "
                "(starts / stops / steps).")
        rows = []
        for ln in lines:
            vals = []
            for tok in re.split(r"[,;\s]+", ln.strip()):
                tok = tok.strip()
                if tok:
                    vals.append(float(tok))
            rows.append(vals)
        if not (len(rows[0]) == len(rows[1]) == len(rows[2]) >= 1):
            raise ValueError(
                "The 3 rows must have the same number of columns "
                "(one per segment).")
        for stp in rows[2]:
            if stp <= 0:
                raise ValueError("Every segment step must be > 0.")
        return list(zip(rows[0], rows[1], rows[2]))

    def _build_branches(self, segs):
        branches = []
        for bi, (s, e, stp) in enumerate(segs):
            if s == e:
                pts = [s]
            else:
                pts = self._sequence(s, e, stp)
            direction = 1 if e >= s else -1
            # Drop the leading point of every segment after the first: it equals
            # the previous segment's END, which was already measured there.
            if bi > 0:
                pts = pts[1:]
            branches.append(Branch(index=bi + 1, start=s, end=e, points=pts,
                                   direction=direction))
        return branches

    # ------------------------------------------------------------------
    # Wait helpers
    # ------------------------------------------------------------------

    def _sleep_stop_aware(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.stop_event.is_set():
            time.sleep(min(0.1, end - time.monotonic()))

    # ------------------------------------------------------------------
    # Matrix batch helpers (same convention as the R-T program)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_combos(text):
        """Parse/validate the pin-combination text.

        One combination per line, each ``I+列, I-列, V+列, V-列`` (four column
        numbers 1..24). Spaces, full-width commas and semicolons tolerated.
        """
        combos = []
        seen = set()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            cleaned = line.replace("；", ",").replace(";", ",")
            tokens = [t.strip() for t in cleaned.split(",") if t.strip()]
            if len(tokens) != 4:
                raise ValueError(
                    f"Pin combination must have exactly 4 columns "
                    f"(I+, I-, V+, V-), got {len(tokens)} in: {line!r}")
            try:
                cols = tuple(int(t) for t in tokens)
            except ValueError as exc:
                raise ValueError(
                    f"Pin combination contains a non-integer column: "
                    f"{line!r}") from exc
            for c in cols:
                if not 1 <= c <= 24:
                    raise ValueError(
                        f"Matrix column out of range (1..24): {c} in {line!r}")
            if len(set(cols)) != 4:
                raise ValueError(
                    f"Four columns of a combination must be pairwise "
                    f"distinct: {line!r}")
            if cols in seen:
                raise ValueError(
                    f"Duplicate pin combination (would double the "
                    f"measurement time): {line!r}")
            seen.add(cols)
            combos.append(cols)
        if not combos:
            raise ValueError("At least one pin combination is required.")
        return combos

    def _safe_output_off(self):
        """Force the 6221 output off and current to 0; never raises."""
        try:
            self.src.output_off()
            self.src.set_current(0.0)
        except Exception as exc:
            self.put(("log", f"Warning: failed to force output off: {exc}"))

    def _switch_to_combo(self, combo):
        """Safe matrix switch sequence for one pin combination:
        1) current output OFF (never switch relays under bias);
        2) route I+/I-/V+/V- through the matrix;
        3) wait the settle time for relay contact stabilisation."""
        # 1. Cut current first.
        self.src.output_off()
        try:
            if self.src.output_state():
                self.src.output_off()
                if self.src.output_state():
                    raise RuntimeError(
                        "6221 output refused to turn OFF before matrix "
                        "switch.")
        except AttributeError:
            pass  # older driver: rely on output_off
        self.src.set_current(0.0)

        # 2. Route the four terminals (row order follows MATRIX_ROWS).
        i_plus, i_minus, v_plus, v_minus = combo
        self.sw.route_4terminal(
            i_plus, i_minus, v_plus, v_minus,
            rows=MATRIX_ROWS,
            open_others=True,
        )

        # 3. Relay settle before the current is re-applied.
        settle = self._run.get("matrix_settle", DEFAULT_MATRIX_SETTLE_S)
        self._sleep_stop_aware(settle)

    def _wait_temperature_stable(self, target):
        tol = self._run.get("t_tol", DEFAULT_T_TOL_K)
        stable = self._run.get("t_stable", DEFAULT_T_STABLE_S)
        if tol <= 0 or stable < 0:
            raise ValueError("Invalid temperature stability settings.")
        stable_start = None
        while not self.stop_event.is_set():
            actual = self.pt.probe_temperature()
            if abs(actual - target) <= tol:
                if stable_start is None:
                    stable_start = time.monotonic()
                elapsed = time.monotonic() - stable_start
                self.put(("progress",
                          f"T {actual:.4f}/{target:.4f} K "
                          f"| stable {elapsed:.1f}/{stable:.1f} s"))
                if elapsed >= stable:
                    return
            else:
                stable_start = None
                self.put(("progress",
                          f"T {actual:.4f}/{target:.4f} K "
                          f"| ΔT={actual-target:+.4f} K"))
            time.sleep(DEFAULT_POLL_S)
        raise InterruptedError("Measurement stopped.")

    def _wait_field_reached(self, target, direction):
        """SWEEP: return as soon as the field arrives at target (within tol),
        also returning on overshoot past the target in the ramp direction."""
        tol = self._run.get("b_tol", DEFAULT_B_TOL_T)
        if tol <= 0:
            raise ValueError("B tolerance must be > 0.")
        while not self.stop_event.is_set():
            actual = self.pt.field()
            diff = actual - target
            self.put(("progress",
                      f"B={actual:.4f} T / target={target:.4f} T "
                      f"| ΔB={diff:+.4f} T"))
            if abs(diff) <= tol:
                return
            if direction > 0 and diff >= 0:
                return
            if direction < 0 and diff <= 0:
                return
            # No wait-time cap - the ramp is waited out until arrival or the
            # user presses STOP. The one automatic exit is a genuine iPS
            # abort: if ACTN auto-returned to HOLD while the field is still
            # short of the target, the controller aborted this ramp itself
            # (reproduced 2026-09-03 on a 0 -> -5 ramp right after a long
            # 5 -> 0 arrival). set_field_verified() had already confirmed
            # RTOS, so the command was accepted - the abort is on the iPS
            # side. Parked closer than IPS_ARRIVAL_BAND_T to the target is
            # the normal end-of-ramp auto-HOLD / fine-trim zone (hardware
            # band 0.012 T > b_tol), NOT an abort - keep waiting for the
            # trim.
            action = self._read_magnet_action()
            if (action and not self._action_is_ramping(action)
                    and abs(actual - target) > IPS_ARRIVAL_BAND_T):
                raise _RampAbortedByIPS(target, actual, action)
            time.sleep(DEFAULT_POLL_S)
        raise InterruptedError("Measurement stopped.")

    def _wait_magnet_hold(self, timeout_s: float = 300.0):
        """SWEEP segment-end guard: block until the iPS reports HOLD.

        Official workflow (handbook sec.5): a ramp ends when the output
        enters the setpoint's current-epsilon band and the unit drops back
        into HOLD (fine trim). The next RFST/FSET/RTOS sequence is only
        defined from HOLD; issuing it mid-ramp is undefined behaviour (the
        command is accepted but its effect is not specified).

        With the default tight B tolerance, _wait_field_reached already
        implies HOLD at the segment end; this explicit wait keeps that
        guarantee independent of the user's tolerance choice (a tolerance
        looser than the ~0.012 T epsilon band would otherwise release the
        next segment command while the iPS is still in To Set). CLMP is
        passed through: set_field_verified() performs the official
        unclamp (HOLD) sequence itself.
        """
        deadline = time.monotonic() + timeout_s
        while not self.stop_event.is_set():
            try:
                action = self.pt.magnet_action().upper()
            except (MercuryCommunicationError, MercuryInvalidResponseError):
                # A failed status read must not wedge the run; keep polling
                # until the deadline. The RTOS confirmation inside
                # set_field_verified remains the real gate.
                action = ""
            if "HOLD" in action or "CLMP" in action or "CLAMP" in action:
                return
            if time.monotonic() > deadline:
                raise TimeoutError(
                    "iPS never returned to HOLD before the next SWEEP "
                    f"segment (last ACTN={action or 'unreadable'}). "
                    "Stop the run and check the magnet state / connections."
                )
            time.sleep(DEFAULT_POLL_S)
        raise InterruptedError("Measurement stopped.")

    def _wait_field_stable(self, target):
        """STEP: set earlier; wait until field is stable for b_stable seconds."""
        tol = self._run.get("b_tol", DEFAULT_B_TOL_T)
        stable = self._run.get("b_stable", DEFAULT_B_STABLE_S)
        if tol <= 0 or stable < 0:
            raise ValueError("Invalid field stability settings.")
        stable_start = None
        while not self.stop_event.is_set():
            actual = self.pt.field()
            if abs(actual - target) <= tol:
                if stable_start is None:
                    stable_start = time.monotonic()
                elapsed = time.monotonic() - stable_start
                self.put(("progress",
                          f"B {actual:.4f}/{target:.4f} T "
                          f"| stable {elapsed:.1f}/{stable:.1f} s"))
                if elapsed >= stable:
                    return
            else:
                stable_start = None
                self.put(("progress",
                          f"B {actual:.4f}/{target:.4f} T "
                          f"| ΔB={actual-target:+.4f} T"))
                # The wait has no time cap, so the only automatic exit is a
                # genuine iPS abort: if the controller has auto-returned to
                # HOLD while the field is still short of the target, it
                # aborted this move itself. Same arrival-band guard as
                # _wait_field_reached: parked within IPS_ARRIVAL_BAND_T of
                # the target is the normal end-of-ramp auto-HOLD / fine-trim
                # zone, not an abort.
                action = self._read_magnet_action()
                if (action and not self._action_is_ramping(action)
                        and abs(actual - target) > IPS_ARRIVAL_BAND_T):
                    raise _RampAbortedByIPS(target, actual, action)
            time.sleep(DEFAULT_POLL_S)
        raise InterruptedError("Measurement stopped.")

    # ------------------------------------------------------------------
    # Ramp-abort detection / auto-retry (2026-09-03)
    #
    # Real-machine finding: after a LONG ramp arrives (e.g. 5 -> 0 over
    # ~21 min) the immediately-following opposite-sign segment (0 -> -5)
    # can be aborted BY THE iPS ITSELF ~1-2 min into the ramp: ACTN drops
    # from To Set back to HOLD while the field sits short of target. The
    # software chain is clean (FSET read-back verified, RTOS confirmed);
    # restarting from a settled HOLD always succeeds. So: detect the
    # auto-return (the wait has no time cap any more) and re-issue
    # the segment command once from HOLD.
    # ------------------------------------------------------------------

    @staticmethod
    def _action_is_ramping(action):
        """True while the iPS is executing a ramp (either token spelling)."""
        return any(k in action for k in ("RTOS", "RTOZ", "TO SET", "TO ZERO"))

    def _read_magnet_action(self):
        """Return the current iPS ACTN token (upper-cased), or '' when the
        read fails - a failed status read must never raise here."""
        try:
            return self.pt.magnet_action().upper()
        except (MercuryCommunicationError, MercuryInvalidResponseError):
            return ""

    def _field_is_mid_segment(self, b, tol):
        """True when the field currently sits INSIDE segment b (past its
        start, short of its end). Used on retry to decide whether homing
        back to b.start is required: a mid-segment abort must resume the
        ramp from where it stopped, never retreat to the start."""
        f = self.pt.field()
        lo, hi = min(b.start, b.end), max(b.start, b.end)
        return (abs(f - b.start) > tol and abs(f - b.end) > tol
                and lo - tol < f < hi + tol)

    def _command_field_stable_retry(self, target, ramp_rate, label):
        """STEP-style move: set_field_verified(target) then wait for arrival
        + stability. Retries once if the iPS aborts the move (auto-return to
        HOLD short of target), then re-raises."""
        for attempt in range(RAMP_ABORT_RETRIES + 1):
            try:
                self.pt.set_field_verified(
                    target, ramp_rate,
                    log=lambda m: self.put(("log", m)))
                self._wait_field_stable(target)
                return
            except _RampAbortedByIPS as exc:
                if attempt >= RAMP_ABORT_RETRIES:
                    raise
                self.put(("log",
                          f"{label}: move to {target:g} T aborted by iPS "
                          f"({exc.actual:.4g} T, auto HOLD) - retrying once "
                          f"from HOLD."))
                self._wait_magnet_hold()

    def _run_sweep_segment(self, b, T, elec_mode, completed, total,
                           ramp_rate):
        """SWEEP one segment: home to b.start if needed, command FSET=b.end
        ONCE, then sample each point as the magnet ramps through it.

        Auto-retries the whole ramp once if the iPS aborts it mid-segment:
        the end command is re-issued from HOLD and sampling resumes at the
        first unrecorded point (already-recorded points are never
        duplicated, and a mid-segment abort never retreats to b.start)."""
        b_tol = self._run.get("b_tol", DEFAULT_B_TOL_T)
        remaining = list(b.points)   # pop(0) after each successful record
        for attempt in range(RAMP_ABORT_RETRIES + 1):
            # Segment entry: home to the absolute segment start unless this
            # is a retry of a MID-SEGMENT abort (field already inside the
            # ramp - re-issue the end command from where it stopped).
            if attempt == 0 or not self._field_is_mid_segment(b, b_tol):
                if abs(self.pt.field() - b.start) > b_tol:
                    self.put(("log",
                              f"SWEEP seg{b.index}: field "
                              f"{self.pt.field():.4g} T is off the segment "
                              f"start {b.start:g} T - moving there first."))
                    self._command_field_stable_retry(
                        b.start, ramp_rate, f"SWEEP seg{b.index} homing")
            self._wait_magnet_hold()
            # Segment-start dwell (default off): hold the field here BEFORE
            # commanding the ramp, letting the magnet cool between direction
            # reversals (the over-temperature interlock can abort a 0 -> -5
            # ramp issued right after 5 -> 0 - user-confirmed root cause,
            # 2026-09-03). Applied on every attempt, so a mid-ramp abort
            # retry also cools first - giving a thermal abort time to clear
            # instead of failing the retry seconds later.
            seg_dwell = self._run.get("seg_dwell", DEFAULT_SEG_DWELL_S)
            if seg_dwell > 0:
                self.put(("log",
                          f"SWEEP seg{b.index}: dwelling {seg_dwell:g} s at "
                          f"{self.pt.field():.4f} T before ramping."))
                self._sleep_stop_aware(seg_dwell)
            self.pt.set_field_verified(
                b.end, ramp_rate,
                log=lambda m: self.put(("log", m)))
            self.put(("log",
                      f"SWEEP seg{b.index}: commanded FSET={b.end:g} T "
                      f"(ramp {ramp_rate:g} T/min)"
                      + (f" [retry {attempt}]" if attempt else "")))
            try:
                while remaining:
                    bpt = remaining[0]
                    self._wait_field_reached(bpt, b.direction)
                    if self._run["b_settle"] > 0:
                        self._sleep_stop_aware(self._run["b_settle"])
                    completed = self._record(
                        bpt, T, b.index, elec_mode, completed, total)
                    remaining.pop(0)
                return completed
            except _RampAbortedByIPS as exc:
                if attempt >= RAMP_ABORT_RETRIES:
                    raise RuntimeError(
                        f"SWEEP seg{b.index}: ramp toward {b.end:g} T was "
                        f"aborted by the iPS controller twice (auto HOLD at "
                        f"{exc.actual:.4g} T, short of target). Stop and "
                        f"restart the run from a settled HOLD.") from exc
                self.put(("log",
                          f"SWEEP seg{b.index}: ramp aborted by iPS at "
                          f"{exc.actual:.4g} T (auto HOLD before "
                          f"{remaining[0]:g} T) - re-issuing FSET="
                          f"{b.end:g} T once from HOLD."))
                self._wait_magnet_hold()
        return completed

    # ------------------------------------------------------------------
    # Electrical measurement
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_delta_trace(raw):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if len(parts) != 1:
            return None
        try:
            return float(parts[0])
        except ValueError:
            return None

    def _measure_dc(self):
        current = self._run["current"]
        nplc = self._run["nplc"]
        settle = self._run["dc_settle"]
        if self.dmm is None:
            raise RuntimeError("DC mode requires a connected 2182A.")
        self.src.set_current(current)
        if settle > 0:
            self._sleep_stop_aware(settle)
        try:
            voltage = self.dmm.read()
        except Exception:
            self._sleep_stop_aware(0.5)
            voltage = self.dmm.read()
        resistance = voltage / current
        return current, voltage, resistance, "DC", "DC_DIRECT"

    def _resolve_vrange(self, mode: str) -> float:
        """Resolve the shared V-range dropdown for a given mode.

        DC accepts "auto" (VOLT:RANG:AUTO ON). Delta requires one fixed range
        for the HIGH/LOW pair - "auto" falls back to 0.1 V with a warning.
        """
        spec = str(self._run.get("dc_vrange", "0.1"))
        if spec.lower() == "auto":
            if mode == "delta":
                self.put(("log",
                          "Warning: auto range is not valid for Delta "
                          "(HIGH/LOW must share one range); using 0.1 V."))
                return 0.1
            return 1.0
        return float(spec)

    def _measure_delta(self, reset_before=None, configure_2182=True):
        current = self._run["current"]
        nplc = self._run["nplc"]
        delay = self._run["delta_delay"]
        if reset_before is None:
            reset_before = True
        last_exc = None
        for attempt in range(2):
            rb = True if attempt > 0 else reset_before
            cfg = True if attempt > 0 else configure_2182
            try:
                result = self.src.run_delta_once(
                    high=current, low=-current, delay=delay,
                    count=DEFAULT_DELTA_COUNT, feed="CALC1", timeout_s=60.0,
                    configure_2182=cfg,
                    voltage_range=self._resolve_vrange("delta"),
                    nplc=nplc, reset_before=rb,
                )
                raw = result["trace_data"]
                trace_type = result["data_type"]
                voltage_delta = self._parse_delta_trace(raw)
                if voltage_delta is None or voltage_delta != voltage_delta \
                        or abs(voltage_delta) == float("inf"):
                    raise RuntimeError(
                        "Delta completed but the trace could not be parsed as a "
                        f"single finite voltage. TYPE={trace_type}; RAW={raw!r}")
                if abs(voltage_delta) > 1000.0:
                    raise RuntimeError(
                        f"Delta returned an implausible VΔ={voltage_delta:.6g} V "
                        "(2182A error/over-range sentinel) — treated as "
                        "corruption.")
                # V_delta = V(+I) - V(-I); R = V_delta / (2 I).
                resistance = voltage_delta / (2.0 * current)
                return current, voltage_delta, resistance, trace_type, raw
            except Exception as exc:
                last_exc = exc
                self.put(("log",
                          f"  delta attempt {attempt+1} failed: {exc}; "
                          f"retrying with full reset..."))
                self._sleep_stop_aware(0.5)
        raise last_exc

    # ------------------------------------------------------------------
    # Environment snapshot
    # ------------------------------------------------------------------

    def _env_snapshot(self):
        try:
            r = self.pt.readings()
            self._env_failures = 0
            return r
        except Exception:
            self._env_failures += 1
            if self._env_failures >= ENV_SNAPSHOT_MAX_CONSEC_FAILURES:
                raise RuntimeError(
                    "Environment snapshot failed "
                    f"{ENV_SNAPSHOT_MAX_CONSEC_FAILURES}x; aborting run.")
            return TeslatronReadings(
                vti_temperature_k=float("nan"),
                probe_temperature_k=float("nan"),
                pressure_mbar=float("nan"),
                field_t=float("nan"),
                target_field_t=float("nan"),
                field_ramp_rate_t_per_min=float("nan"),
            )

    # ------------------------------------------------------------------
    # CSV
    # ------------------------------------------------------------------

    CSV_HEADER = [
        "timestamp", "temperature_setpoint_K", "temperature_K",
        "vti_temperature_K", "pressure_mbar", "field_setpoint_T",
        "field_T", "current_A", "voltage_V", "resistance_ohm",
        "trace_type", "raw_trace", "electrical_mode", "branch",
    ]

    def _prepare_output(self):
        if not hasattr(self, "_csv_lock"):
            self._csv_lock = threading.Lock()
        combos = self._run.get("combos")
        wide = bool(self._run.get("matrix_enabled")) and bool(combos)
        self._csv_wide = wide

        path = Path(self.output_var.get()).expanduser()
        if path.suffix.lower() != ".csv":
            path = path.with_suffix(".csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.exists() and path.stat().st_size > 0

        if wide:
            # Wide format: ONE row per (T, B) point, one R_<combo> column per
            # pin combination (combo order fixed: I+, I-, V+, V-).
            tags = [",".join(map(str, c)) for c in combos]
            header = [
                "timestamp",
                "temperature_setpoint_K",
                "temperature_K",
                "vti_temperature_K",
                "pressure_mbar",
                "field_setpoint_T",
                "field_T",
                "current_A",
                "electrical_mode",
                "branch",
            ] + [f"R_{t.replace(',', '-')}" for t in tags]
            self._wide_combo_tags = tags

            if exists:
                try:
                    with path.open("r", encoding="utf-8", newline="") as rf:
                        existing = rf.readline().strip()
                    if existing != ",".join(header):
                        # Combo set differs -> spill to a fresh timestamped
                        # file instead of misaligning the existing columns.
                        new_path = path.with_name(
                            f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S}"
                            f"{path.suffix}")
                        self.put(("log",
                                  "Warning: existing CSV header/combo set "
                                  "differs; writing a fresh file "
                                  f"{new_path.name} to avoid column "
                                  "misalignment."))
                        path = new_path
                        exists = False
                except Exception as exc:
                    self.put(("log", f"CSV header check failed: {exc}"))

            self.put(("log",
                      "Matrix wide CSV: one row per (T,B) point; columns "
                      + ", ".join(f"R_{t.replace(',', '-')}" for t in tags)
                      + "  [combo order: I+,I-,V+,V-]"))
            with self._csv_lock:
                with path.open("a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    if not exists:
                        writer.writerow(header)
                        f.flush()
            self.output_path = path
            return

        with self._csv_lock:
            with path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not exists:
                    writer.writerow(self.CSV_HEADER)
                    f.flush()
        self.output_path = path

    def _append_point(self, point: MRPoint):
        row = [
            point.timestamp,
            f"{point.temperature_setpoint_k:.10g}",
            f"{point.temperature_k:.10g}",
            f"{point.vti_temperature_k:.10g}",
            f"{point.pressure_mbar:.10g}",
            f"{point.field_setpoint_t:.10g}",
            f"{point.field_t:.10g}",
            f"{point.current_a:.12g}",
            f"{point.voltage_v:.12g}",
            f"{point.resistance_ohm:.12g}",
            point.trace_type,
            point.raw_trace,
            point.electrical_mode,
            point.branch,
        ]
        with self._csv_lock:
            with self.output_path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(row)
                f.flush()

    # ------------------------------------------------------------------
    # Start / validation
    # ------------------------------------------------------------------

    def choose_output(self):
        path = filedialog.asksaveasfilename(
            title="Save R-B data", defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if path:
            self.output_var.set(path)

    def start_measurement(self):
        if not self.device_ready:
            messagebox.showerror(
                "Not connected",
                "Oxford Mercury controllers are not connected (live readings "
                "would be unavailable). Restart the program to reconnect.")
            return
        if self.measurement_running:
            return

        try:
            current = abs(self._float(self.current_var.get(), "Delta current"))
            if current <= 0:
                raise ValueError("Delta current must be > 0.")
            if current > MAX_SOURCE_CURRENT_A:
                raise ValueError(
                    f"Bias current {current:g} A exceeds 6221 capability "
                    f"(±{MAX_SOURCE_CURRENT_A*1000:.0f} mA).")

            t_tol = self._float(self.t_tol_var.get(), "T tolerance")
            if t_tol <= 0:
                raise ValueError("T tolerance must be > 0 K.")
            t_stable = self._float(self.t_stable_var.get(), "T stable time")
            if t_stable < 0:
                raise ValueError("T stable must be >= 0 s.")

            b_tol = self._float(self.b_tol_var.get(), "B tolerance")
            if b_tol <= 0:
                raise ValueError("B tolerance must be > 0 T.")
            b_stable = self._float(self.b_stable_var.get(), "B stable time")
            if b_stable < 0:
                raise ValueError("B stable must be >= 0 s.")
            b_settle = self._float(self.b_settle_var.get(), "B settle")
            if b_settle < 0:
                raise ValueError("B settle must be >= 0 s.")
            seg_dwell = self._float(self.seg_dwell_var.get(), "Seg dwell")
            if seg_dwell < 0:
                raise ValueError("Seg dwell must be >= 0 s.")

            # Explicit, valid, non-zero ramp rate (the field-freeze fix).
            raw_rate = self.field_ramp_var.get().strip()
            if raw_rate == "":
                ramp_rate = DEFAULT_FIELD_RAMP_T_PER_MIN
            else:
                ramp_rate = self._float(raw_rate, "Field ramp rate")
            if ramp_rate <= 0 or ramp_rate > MAX_FIELD_RAMP_T_PER_MIN:
                raise ValueError(
                    f"Field ramp rate must be >0 and <= "
                    f"{MAX_FIELD_RAMP_T_PER_MIN:g} T/min.")
            if self.field_mode_var.get().upper() == "SWEEP" and b_settle <= 0:
                self.put(("log",
                          "Note: SWEEP with B settle=0 samples exactly at "
                          "arrival (no extra soak)."))

            temperatures = self._parse_list(self.temperatures_var.get())
            segments = self._parse_segments(self.segments_text.get("1.0", "end"))
            branches = self._build_branches(segments)
            for b in branches:
                if not b.points:
                    raise ValueError(
                        f"Segment seg{b.index} produced no field points.")
                for p in b.points:
                    if abs(p) > MAX_FIELD_T:
                        raise ValueError(
                            f"Field {p:g} T exceeds nominal "
                            f"±{MAX_FIELD_T:g} T input limit.")

            nplc = self._float(self.nplc_var.get(), "NPLC")
            if not 0.01 <= nplc <= 60:
                raise ValueError("NPLC must be 0.01..60.")
            delta_delay = self._float(self.delta_delay_var.get(), "Delta delay")
            if delta_delay < 0:
                raise ValueError("Delta delay must be >= 0 s.")

            elec_mode = self.electrical_mode_var.get().upper()
            if elec_mode == "DC":
                dc_settle = self._float(self.dc_settle_var.get(), "DC settle")
                if dc_settle < 0:
                    raise ValueError("DC settle must be >= 0 s.")
            else:
                dc_settle = 0.0

            # Matrix batch (optional; R-T style). Enabled -> DELTA + STEP
            # only (multi-channel polling needs a settled field and the
            # Delta readout). DC mode is incompatible with relay switching.
            matrix_enabled = bool(self.matrix_enabled_var.get())
            combos = []
            if matrix_enabled:
                if elec_mode != "DELTA":
                    raise ValueError(
                        "Matrix batch requires DELTA electrical mode "
                        "(the GUI forces it when you tick Enable matrix).")
                if self.field_mode_var.get().upper() != "STEP":
                    raise ValueError(
                        "Matrix batch requires STEP field mode (SWEEP keeps "
                        "the field moving - impossible to poll channels).")
                combos = self._parse_combos(
                    self.matrix_combos_text.get("1.0", "end"))
                matrix_settle = self._float(
                    self.matrix_settle_var.get(), "Matrix settle")
                if matrix_settle < 0:
                    raise ValueError("Matrix settle must be >= 0 s.")
            else:
                matrix_settle = 0.0

            self._run = {
                "mode": self.field_mode_var.get().upper(),
                "elec_mode": elec_mode,
                "current": current,
                "nplc": nplc,
                "delta_delay": delta_delay,
                "auto_pressure": bool(self.auto_pressure_var.get()),
                "b_tol": b_tol,
                "b_stable": b_stable,
                "b_settle": b_settle,
                "seg_dwell": seg_dwell,
                "t_tol": t_tol,
                "t_stable": t_stable,
                "dc_settle": dc_settle,
                "dc_vrange": self.dc_vrange_var.get().strip() or "0.1",
                "ramp_rate": ramp_rate,
                "matrix_enabled": matrix_enabled,
                "matrix_simulate": bool(self.matrix_simulate_var.get()),
                "combos": combos,
                "matrix_settle": matrix_settle,
                # DELTA-only performance option (manual Table 13-1); ignored
                # in DC mode.
                "disable_frontpanel": bool(
                    self.disable_frontpanel_var.get()),
                # GPIB addresses captured on the UI thread (worker connects
                # them lazily at Start - never touches Tk itself).
                "addr_6221": self.visa_6221_var.get().strip(),
                "addr_2182": self.visa_2182_var.get().strip(),
                "addr_matrix": self.visa_matrix_var.get().strip(),
            }
            self._csv_lock = threading.Lock()
            self._prepare_output()

        except Exception as exc:
            messagebox.showerror("Input error", str(exc))
            return

        total = len(temperatures) * sum(len(b.points) for b in branches)
        self.put(("log",
                  f"Run start: {len(temperatures)} T x {total} B points "
                  f"({self._run['mode']}/{elec_mode}), "
                  f"ramp {ramp_rate:g} T/min."))

        self.measurement_running = True
        self.stop_event.clear()
        self.start_btn.state(["disabled"])
        threading.Thread(
            target=self._measurement_worker,
            args=(temperatures, branches, ramp_rate, total),
            daemon=True,
        ).start()

    # ------------------------------------------------------------------
    # Measurement worker — the simple, complete loop
    # ------------------------------------------------------------------

    def _measurement_worker(self, temperatures, branches, ramp_rate, total):
        elec_mode = self._run["elec_mode"]
        mode = self._run["mode"]

        # Wait for any in-flight poll to finish before touching instruments.
        self._wait_poll_idle()

        try:
            # Lazy GPIB connect (operator-chosen addresses captured on the
            # UI thread). Never happens at startup - only now that a run is
            # actually starting. Fail fast with a clear message; the live
            # Mercury readings keep running unaffected.
            matrix_enabled = bool(self._run.get("matrix_enabled", False))
            self._ensure_gpib(
                elec_mode,
                self._run.get("addr_6221", ""),
                self._run.get("addr_2182", ""),
                matrix_enabled=matrix_enabled,
                addr_matrix=self._run.get("addr_matrix", ""),
                matrix_simulate=bool(self._run.get("matrix_simulate", False)),
            )
            # Source prep (no auto-reset; operator resets explicitly).
            try:
                self.src.set_compliance(DEFAULT_COMPLIANCE_V)
            except Exception as exc:
                self.put(("log", f"Compliance warning: {exc}"))
            self.src.output_off()
            self.src.set_current(0.0)

            # DELTA-only performance mode (manual Table 13-1 + §10): with the
            # front panel off the 6221 runs faster and the up-to-1 ms
            # step-to-step timing jitter of Delta is eliminated. It MUST be
            # disabled before any Delta test is armed (a mid-test disable
            # forces a 2 s pause) - here, during run preparation, is the
            # earliest safe point. Always restored in the finally block:
            # *RST does NOT bring the panel back.
            self._frontpanel_disabled = False
            if elec_mode == "DELTA" and self._run.get(
                    "disable_frontpanel", True):
                try:
                    self.src.set_display_enabled(False)
                    self._frontpanel_disabled = True
                    self.put((
                        "log",
                        "6221 front panel disabled for the DELTA run "
                        "(manual: higher speed, no 1 ms step-to-step "
                        "jitter). It is restored automatically when the run "
                        "ends; to recover manually press LOCAL or DISP on "
                        "the panel."
                    ))
                except Exception as exc:
                    # A convenience feature must not abort the run.
                    self.put((
                        "log",
                        f"⚠ front-panel disable failed (run continues): {exc}"
                    ))

            if elec_mode == "DC" and self.dmm is not None:
                try:
                    self.dmm.configure_voltage(channel=1)
                    vspec = str(self._run.get("dc_vrange", "0.1"))
                    if vspec.lower() == "auto":
                        self.dmm.set_range(1.0, auto=True)
                    else:
                        self.dmm.set_range(float(vspec), auto=False)
                    self.dmm.set_nplc(self._run["nplc"])
                    self.src.set_current(self._run["current"])
                    self.src.output_on()
                    if self._run["dc_settle"] > 0:
                        self._sleep_stop_aware(self._run["dc_settle"])
                    self.put(("log",
                              f"DC: 6221 output ON (I={self._run['current']:g} A) "
                              f"for the whole run."))
                except Exception as exc:
                    self.put(("log", f"DC output warning: {exc}"))

            completed = 0
            regime = None  # auto-pressure hysteresis tracker (outside loops)

            for ti, T in enumerate(temperatures, start=1):
                if self.stop_event.is_set():
                    break

                self.put(("progress",
                          f"Temp {ti}/{len(temperatures)}: setting {T:g} K"))
                self.pt.set_sample_temperature(T)
                self._wait_temperature_stable(T)

                if self._run["auto_pressure"]:
                    regime, actions = self.pt.enforce_thermal_link(
                        actual_probe_temperature_k=self.pt.probe_temperature(),
                        probe_target_temperature_k=T,
                        auto_pressure=True,
                        previous_regime=regime,
                    )
                    if actions:
                        self.put(("log",
                                  f"Auto-pressure/link: {', '.join(actions)}"))

                self.put(("log",
                          f"T stabilized at {self.pt.probe_temperature():.4f} K "
                          f"(VTI {self.pt.vti_temperature():.4f} K); "
                          f"starting field loop."))

                # ---- one complete field loop for this temperature ----
                for b in branches:
                    if self.stop_event.is_set():
                        break

                    if mode == "SWEEP":
                        # One segment: home to the absolute segment start if
                        # needed, command FSET = segment end ONCE, then sample
                        # each point as the magnet ramps through it. Aborts by
                        # the iPS mid-ramp are detected fast and the segment
                        # is re-issued once from HOLD (see _run_sweep_segment).
                        completed = self._run_sweep_segment(
                            b, T, elec_mode, completed, total, ramp_rate)
                    else:  # STEP
                        # Per-point absolute move with the same fast-abort
                        # detection + one automatic retry. Same segment-start
                        # dwell as SWEEP: cool the magnet before the first
                        # move of a new segment (direction reversals are the
                        # over-temperature trigger - see _run_sweep_segment).
                        seg_dwell = self._run.get(
                            "seg_dwell", DEFAULT_SEG_DWELL_S)
                        if seg_dwell > 0:
                            self.put(("log",
                                      f"STEP seg{b.index}: dwelling "
                                      f"{seg_dwell:g} s before segment."))
                            self._sleep_stop_aware(seg_dwell)
                        for bpt in b.points:
                            self._command_field_stable_retry(
                                bpt, ramp_rate, f"STEP seg{b.index}")
                            if matrix_enabled:
                                completed = self._record_matrix(
                                    bpt, T, b.index, completed, total)
                            else:
                                completed = self._record(
                                    bpt, T, b.index, elec_mode, completed, total)

            self.put(("progress",
                      "Measurement finished." if not self.stop_event.is_set()
                      else "Measurement stopped by user."))
            self.put(("log",
                      f"Done. {completed}/{total} points recorded to "
                      f"{self.output_path.name}"))

        except Exception as exc:
            self.put(("error", f"Measurement failed: {exc}"))
        finally:
            # Restore the 6221 front panel no matter how the run ended
            # (finished / stopped / failed): *RST does NOT restore it
            # (manual Table 13-1 note).
            if getattr(self, "_frontpanel_disabled", False):
                try:
                    self.src.set_display_enabled(True)
                    self.put(("log", "6221 front panel restored."))
                except Exception as exc:
                    self.put((
                        "log",
                        f"⚠ front-panel restore failed: {exc} - press "
                        "LOCAL/DISP on the panel or power-cycle to recover."
                    ))
                self._frontpanel_disabled = False
            try:
                if self.src is not None:
                    self.src.output_off()
            except Exception:
                pass
            self.measurement_running = False
            self.start_btn.state(["!disabled"])

    def _record(self, bpt, T, branch_idx, elec_mode, completed, total):
        env = self._env_snapshot()
        if elec_mode == "DC":
            current, voltage, resistance, trace_type, raw = self._measure_dc()
        else:
            current, voltage, resistance, trace_type, raw = self._measure_delta()
        point = MRPoint(
            timestamp=datetime.now().isoformat(timespec="milliseconds"),
            temperature_setpoint_k=T,
            temperature_k=env.probe_temperature_k,
            vti_temperature_k=env.vti_temperature_k,
            pressure_mbar=env.pressure_mbar,
            field_setpoint_t=bpt,
            field_t=env.field_t,
            current_a=current,
            voltage_v=voltage,
            resistance_ohm=resistance,
            trace_type=trace_type,
            raw_trace=raw,
            electrical_mode=elec_mode,
            branch=f"seg{branch_idx}",
        )
        self._append_point(point)
        completed += 1
        self.put(("progress",
                  f"[{completed}/{total}] T={T:g}K B={bpt:g}T "
                  f"R={resistance:.6g} ohm ({elec_mode})"))
        return completed

    def _record_matrix(self, bpt, T, branch_idx, completed, total):
        """Matrix batch: ONE wide CSV row per (T, B) point.

        Every pin combo of the combo list is routed in turn and measured with
        the Delta method. A failed combo is skipped (row cell stays empty)
        rather than aborting the whole run - the error is logged. Mirrors the
        R-T batch behaviour, including the full reset + 2182A reconfigure on
        EVERY combo (fixes the intermittent 9.91E+37 sentinel / STAT:OPER
        timeout seen when the Delta state was reused across matrix switches).
        """
        combos = self._run["combos"]
        env = self._env_snapshot()
        total_combos = len(combos)
        combo_results = {}
        for ci, combo in enumerate(combos, start=1):
            if self.stop_event.is_set():
                break
            combo_tag = ",".join(map(str, combo))
            self.put(("progress",
                      f"[{completed}/{total}] T={T:g}K B={bpt:g}T "
                      f"combo {ci}/{total_combos}: (I+={combo[0]}, "
                      f"I-={combo[1]}, V+={combo[2]}, V-={combo[3]})"))
            try:
                # Output off -> route -> settle, then measure with a FULL
                # reset (same policy as the R-T matrix batch).
                self._switch_to_combo(combo)
                _cur, _vd, resistance, _tt, _raw = self._measure_delta(
                    reset_before=True, configure_2182=True)
                combo_results[combo_tag] = resistance
                self.put(("log",
                          f"  combo {combo_tag}: R={resistance:.6g} ohm"))
            except Exception as exc:
                # Never switch away under a live fault; force the output off,
                # leave the cell empty so the wide row stays aligned.
                self._safe_output_off()
                combo_results[combo_tag] = None
                self.put(("log",
                          f"  combo {combo_tag} failed, skipped: {exc}"))
        self._append_matrix_row(T, bpt, branch_idx, combo_results)
        self._safe_output_off()
        completed += 1
        return completed

    def _append_matrix_row(self, T, bpt, branch_idx, combo_results):
        """Write ONE matrix wide row for this (T, B) point."""
        env = self._env_snapshot()
        tags = self._wide_combo_tags
        row = [
            datetime.now().isoformat(timespec="milliseconds"),
            f"{T:.10g}",
            f"{env.probe_temperature_k:.10g}",
            f"{env.vti_temperature_k:.10g}",
            f"{env.pressure_mbar:.10g}",
            f"{bpt:.10g}",
            f"{env.field_t:.10g}",
            f"{self._run['current']:.12g}",
            "DELTA",
            f"seg{branch_idx}",
        ]
        for tag in tags:
            r = combo_results.get(tag)
            row.append("" if r is None else f"{r:.12g}")
        with self._csv_lock:
            with self.output_path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(row)
                f.flush()

    def _wait_poll_idle(self, timeout=12.0):
        deadline = time.monotonic() + timeout
        while self.poll_busy and time.monotonic() < deadline:
            time.sleep(0.05)

    # ------------------------------------------------------------------
    # Stop / reset
    # ------------------------------------------------------------------

    def stop_measurement(self):
        if not self.measurement_running:
            return
        self.stop_event.set()

        def emergency():
            try:
                if self.src is not None:
                    self.src.abort()
                    self.src.disarm_delta()
            except Exception as exc:
                self.put(("log", f"Delta abort warning: {exc}"))
            try:
                if self.pt is not None:
                    self.pt.hold()
            except Exception as exc:
                self.put(("log", f"Magnet HOLD warning: {exc}"))

        threading.Thread(target=emergency, daemon=True).start()
        self.progress_var.set("Stopping...")

    def reset_instruments(self):
        if not self.device_ready:
            return
        # Snapshot the operator's GPIB choices on the UI thread; the worker
        # connects lazily (deferred-connect rule: never at startup).
        params = dict(
            elec_mode=self.electrical_mode_var.get().upper(),
            address_6221=self.visa_6221_var.get().strip(),
            address_2182=self.visa_2182_var.get().strip(),
            matrix_enabled=bool(self.matrix_enabled_var.get()),
            addr_matrix=self.visa_matrix_var.get().strip(),
            matrix_simulate=bool(self.matrix_simulate_var.get()),
        )
        threading.Thread(
            target=self._reset_worker, kwargs=params, daemon=True).start()

    def _reset_worker(self, elec_mode, address_6221, address_2182,
                      matrix_enabled=False, addr_matrix="",
                      matrix_simulate=False):
        try:
            self._ensure_gpib(
                elec_mode, address_6221, address_2182,
                matrix_enabled=matrix_enabled, addr_matrix=addr_matrix,
                matrix_simulate=matrix_simulate)
            self.put(("log", "Resetting instruments (*RST)..."))
            if self.src is not None:
                self.src.output_off()
                self.src.set_current(0.0)
                self.src.reset()
            if self.dmm is not None:
                self.dmm.reset()
            self.put(("log", "Instruments reset complete."))
        except Exception as exc:
            self.put(("log", f"Reset warning: {exc}"))

    def shutdown(self):
        self.stop_event.set()
        # Front-panel recovery BEFORE closing the 6221: a run killed at
        # shutdown must not leave the panel dark (*RST does not restore it).
        if getattr(self, "_frontpanel_disabled", False):
            try:
                if self.src is not None:
                    self.src.set_display_enabled(True)
            except Exception:
                pass
            self._frontpanel_disabled = False
        try:
            if self.src is not None:
                self.src.abort()
        except Exception:
            pass
        try:
            if self.pt is not None:
                self.pt.close()
                self.pt.disable_reconnect()
        except Exception:
            pass
        try:
            if self.dmm is not None:
                self.dmm.close()
        except Exception:
            pass


def main():
    app = MRMeasurementApp()
    try:
        app.mainloop()
    finally:
        app.shutdown()


if __name__ == "__main__":
    main()
