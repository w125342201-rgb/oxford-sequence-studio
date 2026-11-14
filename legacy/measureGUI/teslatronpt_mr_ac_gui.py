# -*- coding: utf-8 -*-
"""
TeslatronPT MR + AC lock-in (6221 sine + PXI-4462 digital lock-in)
====================================================================

Field-axis-only MR program: magnetic-field STEP/SWEEP scans with a lock-in
resistance readout. No 2182A / Delta / DC / matrix options by design (user
decision 2026-09-04). Depends on:

    oxford.oxford_teslatronpt_api   -> TeslatronPT (iTC-VTI / iTC-Probe / iPS)
    keithely6221_2182A.driver        -> Keithley6221 (AC excitation, marker)
    pxi4462.pxi4462_lockin           -> PXI-4462 digital lock-in readout

AC lock-in readout: 6221 outputs I_AC * sin(2*pi*f*t) for the whole run (its
phase marker, one 1 us pulse per cycle on trigger line 3, starts each 1 s
PXI-4462 acquisition on PFI 0); the 1 s window holds an integral number of
cycles for integer f, so a single-bin DFT (rectangular window) gives the
exact amplitude at f: R = V_peak / I_peak. See teslatronpt_mr_ac_gui_README.txt.

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
    pressure_mbar, field_setpoint_T, field_T, current_A, voltage_V(=V_peak),
    resistance_ohm(=R, lock-in), trace_type(=LOCKIN),
    raw_trace(=x,y,phase_deg), electrical_mode(=AC), branch
"""

from __future__ import annotations

import csv
import re
import math
import queue
from typing import Optional
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
    DEFAULT_COMPLIANCE_V,
    MAX_SOURCE_CURRENT_A,
)
from pxi4462.pxi4462_lockin import Pxi4462Lockin


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
DEFAULT_POLL_S = 1.0
DEFAULT_OUTPUT = "mr_ac_measurements.csv"


# ---- AC lock-in mode (6221 AC excitation + PXI-4462 digital lock-in) ----
# User decisions (2026-09-04): I_AC = GUI current, f = 13 Hz (integer Hz ->
# integral cycles in the 1 s FFT window -> no leakage; clear of 50 Hz
# harmonics and the 4462 AC-coupling high-pass ~3.4 Hz), 1 s per point,
# 6221 phase marker -> 4462 PFI 0 rising edge starts each acquisition.
DEFAULT_AC_FREQ_HZ = 13.0
DEFAULT_DAQ_FS_HZ = 51200.0
DEFAULT_DAQ_RANGE_V = 0.316   # smallest range: best noise (live-verified)
DEFAULT_DAQ_TERMINAL = "diff"   # 4-wire sample: use DIFF (verified 20x lower noise)
DEFAULT_DAQ_COUPLING = "dc"
DEFAULT_DAQ_SIM_NSV = 5e-6
DEFAULT_MARKER_LINE = 1           # 6221 trigger-connector line -> 4462 PFI0
DEFAULT_PHASE_REF_DEG = 0.0       # no rotation by default (phase varies between
                                  # 6221 power-ups); R = amplitude is unaffected.
                                  # Fill a measured value only if you need X/Y
                                  # de-rotated for a specific session.

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
    # extra simultaneous AI channels (PXI-4462 ai1..ai3), lock-in R each
    r_ai1_ohm: Optional[float] = None
    r_ai2_ohm: Optional[float] = None
    r_ai3_ohm: Optional[float] = None
    # second harmonic (2f) of every AI channel: V2f peak and V2f/V1f ratio
    v2_ai0_v: Optional[float] = None
    ratio2_ai0: Optional[float] = None
    v2_ai1_v: Optional[float] = None
    ratio2_ai1: Optional[float] = None
    v2_ai2_v: Optional[float] = None
    ratio2_ai2: Optional[float] = None
    v2_ai3_v: Optional[float] = None
    ratio2_ai3: Optional[float] = None


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
        self.geometry("860x540")

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
        self.daq = None                # PXI-4462 lock-in layer (AC mode)

        # --- queue / thread plumbing ---
        self.queue = queue.Queue()
        self._queue_after = None
        self._poll_after = None
        self.log_widget = None

        # --- UI vars ---
        self.status_var = tk.StringVar(value="Connecting...")
        self.progress_var = tk.StringVar(value="Idle")

        self.visa_6221_var = tk.StringVar(value="GPIB0::14::INSTR")
        self.visa_hint_var = tk.StringVar(
            value="AC 锁相：6221 sin（marker→线1）+ PXI-4462（PFI0 触发，"
                  "1s 整周期 FFT）。无 2182A/矩阵——见 AC lock-in 框。"
        )

        self.temperatures_var = tk.StringVar(value=DEFAULT_TEMPERATURES)
        self.current_var = tk.StringVar(value=f"{DEFAULT_CURRENT_A:.3g}")

        self.field_ramp_var = tk.StringVar(value="")  # blank -> default 0.237
        self.b_tol_var = tk.StringVar(value=f"{DEFAULT_B_TOL_T:g}")
        self.b_stable_var = tk.StringVar(value=f"{DEFAULT_B_STABLE_S:g}")
        self.b_settle_var = tk.StringVar(value=f"{DEFAULT_B_SETTLE_S:g}")
        self.seg_dwell_var = tk.StringVar(value=f"{DEFAULT_SEG_DWELL_S:g}")
        self.t_tol_var = tk.StringVar(value=f"{DEFAULT_T_TOL_K:g}")
        self.t_stable_var = tk.StringVar(value=f"{DEFAULT_T_STABLE_S:g}")

        self.field_mode_var = tk.StringVar(value="STEP")
        # AC lock-in (6221 + PXI-4462) parameters.
        self.ac_freq_var = tk.StringVar(value=f"{DEFAULT_AC_FREQ_HZ:g}")
        self.daq_simulate_var = tk.BooleanVar(value=False)
        # fixed by design: all 4 AI channels, diff, DC coupling
        self.daq_range_var = tk.StringVar(value="0.316")
        self.daq_trigger_var = tk.StringVar(value="marker")
        self.daq_phase_ref_var = tk.StringVar(value=f"{DEFAULT_PHASE_REF_DEG:g}")
        self.auto_pressure_var = tk.BooleanVar(value=True)
        self.output_var = tk.StringVar(value=str(self.output_path))
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
        ttk.Label(head, text="TeslatronPT MR + AC lock-in (6221 sin + PXI-4462)",
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

        # balanced two-column layout: left column = field/T params,
        # right column starts at col 3; stability trio lives in a vertical
        # mini-panel hugging the right edge (col 7).
        row_label(0, 0, "Temps (K)")
        ttk.Entry(setup, textvariable=self.temperatures_var, width=20).grid(
            row=0, column=1, sticky="w", padx=2)
        row_label(0, 3, "I_AC (A) pk")
        ttk.Entry(setup, textvariable=self.current_var, width=10).grid(
            row=0, column=4, sticky="w", padx=2)

        row_label(1, 0, "Field ramp")
        ttk.Entry(setup, textvariable=self.field_ramp_var, width=8).grid(
            row=1, column=1, sticky="w", padx=2)
        ttk.Label(setup, text="T/min (blank=0.237)").grid(
            row=1, column=2, sticky="w", padx=2)
        row_label(1, 3, "B tol")
        ttk.Entry(setup, textvariable=self.b_tol_var, width=8).grid(
            row=1, column=4, sticky="w", padx=2)

        row_label(2, 0, "T tol")
        ttk.Entry(setup, textvariable=self.t_tol_var, width=8).grid(
            row=2, column=1, sticky="w", padx=2)
        row_label(2, 3, "T stable")
        ttk.Entry(setup, textvariable=self.t_stable_var, width=6).grid(
            row=2, column=4, sticky="w", padx=2)

        # stability trio: vertical mini-panel at the right edge
        vpan = ttk.Frame(setup)
        vpan.grid(row=0, column=6, rowspan=3, sticky="nsew", padx=(4, 2))
        for r, (lbl, var) in enumerate((
                ("B stb", self.b_stable_var),
                ("B stl", self.b_settle_var),
                ("Sg dwl", self.seg_dwell_var))):
            ttk.Label(vpan, text=lbl).grid(row=r, column=0, sticky="w")
            ttk.Entry(vpan, textvariable=var, width=6).grid(
                row=r, column=1, padx=2)
        ttk.Checkbutton(setup, text="Auto pres",
                        variable=self.auto_pressure_var).grid(
            row=3, column=6, columnspan=2, sticky="w", padx=2)

        row_label(3, 0, "Field mode")
        self.field_mode_combo = ttk.Combobox(
            setup, textvariable=self.field_mode_var,
            values=["STEP", "SWEEP"], width=8, state="readonly")
        self.field_mode_combo.grid(row=3, column=1, padx=2)
        ttk.Label(setup, text="Elec: AC lock-in (固定)",
                  foreground="#666666").grid(
            row=3, column=3, sticky="w", padx=(12, 2))

        # 6221 GPIB address (operator picks from the dropdown).
        row_label(4, 0, "6221")
        self.addr_6221 = ttk.Combobox(setup, textvariable=self.visa_6221_var,
                                       values=[], width=14)
        self.addr_6221.grid(row=4, column=1, padx=2)
        ttk.Label(setup, textvariable=self.visa_hint_var,
                  font=("Segoe UI", 8), foreground="#888888").grid(
            row=4, column=3, columnspan=6, sticky="w", padx=4)

        # Segmented field scan (3 x N)
        seg = ttk.LabelFrame(outer, text="Field sweep (3 x N): row1=starts, "
                                          "row2=stops, row3=steps (T)", padding=3)
        seg.pack(fill="x", pady=3)
        self.segments_text = tk.Text(seg, height=3, width=48,
                                     font=("Consolas", 10))
        self.segments_text.grid(row=0, column=0, padx=2, pady=2)
        self.segments_text.insert("1.0", self.segments_var.get())
        ttk.Button(seg, text="Refresh",
                   command=self.refresh_visa_resources).grid(
            row=0, column=1, padx=4, sticky="n")

        # AC lock-in (6221 AC + PXI-4462) parameters - Elec mode "AC".
        acf = ttk.LabelFrame(
            outer, text="AC lock-in (6221 sin + PXI-4462 digital lock-in)",
            padding=3)
        acf.pack(fill="x", pady=3)
        ttk.Label(acf, text="f (Hz)").grid(row=0, column=0, padx=(4, 2))
        ttk.Entry(acf, textvariable=self.ac_freq_var, width=6).grid(
            row=0, column=1, padx=2)
        ttk.Label(acf, text="phase ref(deg)").grid(
            row=0, column=2, padx=(10, 2))
        ttk.Entry(acf, textvariable=self.daq_phase_ref_var, width=7).grid(
            row=0, column=3, padx=2)
        ttk.Label(acf, text="range").grid(row=0, column=4, padx=(10, 2))
        ttk.Combobox(acf, textvariable=self.daq_range_var, width=7,
                     values=["0.316", "1", "3.16", "10"],
                     state="readonly").grid(row=0, column=5, padx=2)
        ttk.Label(acf, text="trig").grid(row=0, column=6, padx=(10, 2))
        ttk.Combobox(acf, textvariable=self.daq_trigger_var, width=9,
                     values=["marker", "software"], state="readonly").grid(
            row=0, column=7, padx=2)
        ttk.Checkbutton(
            acf, text="DAQ simulate", variable=self.daq_simulate_var).grid(
            row=0, column=8, padx=(12, 2))
        ttk.Label(acf,
                  text="4 路同采 ai0..ai3（diff + DC 耦合固定）；I_AC = 上方 "
                       "I_AC (A)。phase ref 默认 0：需要 X/Y 去旋转时按"
                       "会话实测填。",
                  font=("Segoe UI", 8), foreground="#888888").grid(
            row=1, column=0, columnspan=9, sticky="w", padx=4, pady=(1, 0))

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
        bus's real address (6221=GPIB0::14); pick another entry from a
        dropdown if the bus layout changes. No instrument is
        opened here - connection happens only when Connect is pressed.
        """
        try:
            rm = pyvisa.ResourceManager()
            resources = list(rm.list_resources())
            rm.close()
            self.visa_resources = resources
            self.addr_6221["values"] = resources
            self.put(("log", "VISA resources (enumerated only): "
                              f"{resources if resources else 'none found'}"))
        except Exception as exc:
            self.put(("log", f"VISA scan failed: {exc}"))

    def _spawn_connect(self):
        # Startup auto-connect: ONLY the Oxford Mercury controllers, so the
        # live T/B/p readings refresh right away. The GPIB electrical
        # instruments (6221) are connected lazily when Start (or
        # Reset instruments) is pressed, using the addresses the operator
        # picked in the dropdowns.
        if self._connecting:
            return
        self._connecting = True
        threading.Thread(
            target=self._connect_worker,
            kwargs=dict(),
            daemon=True,
        ).start()

    def _connect_worker(self):
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
            self.put(("status", "Mercury connected (AC lock-in)"))
            for k, v in ids.items():
                self.put(("log", f"{k}: {v}"))
            self.put(("log",
                      "GPIB instruments (6221) connect when Start is "
                      "pressed - pick their addresses in the dropdowns."))
        except Exception as exc:
            self.put(("error", f"Mercury connection failed: {exc}"))
        finally:
            self._connecting = False

    def _ensure_gpib(self, address_6221):
        """Connect the 6221 (GPIB) if not already up.

        This program uses only the 6221 (AC excitation) plus the PXI-4462
        lock-in layer - no 2182A GPIB session, no matrix switch. Called at
        the start of a measurement and by Reset instruments - never at
        startup. Raises on failure so the caller aborts with a clear message.
        """
        if self.gpib_ready and self.src is not None:
            return
        # Clean up any half-open handles from an earlier failed attempt.
        for inst in (self.src, self.dmm):
            if inst is not None:
                try:
                    inst.close()
                except Exception:
                    pass
        self.src = None
        self.dmm = None
        if self.rm is None:
            self.rm = pyvisa.ResourceManager()
        if not address_6221:
            raise ValueError("6221 VISA/GPIB address is empty.")
        self.src = Keithley6221(address_6221, self.rm)
        self.put(("log", f"6221: {self.src.idn()}"))
        self.dmm = None
        self.gpib_ready = True

    def _close_instruments(self):
        for inst, name in ((self.src, "6221"), (self.dmm, "2182A"),
                           (self.pt, "TeslatronPT")):
            try:
                if inst is not None:
                    inst.close()
            except Exception:
                pass
        self.src = None
        self.dmm = None
        self.pt = None
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



    def _measure_ac(self):
        """One PXI-4462 multi-channel digital lock-in reading.

        Every configured AI channel is sampled in the same marker-triggered
        acquisition and reduced independently. The FIRST channel (ai0) is the
        main sample and its R/V go into the standard CSV columns; the other
        channels' lock-in resistances ride along as ``others``.

        Returns (current, v0, r0, "LOCKIN", "x,y,phase_deg", others).
        One retry on acquisition failure.
        """
        if getattr(self, "daq", None) is None:
            raise RuntimeError("AC mode requires the lock-in layer; create it "
                               "in the measurement worker.")
        # Fixed phase reference (GUI "phase ref(deg)", default -19 deg =
        # measured trigger delay): X carries the amplitude, Y ~ 0. A fixed
        # GUI value works even when the sample starts in the zero-resistance
        # state (no signal to auto-calibrate from).
        phase_ref = float(self._run.get("daq_phase_ref", 0.0))
        last_exc = None
        for attempt in range(2):
            try:
                results = self.daq.acquire_one(phase_ref_deg=phase_ref)
                names = list(results.keys())
                r0 = results[names[0]]
                raw = (f"{r0.x_v:.9g},{r0.y_v:.9g},"
                       f"{math.degrees(r0.phase_rad):.6g}")
                others = {"v2_ai0_v": r0.v2_peak_v,
                          "ratio2_ai0": r0.ratio2}
                for i, name in enumerate(names[1:], start=1):
                    rr = results[name]
                    others[f"r_ai{i}_ohm"] = rr.resistance_ohm
                    others[f"v2_ai{i}_v"] = rr.v2_peak_v
                    others[f"ratio2_ai{i}"] = rr.ratio2
                return (self._run["current"], r0.v_peak_v,
                        r0.resistance_ohm, "LOCKIN", raw, others)
            except Exception as exc:
                last_exc = exc
                self.put(("log",
                          f"  lock-in attempt {attempt + 1} failed: {exc}; "
                          f"retrying..."))
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
        # extra PXI-4462 AI channels (simultaneous lock-in, ai0 is above)
        "R_ai1_ohm", "R_ai2_ohm", "R_ai3_ohm",
        # second harmonic 2f of every channel (V2f peak, V2f/V1f ratio)
        "V2_ai0_V", "ratio2_ai0",
        "V2_ai1_V", "ratio2_ai1",
        "V2_ai2_V", "ratio2_ai2",
        "V2_ai3_V", "ratio2_ai3",
    ]

    def _prepare_output(self):
        if not hasattr(self, "_csv_lock"):
            self._csv_lock = threading.Lock()
        path = Path(self.output_var.get()).expanduser()
        if path.suffix.lower() != ".csv":
            path = path.with_suffix(".csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.exists() and path.stat().st_size > 0

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
            "" if point.r_ai1_ohm is None else f"{point.r_ai1_ohm:.12g}",
            "" if point.r_ai2_ohm is None else f"{point.r_ai2_ohm:.12g}",
            "" if point.r_ai3_ohm is None else f"{point.r_ai3_ohm:.12g}",
            "" if point.v2_ai0_v is None else f"{point.v2_ai0_v:.12g}",
            "" if point.ratio2_ai0 is None else f"{point.ratio2_ai0:.12g}",
            "" if point.v2_ai1_v is None else f"{point.v2_ai1_v:.12g}",
            "" if point.ratio2_ai1 is None else f"{point.ratio2_ai1:.12g}",
            "" if point.v2_ai2_v is None else f"{point.v2_ai2_v:.12g}",
            "" if point.ratio2_ai2 is None else f"{point.ratio2_ai2:.12g}",
            "" if point.v2_ai3_v is None else f"{point.v2_ai3_v:.12g}",
            "" if point.ratio2_ai3 is None else f"{point.ratio2_ai3:.12g}",
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

            ac_freq = self._float(self.ac_freq_var.get(), "AC frequency")
            if ac_freq <= 0 or ac_freq >= DEFAULT_DAQ_FS_HZ / 2:
                raise ValueError(
                    f"AC frequency must be in (0, "
                    f"{DEFAULT_DAQ_FS_HZ / 2:g}) Hz.")
            daq_range = self.daq_range_var.get().strip() or "0.316"
            if str(daq_range) not in ("0.316", "1", "3.16", "10"):
                raise ValueError(
                    "DAQ range must be one of 0.316, 1, 3.16 or 10 V.")

            self._run = {
                "mode": self.field_mode_var.get().upper(),
                "elec_mode": "AC",
                "current": current,
                "ac_freq": ac_freq,
                "daq_simulate": bool(self.daq_simulate_var.get()),
                "daq_range": daq_range,
                "daq_trigger": self.daq_trigger_var.get().strip() or "marker",
                "daq_phase_ref": self._float(self.daq_phase_ref_var.get(),
                                             "Phase ref"),
                "marker_line": DEFAULT_MARKER_LINE,
                "auto_pressure": bool(self.auto_pressure_var.get()),
                "b_tol": b_tol,
                "b_stable": b_stable,
                "b_settle": b_settle,
                "seg_dwell": seg_dwell,
                "t_tol": t_tol,
                "t_stable": t_stable,
                "ramp_rate": ramp_rate,
                # 6221 GPIB address captured on the UI thread (worker
                # connects lazily at Start - never touches Tk itself).
                "addr_6221": self.visa_6221_var.get().strip(),
            }
            self._csv_lock = threading.Lock()
            self._prepare_output()

        except Exception as exc:
            messagebox.showerror("Input error", str(exc))
            return

        total = len(temperatures) * sum(len(b.points) for b in branches)
        self.put(("log",
                  f"Run start: {len(temperatures)} T x {total} B points "
                  f"({self._run['mode']}/AC lock-in), "
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
        mode = self._run["mode"]          # STEP or SWEEP (field axis)

        # Wait for any in-flight poll to finish before touching instruments.
        self._wait_poll_idle()

        try:
            # Lazy GPIB connect: only the 6221 (AC excitation). The PXI-4462
            # lock-in layer is created below (real DAQ or simulate).
            self._ensure_gpib(
                self._run.get("addr_6221", ""),
            )
            # Source prep (no auto-reset; operator resets explicitly).
            try:
                self.src.set_compliance(DEFAULT_COMPLIANCE_V)
            except Exception as exc:
                self.put(("log", f"Compliance warning: {exc}"))
            self.src.output_off()
            self.src.set_current(0.0)

            # PXI-4462 digital lock-in layer (real DAQ or simulate).
            self.daq = Pxi4462Lockin(
                freq_hz=self._run["ac_freq"],
                i_peak_a=self._run["current"],
                fs_hz=DEFAULT_DAQ_FS_HZ,
                duration_s=1.0,
                channels=[0, 1, 2, 3],       # always all four, simultaneous
                terminal="diff",             # fixed: 4-wire differential
                coupling="dc",               # fixed: flat response at 13 Hz
                voltage_range_v=self._run.get("daq_range", "0.316"),
                trigger_terminal=(None if str(self._run.get(
                    "daq_trigger", "marker")).lower() == "software"
                    else "PFI0"),
                simulate=bool(self._run.get("daq_simulate", False)),
                noise_sim_v=DEFAULT_DAQ_SIM_NSV,
                log=lambda m: self.put(("log", m)))
            # 6221 AC excitation on for the WHOLE run (no DC bias; the
            # wave keeps running while the magnet steps).
            try:
                self.src.output_on()
                self.src.set_current(0.0)
                self.src.configure_ac(
                    freq=self._run["ac_freq"],
                    amplitude=self._run["current"],
                    offset=0.0, func="SIN", phase_marker=True,
                    marker_position=0,
                    marker_line=self._run.get("marker_line", 3))
                self.src.arm_ac()
                self.src.init_ac()
                self.put(("log",
                          f"AC: 6221 sine ON (I_AC={self._run['current']:g} A "
                          f"pk, f={self._run['ac_freq']:g} Hz, marker line "
                          f"{self._run.get('marker_line', 3)} -> 4462 PFI0)."))
            except Exception as exc:
                raise RuntimeError(
                    f"AC excitation start failed: {exc}") from exc

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
                            b, T, "AC", completed, total, ramp_rate)
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
                            completed = self._record(
                                bpt, T, b.index, "AC", completed, total)

            self.put(("progress",
                      "Measurement finished." if not self.stop_event.is_set()
                      else "Measurement stopped by user."))
            self.put(("log",
                      f"Done. {completed}/{total} points recorded to "
                      f"{self.output_path.name}"))

        except Exception as exc:
            self.put(("error", f"Measurement failed: {exc}"))
        finally:
            try:
                if self.src is not None:
                    self.src.write("SOUR:WAVE:ABOR")
            except Exception:
                pass
            try:
                if self.src is not None:
                    self.src.output_off()
            except Exception:
                pass
            self.measurement_running = False
            self.start_btn.state(["!disabled"])

    def _record(self, bpt, T, branch_idx, elec_mode, completed, total):
        # elec_mode is always "AC" in this program (lock-in readout).
        env = self._env_snapshot()
        (current, voltage, resistance, trace_type, raw,
         chan_r) = self._measure_ac()
        point = MRPoint(
            r_ai1_ohm=chan_r.get("r_ai1_ohm"),
            r_ai2_ohm=chan_r.get("r_ai2_ohm"),
            r_ai3_ohm=chan_r.get("r_ai3_ohm"),
            v2_ai0_v=chan_r.get("v2_ai0_v"),
            ratio2_ai0=chan_r.get("ratio2_ai0"),
            v2_ai1_v=chan_r.get("v2_ai1_v"),
            ratio2_ai1=chan_r.get("ratio2_ai1"),
            v2_ai2_v=chan_r.get("v2_ai2_v"),
            ratio2_ai2=chan_r.get("ratio2_ai2"),
            v2_ai3_v=chan_r.get("v2_ai3_v"),
            ratio2_ai3=chan_r.get("ratio2_ai3"),
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
                    self.src.write("SOUR:WAVE:ABOR")
            except Exception as exc:
                self.put(("log", f"AC abort warning: {exc}"))
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
            address_6221=self.visa_6221_var.get().strip(),
        )
        threading.Thread(
            target=self._reset_worker, kwargs=params, daemon=True).start()

    def _reset_worker(self, address_6221):
        try:
            self._ensure_gpib(address_6221)
            self.put(("log", "Resetting instruments (*RST)..."))
            if self.src is not None:
                self.src.output_off()
                self.src.set_current(0.0)
                self.src.reset()
            self.put(("log", "Instruments reset complete."))
        except Exception as exc:
            self.put(("log", f"Reset warning: {exc}"))

    def shutdown(self):
        self.stop_event.set()
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
