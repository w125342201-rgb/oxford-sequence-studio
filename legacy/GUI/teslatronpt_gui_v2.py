# -*- coding: utf-8 -*-
"""TeslatronPT GUI for Oxford Instruments Mercury iTC/iPS.

Machine topology (two independent iTCs + one iPS, per lab hardware):
    iTC-VTI   TCPIP0::192.168.1.113::7020::SOCKET
        VTI bath temperature (background)   : DB6.T1
        Exchange-gas pressure               : DB3.P1
    iTC-Probe TCPIP0::192.168.1.114::7020::SOCKET
        Probe (sample) temperature          : DB8.T1
        Probe fine temperature-control loop : TSET on probe channel
    iPS       TCPIP0::192.168.1.115::7020::SOCKET
        Magnet group                        : GRPZ

Physics (lab): the sample rod sits inside the VTI. The VTI provides the
background temperature and the exchange-gas pressure; the probe controller
(114) fine-controls the sample temperature. The *sample* temperature is
therefore the PROBE thermometer (DB8.T1). The VTI-Probe thermal coupling is
set by the exchange-gas pressure (VTI-side, 113).

Module numbers (DB6 / DB8 / DB3) were verified against the controllers
on the industrial PC - no need to re-confirm.

Linked temperature control (VTI + probe always work together):
    User sets the desired SAMPLE (= probe) temperature T:
        probe TSET = T          on iTC-Probe (114)
        VTI   TSET = T - offset on iTC-VTI  (113)
        offset = 1.0 K if T >= 10 K
        offset = 0.2 K if T <  10 K
    When the sample (probe) crosses 10 K, the offsets/pressure are re-applied
    automatically (pressure switch with hysteresis to avoid chatter).

Controls:
    Temperature -> SET:DEV:DB8.T1:TEMP:LOOP:TSET:<K>          on iTC-Probe (114)
                  + SET:DEV:DB6.T1:TEMP:LOOP:TSET:<T-offset>  on iTC-VTI  (113)
    Pressure    -> SET:DEV:DB3.P1:TEMP:LOOP:TSET:<mbar>       on iTC-VTI  (113)  (needle-valve loop is the TEMP loop; PRES is read-only)
    Field       -> SET:DEV:GRPZ:PSU:SIG:FSET:<T> then RTOS    on iPS      (115)
    Temp ramp (iTC loop-specific, remote-only):
        SET:DEV:DB8.T1:TEMP:LOOP:RSET:<K/min> + RENA:ON|OFF   on iTC-Probe (114)
        SET:DEV:DB6.T1:TEMP:LOOP:RSET:<K/min> + RENA:ON|OFF   on iTC-VTI  (113)
        with RENA=ON any TSET change ramps at RSET (K/min) instead of stepping.
        GUI uses ONE input and broadcasts the same RSET/RENA to both loops
        so the linked offset stays constant.

Physical notes (lab operation):
    * The actual *sample* temperature is the PROBE thermometer (DB8.T1). The VTI
      (DB6.T1) is the cold bath; the VTI provides the background temperature and
      the probe controller fine-controls the sample (two controllers linked).
    * Linked-control offsets / empirical rule at the 10 K boundary:
        T_sample < 10 K : VTI = probe - 0.2 K, 4  mbar
        T_sample >= 10 K: VTI = probe - 1.0 K, 10 mbar
      The exchange-gas pressure sets the VTI-Probe thermal coupling, so
      auto-pressure switches the VTI pressure when the *sample (probe)*
      temperature crosses 10 K (with hysteresis to avoid chatter).
    * Magnet ramp rate must never exceed 0.237 T/min (= approx. 39.5 Oe/s).
"""

import queue
import re
import socket
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

ITC_VTI_HOST = "192.168.1.113"    # VTI bath temperature + exchange-gas pressure
ITC_PROBE_HOST = "192.168.1.114"  # probe (sample) fine temperature control
IPS_HOST = "192.168.1.115"        # magnet
PORT = 7020
# Module numbers verified against the controllers on the industrial PC.
VTI_MODULE = "DB6"        # on iTC-VTI (113)
PROBE_MODULE = "DB8"      # on iTC-Probe (114)
PRESSURE_MODULE = "DB3"   # on iTC-VTI (113)
MAGNET_GROUP = "GRPZ"

# Magnet ramp-rate hard ceiling: 0.237 T/min (= approx. 39.5 Oe/s).  Never exceed this.
MAX_FIELD_RAMP_T_PER_MIN = 0.237
# Software ceiling for the field SETPOINT (T). The installed magnet is rated
# 14 T (user-confirmed 2026-08-30).
MAX_FIELD_T = 14.0
# Fallback ramp rate when the magnet's residual RFST is unreadable or zero:
# a stale zero RFST is the known root cause of "FSET accepted but the field
# never ramps" - never reuse it blindly.
DEFAULT_FIELD_RAMP_T_PER_MIN = 0.237

# Temperature-control / exchange-gas rule.
SAMPLE_T_SWITCH_K = 10.0          # 10 K boundary
PRESSURE_BELOW_10K = 4.0         # < 10 K : 4 mbar  (VTI-Probe diff ≈ 0.2 K)
PRESSURE_ABOVE_10K = 10.0        # >=10 K : 10 mbar (VTI-Probe diff ≈ 1.0 K)
# Expected steady-state VTI-Probe difference == the linked-control offset:
# VTI setpoint always sits this far BELOW the probe setpoint.
EXPECTED_DIFF_BELOW = 0.2
EXPECTED_DIFF_ABOVE = 1.0
# Hysteresis so auto-pressure does not chatter while the sample hovers at 10 K.
AUTO_PRESSURE_HYST = 0.5

# Temperature ramp (Mercury iTC loop-specific; RENA = ramp enable, RSET = rate).
DEFAULT_TEMP_RAMP_K_PER_MIN = 5.0   # sensible default (matches common driver defaults)
MAX_TEMP_RAMP_K_PER_MIN = 100.0     # GUI sanity cap


def vti_offset(t: float) -> float:
    """Linked-control offset: VTI target = probe target - vti_offset(t)."""
    return EXPECTED_DIFF_BELOW if t < SAMPLE_T_SWITCH_K else EXPECTED_DIFF_ABOVE

# Matches a numeric value optionally followed by a unit suffix (K, mK, A, T, mbar, ...)
_VALUE_RE = re.compile(r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)")


class MercuryError(RuntimeError):
    pass


class MercuryController:
    def __init__(self, host: str, port: int = 7020, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None
        self.lock = threading.Lock()
        # Receive buffer: bytes after the first LF are kept for the next read
        # (synced with oxford/oxford_teslatronpt_api.py 2026-08-30).
        self._rxbuf = bytearray()
        # Auto-reconnect on communication failure. Disabled during orderly
        # shutdown so an in-flight query cannot resurrect a closed connection
        # (synced with oxford/oxford_teslatronpt_api.py 2026-08-30).
        self._reconnect_enabled = True
        self.connect()

    def connect(self):
        self.close()
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)

    def set_reconnect(self, enabled: bool):
        self._reconnect_enabled = bool(enabled)

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self._rxbuf = bytearray()

    def _readline(self) -> str:
        while b"\n" not in self._rxbuf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise MercuryError(f"{self.host}:{self.port} closed the connection")
            self._rxbuf.extend(chunk)
        line, _, rest = self._rxbuf.partition(b"\n")
        self._rxbuf = bytearray(rest)
        return line.decode("ascii", errors="replace").strip()

    def query(self, cmd: str) -> str:
        with self.lock:
            if self.sock is None:
                if not self._reconnect_enabled:
                    raise MercuryError(
                        f"{self.host}:{self.port} reconnect is disabled "
                        f"(shutdown in progress)")
                self.connect()
            try:
                self.sock.sendall((cmd + "\n").encode("ascii"))
                return self._readline()
            except (OSError, socket.timeout):
                if not self._reconnect_enabled:
                    self.close()
                    raise
                # One reconnect-and-retry; on a second failure, close and re-raise.
                try:
                    self.connect()
                    self.sock.sendall((cmd + "\n").encode("ascii"))
                    return self._readline()
                except (OSError, socket.timeout):
                    self.close()
                    raise

    @staticmethod
    def _check_valid(resp: str):
        """Raise MercuryError if a SET/action reply indicates INVALID."""
        resp = resp.strip()
        if resp.endswith(":INVALID") or ":INVALID:" in resp or resp == "INVALID":
            raise MercuryError(f"Mercury returned INVALID: {resp}")
        return resp

    @staticmethod
    def parse_value(resp: str) -> float:
        if "INVALID" in resp:
            raise MercuryError(resp)
        if ":INVALID" in resp or resp.strip().endswith("INVALID"):
            raise MercuryError(resp)
        resp = resp.strip()
        parts = resp.split(":")
        token = parts[parts.index("VALID") - 1] if "VALID" in parts else parts[-1]
        m = _VALUE_RE.search(token)
        if not m:
            raise MercuryError(f"Cannot parse Mercury response: {resp}")
        return float(m.group(1))

    def read_signal(self, module, chan, block, field) -> float:
        return self.parse_value(
            self.query(f"READ:DEV:{module}.{chan}:{block}:SIG:{field}")
        )

    def read_group_signal(self, group, block, field) -> float:
        return self.parse_value(
            self.query(f"READ:DEV:{group}:{block}:SIG:{field}")
        )

    def set_signal(self, module, chan, block, sub, field, value) -> str:
        return self._check_valid(self.query(
            f"SET:DEV:{module}.{chan}:{block}:{sub}:{field}:{value}"
        ))

    def set_group_signal(self, group, block, sub, field, value) -> str:
        return self._check_valid(self.query(
            f"SET:DEV:{group}:{block}:{sub}:{field}:{value}"
        ))

    def set_action(self, group, action) -> str:
        return self._check_valid(self.query(f"SET:DEV:{group}:PSU:ACTN:{action}"))

    def idn(self) -> str:
        return self.query("*IDN?")


class Teslatron:
    """Three independent instruments:

      iTC-VTI   (192.168.1.113): VTI bath temperature + exchange-gas pressure
      iTC-Probe (192.168.1.114): probe/sample temperature + its control loop
      iPS       (192.168.1.115): magnet (GRPZ)
    """
    def __init__(self):
        # Connect VTI controller first; on any later failure, close the
        # sockets already opened so nothing leaks.
        self.itc_vti = MercuryController(ITC_VTI_HOST, PORT)
        try:
            self.itc_probe = MercuryController(ITC_PROBE_HOST, PORT)
        except Exception:
            self.itc_vti.close()
            raise
        try:
            self.ips = MercuryController(IPS_HOST, PORT)
        except Exception:
            self.itc_vti.close()
            self.itc_probe.close()
            raise

    def close(self):
        self.itc_vti.close()
        self.itc_probe.close()
        self.ips.close()

    def disable_reconnect(self):
        """Prevent every controller from auto-reconnecting (shutdown path)."""
        for ctl in (self.itc_vti, self.itc_probe, self.ips):
            ctl.set_reconnect(False)

    def idn_vti(self):
        return self.itc_vti.idn()

    def idn_probe(self):
        return self.itc_probe.idn()

    def idn_ips(self):
        return self.ips.idn()

    def vti_temp(self):
        return self.itc_vti.read_signal(VTI_MODULE, "T1", "TEMP", "TEMP")

    def probe_temp(self):
        return self.itc_probe.read_signal(PROBE_MODULE, "T1", "TEMP", "TEMP")

    def pressure(self):
        return self.itc_vti.read_signal(PRESSURE_MODULE, "P1", "PRES", "PRES")

    def field(self):
        return self.ips.read_group_signal(MAGNET_GROUP, "PSU", "FLD")

    def target_field(self):
        return self.ips.read_group_signal(MAGNET_GROUP, "PSU", "FSET")

    def set_temperature(self, kelvin: float):
        # Probe fine temperature-control loop on iTC-Probe (114) only.
        return self.itc_probe.set_signal(
            PROBE_MODULE, "T1", "TEMP", "LOOP", "TSET", f"{kelvin:.8g}"
        )

    def set_vti_temperature(self, kelvin: float):
        # VTI background temperature-control loop on iTC-VTI (113).
        return self.itc_vti.set_signal(
            VTI_MODULE, "T1", "TEMP", "LOOP", "TSET", f"{kelvin:.8g}"
        )

    def set_sample_temperature(self, kelvin: float):
        """Linked control: VTI provides the background, probe fine-controls.

        probe TSET = T           on iTC-Probe (114)
        VTI   TSET = T - offset  on iTC-VTI  (113)
        offset = 1.0 K (T >= 10 K) / 0.2 K (T < 10 K).

        Both setpoints are validated BEFORE any hardware write. If the VTI
        write fails after the probe write succeeded, the raised error says
        so explicitly (PARTIAL) — the two controllers cannot be updated
        atomically, and a silent half-applied linkage is the worst outcome.
        """
        offset = vti_offset(kelvin)
        vti_target = kelvin - offset
        if not (0 < kelvin <= 300) or not (0 < vti_target <= 300):
            raise MercuryError(
                f"Invalid linked setpoints: probe {kelvin:g} K / "
                f"VTI {vti_target:g} K (must be 0..300 K)."
            )
        probe_reply = self.itc_probe.set_signal(
            PROBE_MODULE, "T1", "TEMP", "LOOP", "TSET", f"{kelvin:.8g}")
        try:
            vti_reply = self.itc_vti.set_signal(
                VTI_MODULE, "T1", "TEMP", "LOOP", "TSET", f"{vti_target:.8g}")
        except Exception as exc:
            raise MercuryError(
                f"PARTIAL FAILURE: probe TSET {kelvin:g} K applied, "
                f"but VTI TSET {vti_target:g} K failed: {exc}"
            ) from exc
        return [probe_reply, vti_reply]

    def set_pressure(self, mbar: float):
        # VTI exchange-gas pressure on iTC-VTI (113).
        # The needle-valve control loop is the TEMP loop on DB3.P1; PRES is
        # only the measured read-back, so the setpoint goes to TEMP:LOOP:TSET.
        return self.itc_vti.set_signal(
            PRESSURE_MODULE, "P1", "TEMP", "LOOP", "TSET", f"{mbar:.8g}"
        )

    # --- Temperature ramp (loop-specific; RENA/RSET) ---
    def set_probe_ramp_rate(self, k_per_min: float):
        # RSET = temperature ramp rate in K/min on iTC-Probe (114).
        return self.itc_probe.set_signal(
            PROBE_MODULE, "T1", "TEMP", "LOOP", "RSET", f"{k_per_min:.6g}"
        )

    def set_vti_ramp_rate(self, k_per_min: float):
        # RSET on iTC-VTI (113).
        return self.itc_vti.set_signal(
            VTI_MODULE, "T1", "TEMP", "LOOP", "RSET", f"{k_per_min:.6g}"
        )

    def set_probe_ramp_enable(self, on: bool):
        # RENA = ramp enable (loop-specific, remote-only) on iTC-Probe (114).
        return self.itc_probe.set_signal(
            PROBE_MODULE, "T1", "TEMP", "LOOP", "RENA", "ON" if on else "OFF"
        )

    def set_vti_ramp_enable(self, on: bool):
        # RENA on iTC-VTI (113).
        return self.itc_vti.set_signal(
            VTI_MODULE, "T1", "TEMP", "LOOP", "RENA", "ON" if on else "OFF"
        )

    def set_current_ramp_rate(self, a_per_min: float):
        # RCST = current ramp rate in A/min.
        return self.ips.set_group_signal(
            MAGNET_GROUP, "PSU", "SIG", "RCST", f"{a_per_min:.10g}"
        )

    def set_field_ramp_rate(self, t_per_min: float):
        # RFST = field ramp rate in T/min (must be <= 0.237 T/min == approx. 39.5 Oe/s).
        return self.ips.set_group_signal(
            MAGNET_GROUP, "PSU", "SIG", "RFST", f"{t_per_min:.10g}"
        )

    def read_current_ramp_rate(self) -> float:
        return self.ips.read_group_signal(MAGNET_GROUP, "PSU", "RCST")

    def read_field_ramp_rate(self) -> float:
        return self.ips.read_group_signal(MAGNET_GROUP, "PSU", "RFST")

    def action(self) -> str:
        # ACTN is a string status (HOLD/RTOS/RTOZ/CLMP), not a float signal,
        # so parse_value cannot be used here. The REAL iPS replies WITHOUT a
        # trailing VALID (verified live: "STAT:DEV:GRPZ:PSU:ACTN:HOLD")
        # while simulators/other firmware append ":VALID" - accept both
        # (same fallback as the shared API's parse_status).
        resp = self.ips.query("READ:DEV:GRPZ:PSU:ACTN")
        resp = resp.strip()
        if "INVALID" in resp.upper():
            raise MercuryError(resp)
        parts = resp.split(":")
        if "VALID" in parts:
            idx = parts.index("VALID")
            if idx > 0:
                return parts[idx - 1].strip()
        if parts:
            token = parts[-1].strip()
            if token:
                return token
        raise MercuryError(f"Cannot parse ACTN response: {resp}")

    def set_field(self, tesla: float, ramp_rate_t_per_min: float | None = None):
        """Verified field command (Plan A).

        Official basis: handbook ch.7 "the control program must assume that
        data can be occasionally corrupted" + ch.11 SIG entries annotated
        "check before set".

        0. HOLD pre-check: the RFST/FSET/RTOS sequence is only defined from
           HOLD. Wait for a previous ramp's auto-return to HOLD (handbook
           sec.5) and unclamp a CLMPed output first (official: HOLD releases
           the clamp, crow-bar relays physically open).
        1. RFST: explicit non-zero rate every time. When the rate argument is
           None the magnet's residual RFST is read and validated; an unusable
           (zero/unreadable) value falls back to DEFAULT_FIELD_RAMP_T_PER_MIN
           instead of being reused.
        2. FSET: write -> wait -> read back -> limited retry.
        3. RTOS: send, then confirm the iPS is in the To Set state (ACTN
           reports either the canonical token RTOS or the front-panel label
           "TO SET" - accept both). If the field is already inside the
           arrival band (factory current_epsilon = 0.1 A ~= 0.012 T) the iPS
           legitimately stays in HOLD with fine-trim - that counts as success
           (no-op ramp).

        Raises RuntimeError after the retry budget is exhausted; the control
        dispatcher surfaces this as "Set field+RTOS FAILED" in the log.
        """
        rate = ramp_rate_t_per_min
        if rate is None:
            try:
                rate = self.read_field_ramp_rate()
            except MercuryError:
                rate = None
            if not 0 < (rate or 0.0) <= MAX_FIELD_RAMP_T_PER_MIN:
                rate = DEFAULT_FIELD_RAMP_T_PER_MIN

        # ---- step 0: command only from HOLD (official workflow) -----------
        # Manual-click context: a 30 s budget covers any ramp about to finish;
        # a much longer active ramp should be stopped with HOLD first. Comm
        # errors propagate straight to the dispatcher ("Set field+RTOS
        # FAILED") - the operator is watching and can retry.
        deadline = time.monotonic() + 30.0
        while True:
            actn = self.action().upper()
            if "HOLD" in actn:
                break
            if "CLMP" in actn or "CLAMP" in actn:
                self.ips.set_action(MAGNET_GROUP, "HOLD")
                time.sleep(2.0)   # crow-bar relay action time
                continue
            if time.monotonic() > deadline:
                raise RuntimeError(
                    "iPS not in HOLD before the field command "
                    f"(last ACTN={actn or 'unreadable'}). Use HOLD first or "
                    "wait for the current ramp to finish.")
            time.sleep(0.5)

        self.set_field_ramp_rate(rate)

        last_exc: Exception | None = None
        verified_rb = float("nan")
        for attempt in range(1, 4):
            try:
                self.ips.set_group_signal(
                    MAGNET_GROUP, "PSU", "SIG", "FSET", f"{tesla:.10g}")
                time.sleep(1.0)
                rb = self.target_field()
                if abs(rb - tesla) <= 1e-4:
                    verified_rb = rb
                    break
                last_exc = RuntimeError(
                    f"FSET readback {rb:.6f} T != target {tesla:.6f} T")
            except MercuryError as exc:
                last_exc = exc
            time.sleep(1.0)
        else:
            raise RuntimeError(
                f"iPS FSET never verified after 3 attempts "
                f"(target {tesla:g} T): {last_exc}")

        for attempt in range(1, 4):
            try:
                self.ips.set_action(MAGNET_GROUP, "RTOS")
                time.sleep(1.0)
                actn_u = self.action().upper()
                # Canonical token RTOS (handbook command table) or the
                # front-panel label "TO SET" - both mean "ramp running".
                if ("RTOS" in actn_u or "TO SET" in actn_u
                        or "TOSET" in actn_u):
                    return [
                        f"RFST={rate:g} T/min",
                        f"FSET verified {verified_rb:.6f} T",
                        "RTOS confirmed",
                    ]
                # No-op ramp: already inside the arrival band -> HOLD with
                # fine-trim is the correct end state (handbook sec.4.4.20).
                if abs(self.field() - tesla) <= 0.012:
                    return [
                        f"RFST={rate:g} T/min",
                        f"FSET verified {verified_rb:.6f} T",
                        f"no-op ramp: field already at target "
                        f"(ACTN={actn_u}, HOLD fine-trim)",
                    ]
                last_exc = RuntimeError(f"ACTN={actn_u} after RTOS")
            except MercuryError as exc:
                last_exc = exc
            time.sleep(1.0)
        raise RuntimeError(
            f"iPS RTOS never confirmed after 3 attempts "
            f"(target {tesla:g} T): {last_exc}")

    def hold(self):
        return self.ips.set_action(MAGNET_GROUP, "HOLD")

    def ramp_zero(self):
        return self.ips.set_action(MAGNET_GROUP, "RTOZ")


@dataclass
class Readings:
    vti: float
    probe: float          # sample temperature
    pressure: float       # VTI exchange-gas pressure
    field: float
    target_field: float
    field_ramp_rate: float


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TeslatronPT Control")
        self.geometry("780x560")
        self.minsize(740, 520)
        self.protocol("WM_DELETE_WINDOW", self.shutdown)

        self.device = None
        self.stop_event = threading.Event()
        self.q = queue.Queue()
        # FIFO queue + single dispatcher thread for ALL hardware control
        # actions (see run_async): guarantees click order == execution order.
        self._ctl_queue = queue.Queue()
        self._ctl_dispatcher_started = False
        self._ctl_dispatcher = threading.Thread(
            target=self._ctl_dispatch_loop, daemon=True)
        self._ctl_dispatcher.start()
        self._ctl_dispatcher_started = True
        # A linked-control (10 K crossing) action is queued/in flight.
        self._linked_pending = False
        self.poll_busy = False

        # Linked-control state (VTI + probe always work together).
        self._auto_regime = None      # "low" / "high" / None
        self._probe_target = None     # last probe (sample) setpoint entered
        self._coupling_warn = False
        self._queue_after_id = None
        self._poll_after_id = None

        self.temp_var = tk.StringVar(value="10.0")
        self.press_var = tk.StringVar(value="10.0")
        self.field_var = tk.StringVar(value="0.0")
        self.field_ramp_var = tk.StringVar(value="0.1")
        self.auto_pressure = tk.BooleanVar(value=True)
        self._auto_pressure_on = True   # mirror of auto_pressure, safe to read off-thread

        # Temperature ramp (RSET = K/min, RENA = ramp enable).
        # ONE input; the same values are broadcast to BOTH control loops
        # (probe 114 + VTI 113) so the linked offset stays constant.
        self.ramp_rate_var = tk.StringVar(value=f"{DEFAULT_TEMP_RAMP_K_PER_MIN:g}")
        self.ramp_enable_var = tk.BooleanVar(value=False)

        self.vti_var = tk.StringVar(value="--")
        self.probe_var = tk.StringVar(value="--")
        self.diff_var = tk.StringVar(value="--")
        self.pressure_var = tk.StringVar(value="--")
        self.field_read_var = tk.StringVar(value="--")
        self.field_set_var = tk.StringVar(value="--")
        self.field_ramp_read_var = tk.StringVar(value="--")
        self.coupling_var = tk.StringVar(value="--")
        self.status_var = tk.StringVar(value="Connecting...")

        self.build_ui()
        threading.Thread(target=self.connect_worker, daemon=True).start()
        self._queue_after_id = self.after(300, self.process_queue)
        self._poll_after_id = self.after(1000, self.poll_readings)

    def build_ui(self):
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Oxford Instruments TeslatronPT",
                  font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(outer, text=f"iTC-VTI {ITC_VTI_HOST}:{PORT}    iTC-Probe {ITC_PROBE_HOST}:{PORT}    iPS {IPS_HOST}:{PORT}    GRPZ",
                  font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 4))
        ttk.Label(outer, textvariable=self.status_var,
                  font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 4))

        live = ttk.LabelFrame(outer, text="Live readings", padding=6)
        live.pack(fill="x", pady=4)
        items = [
            ("VTI bath / DB6.T1", self.vti_var, "K"),
            ("Sample (Probe) / DB8.T1", self.probe_var, "K"),
            ("Probe - VTI", self.diff_var, "K"),
            ("VTI pressure / DB3.P1", self.pressure_var, "mbar"),
            ("Field", self.field_read_var, "T"),
            ("Field setpoint", self.field_set_var, "T"),
            ("Field ramp rate / RFST", self.field_ramp_read_var, "T/min"),
        ]
        for i, (name, var, unit) in enumerate(items):
            r, c = divmod(i, 4)
            f = ttk.Frame(live)
            f.grid(row=r, column=c, sticky="ew", padx=4, pady=2)
            ttk.Label(f, text=name, font=("Segoe UI", 9)).pack(anchor="w")
            ttk.Label(f, textvariable=var,
                      font=("Segoe UI", 11, "bold")).pack(side="left")
            ttk.Label(f, text=f" {unit}", font=("Segoe UI", 9)).pack(side="left")
        for c in range(4):
            live.columnconfigure(c, weight=1)

        controls = ttk.LabelFrame(outer, text="Setpoints", padding=6)
        controls.pack(fill="x", pady=4)

        ttk.Label(controls, text="Sample temp (Probe)").grid(row=0, column=0, sticky="w", padx=4, pady=3)
        ttk.Entry(controls, textvariable=self.temp_var, width=9).grid(row=0, column=1, padx=4)
        ttk.Label(controls, text="K").grid(row=0, column=2, sticky="w")
        ttk.Button(controls, text="Set temperature", command=self.set_temperature).grid(row=0, column=3, padx=6)

        ttk.Label(controls, text="VTI pressure").grid(row=1, column=0, sticky="w", padx=4, pady=3)
        ttk.Entry(controls, textvariable=self.press_var, width=9).grid(row=1, column=1, padx=4)
        ttk.Label(controls, text="mbar").grid(row=1, column=2, sticky="w")
        ttk.Button(controls, text="Set pressure", command=self.set_pressure).grid(row=1, column=3, padx=6)
        ttk.Checkbutton(controls, text="Auto: <9.5 K→4; >10.5 K→10 mbar (±0.5 K 迟滞)",
                        variable=self.auto_pressure,
                        command=self.update_auto_pressure).grid(row=1, column=4, columnspan=3, padx=8, sticky="w")

        ttk.Label(controls, text="Magnetic field").grid(row=2, column=0, sticky="w", padx=4, pady=3)
        ttk.Entry(controls, textvariable=self.field_var, width=9).grid(row=2, column=1, padx=4)
        ttk.Label(controls, text="T · rate").grid(row=2, column=2, sticky="w")
        ttk.Entry(controls, textvariable=self.field_ramp_var, width=9).grid(row=2, column=3, padx=4)
        ttk.Label(controls, text="T/min ≤0.237").grid(row=2, column=4, sticky="w")
        ttk.Button(controls, text="Set field+RTOS", command=self.set_field).grid(row=2, column=5, padx=6)
        ttk.Button(controls, text="HOLD", command=self.hold).grid(row=2, column=6, padx=3)
        ttk.Button(controls, text="RTOZ", command=self.ramp_zero).grid(row=2, column=7, padx=3)

        # Temperature ramp: ONE rate + ONE RENA; broadcast to both loops.
        ttk.Label(controls, text="Temp ramp").grid(row=3, column=0, sticky="w", padx=4, pady=3)
        ttk.Entry(controls, textvariable=self.ramp_rate_var, width=9).grid(row=3, column=1, padx=4)
        ttk.Label(controls, text="K/min (auto → 114+113)").grid(row=3, column=2, sticky="w")
        ttk.Checkbutton(controls, text="RENA", variable=self.ramp_enable_var).grid(row=3, column=3, padx=4, sticky="w")
        ttk.Button(controls, text="Apply ramp", command=self.apply_temp_ramp).grid(row=3, column=5, padx=6)

        for c in range(8):
            controls.columnconfigure(c, weight=1)

        rule = ttk.LabelFrame(outer, text="Rule & coupling", padding=6)
        rule.pack(fill="x", pady=4)
        ttk.Label(rule, text=(
            "联动: 设T → probe TSET=T (114); VTI TSET=T−1.0K (≥10K)/T−0.2K (<10K) (113)"
            "   |   气压自动切换: <9.5K→4 mbar, >10.5K→10 mbar (±0.5K 迟滞)"
            "   |   Temp ramp: RENA=ON 时按 RSET 斜坡"
        ), font=("Segoe UI", 9)).pack(anchor="w")
        ttk.Label(rule, textvariable=self.coupling_var,
                  font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(2, 0))

        logf = ttk.LabelFrame(outer, text="Log", padding=5)
        logf.pack(fill="both", expand=True, pady=4)
        self.log = tk.Text(logf, height=5, wrap="word")
        self.log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=sb.set)

    def log_msg(self, text: str):
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
        # Keep the log bounded (long-running sessions used to grow forever).
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 600:
            self.log.delete("1.0", f"{lines - 500}.0")
        self.log.see("end")

    def connect_worker(self):
        try:
            dev = Teslatron()
            vti = dev.idn_vti()
            probe = dev.idn_probe()
            ips = dev.idn_ips()
            self.device = dev
            self.q.put(("status", "Connected"))
            self.q.put(("log", f"iTC-VTI (113): {vti}"))
            self.q.put(("log", f"iTC-Probe (114): {probe}"))
            self.q.put(("log", f"iPS (115): {ips}"))
        except Exception as exc:
            self.q.put(("error", f"Connection failed: {exc}"))

    def process_queue(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "status":
                    self.status_var.set(data)
                elif kind == "log":
                    self.log_msg(data)
                elif kind == "error":
                    self.status_var.set("Error")
                    self.log_msg(data)
                elif kind == "pressure_set":
                    self.press_var.set(data)
                elif kind == "probe_target_confirmed":
                    self._probe_target = data
                elif kind == "readings":
                    r = data
                    self.vti_var.set(f"{r.vti:.6g}")
                    self.probe_var.set(f"{r.probe:.6g}")
                    self.diff_var.set(f"{r.probe - r.vti:+.6g}")
                    self.pressure_var.set(f"{r.pressure:.6g}")
                    self.field_read_var.set(f"{r.field:.8g}")
                    self.field_set_var.set(f"{r.target_field:.8g}")
                    self.field_ramp_read_var.set(f"{r.field_ramp_rate:.6g}")
                    self._update_coupling(r)
        except queue.Empty:
            pass
        self._queue_after_id = self.after(300, self.process_queue)

    def _update_coupling(self, r: Readings):
        t_sample = r.probe
        exp = EXPECTED_DIFF_BELOW if t_sample < SAMPLE_T_SWITCH_K else EXPECTED_DIFF_ABOVE
        diff = r.probe - r.vti
        # Direction-sensitive: a healthy link has VTI COLDER than the probe
        # (diff > 0). abs(diff) would wrongly pass a reversed failure where
        # the VTI is HOTTER than the probe (probe heating runaway).
        reversed_link = diff < 0
        ok = (not reversed_link) and diff <= exp * 3.0 + 0.5
        if reversed_link:
            state = "⚠ VTI 比样品热(反向失控)"
        elif ok:
            state = "OK"
        else:
            state = "检查气压"
        self.coupling_var.set(
            f"样品 {t_sample:.2f} K | 期望 ΔT≈{exp:g} K | 实测 ΔT{diff:+.2f} K | 耦合: {state}"
        )
        if ok and self._coupling_warn:
            self._coupling_warn = False
            self.log_msg("VTI–Probe 热耦合已恢复。")
        elif not ok and not self._coupling_warn:
            self._coupling_warn = True
            if reversed_link:
                self.log_msg(
                    f"⚠⚠ VTI 比样品(probe)更热：ΔT={diff:+.2f} K，热耦合反向失效，"
                    f"疑似 probe 加热失控，请立即检查！"
                )
            else:
                self.log_msg(
                    f"⚠ VTI–Probe ΔT={diff:+.2f} K 超出期望≈{exp:g} K，请检查交换气气压/热耦合。"
                )

    def poll_readings(self):
        if self.device is not None and not self.poll_busy and not self.stop_event.is_set():
            self.poll_busy = True
            threading.Thread(target=self.poll_worker, daemon=True).start()
        self._poll_after_id = self.after(1000, self.poll_readings)

    def poll_worker(self):
        try:
            dev = self.device
            r = Readings(
                dev.vti_temp(),
                dev.probe_temp(),
                dev.pressure(),
                dev.field(),
                dev.target_field(),
                dev.read_field_ramp_rate(),   # read on background thread, not UI thread
            )
            # Linked control always active: on a 10 K crossing, re-align the VTI
            # offset (and the exchange-gas pressure when auto-pressure is on).
            self._enforce_linked_control(dev, r.probe)
            self.q.put(("readings", r))
        except Exception as exc:
            self.q.put(("log", f"Read error: {exc}"))
        finally:
            self.poll_busy = False

    def _pressure_regime(self, t_sample: float):
        """Return 'low' / 'high' / None (dead band) with hysteresis around 10 K."""
        if t_sample < SAMPLE_T_SWITCH_K - AUTO_PRESSURE_HYST:
            return "low"
        if t_sample > SAMPLE_T_SWITCH_K + AUTO_PRESSURE_HYST:
            return "high"
        return None

    def _enforce_linked_control(self, dev: Teslatron, t_sample: float):
        """When the sample (probe) crosses 10 K, re-apply the linked control:
           - VTI TSET = probe_target - offset (always, so VTI tracks probe);
           - exchange-gas pressure switch (only when auto-pressure is on).

        Runs on the poll thread but NEVER touches the hardware itself: the
        actions are ENQUEUED into the same FIFO dispatcher as the UI buttons,
        so there is exactly ONE hardware control channel. The regime marker
        is only advanced on success (otherwise the next poll retries).
        """
        regime = self._pressure_regime(t_sample)
        if regime is None or regime == self._auto_regime:
            return
        if self._linked_pending:
            return  # a linked-control action is already queued/in flight
        p = PRESSURE_BELOW_10K if regime == "low" else PRESSURE_ABOVE_10K
        offset = EXPECTED_DIFF_BELOW if regime == "low" else EXPECTED_DIFF_ABOVE
        base = self._probe_target if self._probe_target is not None else t_sample
        vti_target = base - offset
        auto_p = self._auto_pressure_on
        self._linked_pending = True

        def job():
            try:
                actions = []
                if auto_p:
                    dev.set_pressure(p)
                    actions.append(f"气压→{p:g} mbar")
                    # Only reflect the pressure in the UI when it was REALLY
                    # set (previously this ran unconditionally, so with
                    # auto-pressure OFF the entry showed a value the
                    # instrument never received).
                    self.q.put(("pressure_set", f"{p:g}"))
                dev.set_vti_temperature(vti_target)
                actions.append(
                    f"VTI TSET→{vti_target:g} K (probe基准 {base:g} K, 低 {offset:g} K)"
                )
                # Confirmed: only NOW advance the regime marker.
                self._auto_regime = regime
                return f"样品 {t_sample:.2f} K 越过 10 K: " + "; ".join(actions)
            finally:
                self._linked_pending = False

        self.run_async(job, "联动控制")

    @staticmethod
    def rule_pressure(t: float) -> float:
        return PRESSURE_BELOW_10K if t < SAMPLE_T_SWITCH_K else PRESSURE_ABOVE_10K

    def update_auto_pressure(self):
        on = self.auto_pressure.get()
        self._auto_pressure_on = on
        if not on:
            return
        # Re-arm the regime detector. The next poll uses the ACTUAL sample
        # temperature to choose the pressure regime; the confirmed probe
        # target is used only as the basis for the linked VTI TSET.
        self._auto_regime = None
        self.log_msg(
            "Auto-pressure 已开启：下一次轮询将按实测样品温度重新联动。"
        )

    def apply_temp_ramp(self):
        """Apply temperature ramp settings (RSET + RENA) to BOTH control loops.

        A single input value is broadcast to probe (114) and VTI (113) with the
        same RSET and RENA, so the linked VTI-Probe offset stays constant.
        RENA is loop-specific and remote-only; with RENA=ON every later TSET
        change (including the linked 10 K re-alignment) ramps at RSET (K/min).
        """
        if self.device is None:
            messagebox.showerror("Not connected", "TeslatronPT is not connected yet.")
            return
        try:
            rate = float(self.ramp_rate_var.get())
            if not (0 < rate <= MAX_TEMP_RAMP_K_PER_MIN):
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Input error",
                f"Temp ramp rate must be > 0 and <= {MAX_TEMP_RAMP_K_PER_MIN:g} K/min.")
            return
        # Read the toggle on the UI thread and capture it for the worker.
        on = self.ramp_enable_var.get()
        on_str = "ON" if on else "OFF"

        def job():
            return [
                self.device.set_probe_ramp_rate(rate),
                self.device.set_probe_ramp_enable(on),
                self.device.set_vti_ramp_rate(rate),
                self.device.set_vti_ramp_enable(on),
            ]
        self.run_async(
            job,
            f"Temp ramp {rate:g} K/min RENA {on_str} -> probe(114) + VTI(113)"
        )

    def run_async(self, fn, label):
        """Enqueue a control action for ordered asynchronous execution.

        A SINGLE dispatcher thread drains self._ctl_queue strictly in FIFO
        order. Previously every call spawned its own thread, so composite
        multi-command actions from rapid clicks (e.g. "Set field+RTOS"
        immediately followed by "HOLD") could be reordered by the OS
        scheduler — the HOLD could reach the magnet BEFORE the RTOS, leaving
        the magnet ramping instead of held. Click order now equals execution
        order; the UI still never blocks.
        """
        if self.stop_event.is_set():
            return
        self._ctl_queue.put((fn, label))

    def _ctl_dispatch_loop(self):
        while True:
            fn, label = self._ctl_queue.get()
            if fn is None:  # shutdown sentinel
                return
            try:
                result = fn()
                self.q.put(("log", f"{label}: OK; {result}"))
            except Exception as exc:
                self.q.put(("error", f"{label} FAILED: {exc}"))

    def set_temperature(self):
        if self.device is None:
            messagebox.showerror("Not connected", "TeslatronPT is not connected yet.")
            return
        try:
            t = float(self.temp_var.get())
            if not (EXPECTED_DIFF_BELOW < t <= 300):
                raise ValueError
        except ValueError:
            messagebox.showerror("Input error", f"Temperature must be > {EXPECTED_DIFF_BELOW:g} K and <= 300 K.")
            return
        # Linked control: send both setpoints (probe TSET = T on 114; VTI
        # TSET = T - offset on 113). _probe_target is only updated AFTER the
        # hardware command succeeds (via "probe_target_confirmed") — it must
        # represent the last CONFIRMED setpoint, not merely the last click;
        # otherwise a failed/partial command would leave the software
        # believing a setpoint the instrument never applied.
        off = vti_offset(t)

        def job():
            result = self.device.set_sample_temperature(t)
            # This is control state, not UI state: update it in the same
            # serialized hardware context that confirmed the write.
            self._probe_target = t
            return result

        self.run_async(
            job,
            f"联动控温: probe TSET {t:g} K; VTI TSET {t - off:g} K (低 {off:g} K)"
        )

    def set_pressure(self):
        if self.device is None:
            messagebox.showerror("Not connected", "TeslatronPT is not connected yet.")
            return
        try:
            p = float(self.press_var.get())
            if not (0 <= p <= 2000):
                raise ValueError
        except ValueError:
            messagebox.showerror("Input error", "Pressure must be between 0 and 2000 mbar.")
            return
        self.run_async(lambda: self.device.set_pressure(p), f"Pressure setpoint -> {p:g} mbar")

    def set_field(self):
        if self.device is None:
            messagebox.showerror("Not connected", "TeslatronPT is not connected yet.")
            return
        try:
            b = float(self.field_var.get())
            rate = float(self.field_ramp_var.get())
            # Hard ceiling: 0.237 T/min == approx. 39.5 Oe/s. Never exceed.
            if (not (-MAX_FIELD_T <= b <= MAX_FIELD_T)
                    or not (0 < rate <= MAX_FIELD_RAMP_T_PER_MIN)):
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Input error",
                f"Field must be -{MAX_FIELD_T:g} to +{MAX_FIELD_T:g} T; "
                "ramp rate must be > 0 and <= 0.237 T/min "
                "(<= approx. 39.5 Oe/s)."
            )
            return
        self.run_async(
            lambda: self.device.set_field(b, rate),
            f"Field -> {b:g} T; RFST -> {rate:g} T/min; FSET + RTOS"
        )

    def hold(self):
        if self.device is not None:
            self.run_async(self.device.hold, "Magnet HOLD")

    def ramp_zero(self):
        if self.device is not None:
            self.run_async(self.device.ramp_zero, "Magnet RTOZ")

    def shutdown(self):
        self.stop_event.set()
        # Orderly shutdown for a hardware-control program:
        #   1. stop accepting new work (stop_event, set above);
        #   2. DISCARD queued-but-not-started control commands — they must
        #      not execute after the window is gone;
        #   3. signal the dispatcher to exit after its current command;
        #   4. wait (bounded) for it to finish BEFORE closing the sockets,
        #      so close() cannot race an in-flight query and the Mercury
        #      auto-reconnect cannot resurrect the connection.
        try:
            while True:
                self._ctl_queue.get_nowait()
        except queue.Empty:
            pass
        self._ctl_queue.put((None, None))
        if self._ctl_dispatcher is not None:
            self._ctl_dispatcher.join(timeout=8.0)
        for aid in (self._queue_after_id, self._poll_after_id):
            if aid is not None:
                try:
                    self.after_cancel(aid)
                except Exception:
                    pass
        if self.device is not None:
            try:
                # Kill auto-reconnect BEFORE closing: an in-flight query
                # after the dispatcher join must fail fast, not resurrect
                # the connection and re-send a command post-shutdown.
                self.device.disable_reconnect()
                self.device.close()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
