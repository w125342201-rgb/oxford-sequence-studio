# -*- coding: utf-8 -*-
"""
Keithley 6221 + 2182A complete low-noise Delta driver
======================================================

Hardware:
    Keithley 6221 current source
    Keithley 2182A nanovoltmeter

Typical wiring:
    PC --GPIB/USB--> 6221 --RS-232--> 2182A
                         |
                         +-- Trigger Link --> 2182A

Project defaults:
    2182A NPLC / integration = 10 PLC (~200 ms @ 50 Hz)
    6221 Delta delay          = 0.1 s
    6221 Delta COUNT          = 1
    Trace points              = 1
    Phase marker              = ON for AC waveform mode

Important:
    - The 6221 is the controller of the 6221+2182A Delta measurement.
    - The 2182A is configured through the 6221 serial link when required.
    - The Trace Buffer is configured explicitly; no important buffer state
      is left to instrument defaults.
    - Phase Marker is intentionally retained/enabled by default.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time
from typing import Optional, List, Dict, Any

import pyvisa


DEFAULT_NPLC = 10.0              # 10 PLC ~= 200 ms at 50 Hz
DEFAULT_DELTA_DELAY = 0.100      # seconds
DEFAULT_DELTA_COUNT = 1
DEFAULT_TRACE_POINTS = 1
DEFAULT_VOLTAGE_RANGE = 0.1      # V, fixed range is preferred
DEFAULT_COMPLIANCE_V = 105.0     # 6221 output voltage compliance (max 105 V)
MAX_SOURCE_CURRENT_A = 0.105     # 6221 absolute source max: 100 mA nominal, 105 mA overload

# Reading units for Delta / Pulse Delta / Differential Conductance results
# (622x Reference Manual, Table 5-1: "UNIT[:VOLT][:DC] <name>",
# <name> = V, OHMS, W, or SIEMens; default V). Semantics (manual §5-16
# "Measurement units"): OHMS = V/I (differential resistance dV/dI),
# SIEM = I/V (differential conductance dI/dV), where V is the Delta/DCON
# voltage reading and I is the current sourced by the 622x.
DELTA_UNIT_ALIASES = {
    "V": "V", "VOLT": "V", "VOLTS": "V",
    "OHM": "OHMS", "OHMS": "OHMS",
    "W": "W", "WATT": "W", "WATTS": "W",
    "S": "SIEM", "SIEM": "SIEM", "SIEMENS": "SIEM",
}


def normalize_delta_unit(unit: str) -> str:
    """Map user/unit input onto the manual Table 5-1 UNIT <name> spellings."""
    key = str(unit).strip().upper()
    if key not in DELTA_UNIT_ALIASES:
        raise ValueError(
            f"Delta reading unit must be one of V/OHMS/W/SIEM "
            f"(622x Reference Manual Table 5-1); got {unit!r}.")
    return DELTA_UNIT_ALIASES[key]


class KeithleyError(RuntimeError):
    """Base error for the 6221/2182A driver."""


class Keithley6221Error(KeithleyError):
    """6221-specific error."""


class Keithley2182AError(KeithleyError):
    """2182A-specific error."""


def _is_ok_zero(msg: str) -> bool:
    return msg.startswith("0,") or msg.startswith("+0,")


class Keithley2182A:
    """Keithley 2182A nanovoltmeter driver."""

    def __init__(
        self,
        address: str,
        rm: Optional[pyvisa.ResourceManager] = None,
        timeout_ms: int = 10000,
    ):
        self.address = address
        self._owns_rm = rm is None
        self.rm = rm or pyvisa.ResourceManager()
        # Serializes every VISA session access. The measurement worker and the
        # STOP emergency thread both touch this session; without a lock they can
        # interleave write/read pairs on the same pyvisa handle and raise
        # VI_ERROR_RSRC_LOCKED / corrupt the response stream.
        self._lock = threading.RLock()
        self.inst = self.rm.open_resource(address)
        self.inst.timeout = timeout_ms
        self.inst.write_termination = "\n"
        self.inst.read_termination = "\n"
        self.inst.chunk_size = 4096

    def close(self) -> None:
        with self._lock:
            try:
                self.inst.close()
            finally:
                if self._owns_rm:
                    try:
                        self.rm.close()
                    except Exception:
                        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ---------------- communication ----------------

    def write(self, command: str):
        with self._lock:
            return self.inst.write(command)

    def query(self, command: str) -> str:
        with self._lock:
            return self.inst.query(command).strip()

    def idn(self) -> str:
        return self.query("*IDN?")

    def reset(self) -> None:
        self.write("*RST")

    def clear_status(self) -> None:
        self.write("*CLS")

    def clear_errors(self) -> List[str]:
        errors = []
        for _ in range(32):
            msg = self.query(":SYST:ERR?")
            errors.append(msg)
            if _is_ok_zero(msg):
                break
        return errors

    # ---------------- voltage configuration ----------------

    def configure_voltage(self, channel: int = 1) -> None:
        if channel not in (1, 2):
            raise ValueError("2182A channel must be 1 or 2.")
        self.write(":CONF:VOLT")
        self.write(f":SENS:CHAN {channel}")

    def set_channel(self, channel: int) -> None:
        if channel not in (1, 2):
            raise ValueError("2182A channel must be 1 or 2.")
        self.write(f":SENS:CHAN {channel}")

    def set_nplc(self, nplc: float = DEFAULT_NPLC) -> None:
        if not 0.01 <= nplc <= 60:
            raise ValueError("NPLC must be between 0.01 and 60.")
        self.write(f":SENS:VOLT:NPLC {nplc:g}")

    def get_nplc(self) -> float:
        return float(self.query(":SENS:VOLT:NPLC?"))

    def set_range(self, volts: float = DEFAULT_VOLTAGE_RANGE, auto: bool = False) -> None:
        if auto:
            self.write(":SENS:VOLT:RANG:AUTO ON")
            return
        if volts <= 0:
            raise ValueError("Voltage range must be > 0.")
        self.write(":SENS:VOLT:RANG:AUTO OFF")
        self.write(f":SENS:VOLT:RANG {volts:g}")

    def set_autozero(self, on: bool) -> None:
        self.write(f":SYST:AZER:STAT {'ON' if on else 'OFF'}")

    def set_null(self, on: bool, value: Optional[float] = None, channel: int = 1) -> None:
        if channel not in (1, 2):
            raise ValueError("2182A channel must be 1 or 2.")
        self.write(
            f":SENS:VOLT:CHAN{channel}:NULL:STAT {'ON' if on else 'OFF'}"
        )
        if on and value is not None:
            self.write(f":SENS:VOLT:CHAN{channel}:NULL:VAL {value:.12g}")

    # ---------------- trigger ----------------

    def set_trigger_source(self, source: str) -> None:
        source = source.upper()
        # 2182A trigger sources: IMM, EXT (external BNC), BUS, TLINk (Trigger
        # Link), TIM, MAN. NOTE: in 6221-mastered Delta mode this is driven by
        # the 6221 and does not normally need to be set by the user.
        if source not in {"IMM", "EXT", "BUS", "TLINk", "TLINK", "TIM", "MAN"}:
            raise ValueError("Trigger source must be IMM/EXT/BUS/TLINk/TIM/MAN.")
        self.write(f":TRIG:SOUR {source}")

    def set_trigger_delay(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("Trigger delay must be >= 0.")
        self.write(f":TRIG:DEL {seconds:.12g}")

    # ---------------- standalone 2182A buffer ----------------
    # Kept available, but NOT used for normal 6221-controlled Delta.

    def buffer_clear(self) -> None:
        self.write(":TRAC:CLE")

    def buffer_points(self, points: int) -> None:
        if not 1 <= points <= 1024:
            raise ValueError("2182A buffer points must be 1..1024.")
        self.write(f":TRAC:POIN {points}")

    def buffer_feed_next(self) -> None:
        self.write(":TRAC:FEED:CONT NEXT")

    def buffer_feed_never(self) -> None:
        self.write(":TRAC:FEED:CONT NEV")

    def buffer_count(self) -> int:
        return int(float(self.query(":TRAC:ACT?")))

    def buffer_data(self) -> List[float]:
        raw = self.query(":TRAC:DATA?")
        if not raw:
            return []
        return [float(x) for x in raw.split(",") if x.strip()]

    # ---------------- ordinary readings ----------------

    def fetch(self) -> float:
        return float(self.query(":FETC?"))

    def read(self) -> float:
        return float(self.query(":READ?"))

    def measure(self) -> float:
        return float(self.query(":MEAS:VOLT?"))

    # ---------------- project profile ----------------

    def configure_for_6221_delta(
        self,
        channel: int = 1,
        voltage_range: float = DEFAULT_VOLTAGE_RANGE,
        nplc: float = DEFAULT_NPLC,
        autozero: bool = True,
    ) -> None:
        """
        Configure the 2182A before the 6221 Delta run.

        NPLC=10 is intentional: about 200 ms at 50 Hz.
        """
        self.configure_voltage(channel)
        self.set_range(voltage_range, auto=False)
        self.set_nplc(nplc)
        # NOTE: in 6221-mastered Delta mode the 2182A trigger is driven by the
        # 6221 over the Trigger Link; do NOT set the 2182A trigger source here.
        self.set_autozero(autozero)

    # ---------------- direct 6221 serial-link helpers ----------------

    def configure_via_6221(
        self,
        send_to_2182,
        voltage_range: float = DEFAULT_VOLTAGE_RANGE,
        nplc: float = DEFAULT_NPLC,
    ) -> None:
        """
        Configure the 2182A via the 6221's serial link.

        Useful when the official 6221-controlled Delta workflow is being
        followed from the 6221 side.
        """
        send_to_2182(f"VOLT:RANG {voltage_range:g}")
        send_to_2182(f"VOLT:NPLC {nplc:g}")


class Keithley6221:
    """Keithley 6221 current source with complete Delta/Trace control."""

    def __init__(
        self,
        address: str,
        rm: Optional[pyvisa.ResourceManager] = None,
        timeout_ms: int = 10000,
    ):
        self.address = address
        self._owns_rm = rm is None
        self.rm = rm or pyvisa.ResourceManager()
        # Serializes every VISA session access (see Keithley2182A).
        self._lock = threading.RLock()
        self.inst = self.rm.open_resource(address)
        self.inst.timeout = timeout_ms
        self.inst.write_termination = "\n"
        self.inst.read_termination = "\n"
        self.inst.chunk_size = 8192

    def close(self) -> None:
        with self._lock:
            try:
                self.inst.close()
            finally:
                if self._owns_rm:
                    try:
                        self.rm.close()
                    except Exception:
                        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ---------------- communication ----------------

    def write(self, command: str):
        with self._lock:
            return self.inst.write(command)

    def query(self, command: str) -> str:
        with self._lock:
            return self.inst.query(command).strip()

    def idn(self) -> str:
        return self.query("*IDN?")

    def reset(self) -> None:
        self.write("*RST")

    def clear_status(self) -> None:
        self.write("*CLS")

    def clear_errors(self) -> List[str]:
        errors = []
        for _ in range(32):
            msg = self.query(":SYST:ERR?")
            errors.append(msg)
            if _is_ok_zero(msg):
                break
        return errors

    # ---------------- DC ----------------

    def _wait(self, seconds):
        callback = getattr(self, 'wait_callback', None)
        if callback is None:time.sleep(seconds)
        else:callback(seconds)

    def _check_cancel(self):
        callback = getattr(self, 'cancel_check', None)
        if callback is not None:callback()

    def check_errors(self, context):
        replies=self.clear_errors()
        if not replies or not _is_ok_zero(replies[-1]):
            raise Keithley6221Error(context+': error queue did not terminate')
        for reply in replies:
            try:code=int(reply.split(',',1)[0])
            except (ValueError,AttributeError):raise Keithley6221Error(context+': invalid error reply '+repr(reply))
            if code < 0 or code >= 400 or code in (112,114,301):
                raise Keithley6221Error(context+': '+reply)
        return replies

    def verify_number(self, command, expected, relative=1e-6, absolute=1e-12, query=None):
        replies=[]
        for attempt in range(3):
            self._check_cancel()
            raw=(query or self.query)(command);replies.append(raw)
            try:value=float(raw)
            except (ValueError,TypeError):value=float('nan')
            if math.isfinite(value) and math.isclose(value,expected,rel_tol=relative,abs_tol=absolute):
                if attempt:
                    callback=getattr(self,'verification_log',None)
                    if callback:callback(f'{command} 只读复核恢复，回读序列: {replies!r}')
                return value
            if attempt<2:self._wait(0.1)
        raise Keithley6221Error(f'{command} requested {expected}, read back {replies!r} after 3 read-only checks')

    def verify_ac(self, freq, amplitude, marker_position, compliance_v):
        values={cmd:self.verify_number(cmd,val) for cmd,val in (
            ('SOUR:WAVE:FREQ?',freq),('SOUR:WAVE:AMPL?',amplitude),
            ('SOUR:WAVE:OFFS?',0.),('SOUR:WAVE:PMAR?',marker_position),
            ('SOUR:WAVE:PMAR:OLIN?',1),('SOUR:WAVE:PMAR:STAT?',1),
            ('SOUR:WAVE:EXTR?',0),('SOUR:WAVE:EXTR:ILIN?',0),
            ('SOUR:CURR:COMP?',compliance_v))}
        for cmd,prefix in (('SOUR:WAVE:FUNC?','SIN'),('SOUR:WAVE:RANG?','BEST')):
            value=self.query(cmd).strip().upper()
            if not value.startswith(prefix):raise Keithley6221Error(cmd+' unexpected '+repr(value))
            values[cmd]=value
        self.check_errors('AC configuration')
        return values

    def source_health(self, require_wave=False):
        # Reference Manual: MEAS B3 compliance, B2 overtemperature, B0 overflow.
        condition=int(float(self.query('STAT:MEAS:COND?')))
        event=int(float(self.query('STAT:MEAS:EVEN?')))
        operation=int(float(self.query('STAT:OPER:COND?')))
        if (condition|event) & 13:
            raise Keithley6221Error(f'Source compliance/temperature/overflow: cond={condition}, event={event}')
        if require_wave:
            self.verify_number(':OUTP?',1)
            if not operation & 16:raise Keithley6221Error('6221 waveform is not running')
        errors=self.check_errors('Source status')
        return dict(measurement_condition=condition,measurement_event=event,
                    operation_condition=operation,error_queue=errors)

    def set_current(self, amps: float) -> None:
        amps = float(amps)
        # Driver-level last line of defence (GUI layers validate too):
        # 6221 absolute maximum is 100 mA nominal / 105 mA overload.
        if not -MAX_SOURCE_CURRENT_A <= amps <= MAX_SOURCE_CURRENT_A:
            raise ValueError(
                f"Current {amps:g} A exceeds the 6221 absolute limit of "
                f"+/-{MAX_SOURCE_CURRENT_A:g} A."
            )
        self.write(f":SOUR:CURR {amps:.15g}")

    def get_current(self) -> float:
        return float(self.query(":SOUR:CURR?"))

    def set_current_range_auto(self, on: bool = True) -> None:
        self.write(f":SOUR:CURR:RANG:AUTO {'ON' if on else 'OFF'}")

    def set_current_range(self, amps: float) -> None:
        if amps <= 0:
            raise ValueError("Current range must be > 0.")
        self.write(f":SOUR:CURR:RANG {float(amps):.15g}")

    def set_compliance(self, volts: float) -> None:
        if not math.isfinite(volts) or not 0.1 <= volts <= 105:
            raise ValueError("Compliance must be within 0.1..105 V.")
        self.write(f":SOUR:CURR:COMP {float(volts):.15g}")

    def output_on(self) -> None:
        self.write("OUTPUT ON")

    def output_off(self) -> None:
        self.write("OUTPUT OFF")

    def output_state(self) -> bool:
        """
        查询 6221 输出状态（:OUTP?）。

        Returns
        -------
        bool
            True 表示输出 ON，False 表示输出 OFF。
        """
        resp = self.query(":OUTP?").strip().upper()
        return resp.startswith("1") or resp == "ON"

    def set_display_enabled(self, on: bool) -> None:
        """
        Enable/disable the 6221 front panel (``DISPlay:ENABle <b>``,
        622x Reference Manual Table 13-1).

        Manual notes:

          * "With front panel circuitry turned off, the instrument operates
            at a higher speed."
          * The up-to-1 ms step-to-step timing jitter of Sweeps, Delta and
            Differential Conductance is eliminated by disabling the front
            panel. Disable it BEFORE arming/starting the test: disabling
            while such a test is running forces a 2-second pause.
          * ``*RST`` and ``SYSTem:PRESet`` do NOT restore the display.
            Recovery: send ``set_display_enabled(True)``, press LOCAL or
            DISP on the panel, or power-cycle the instrument.
        """
        self.write(f":DISP:ENAB {'ON' if on else 'OFF'}")

    # ---------------- AC waveform ----------------

    def configure_ac(
        self,
        freq: float,
        amplitude: float,
        offset: float = 0.0,
        func: str = "SIN",
        phase_marker: bool = True,
        marker_position: int = 0,
        marker_line: int = 1,
    ) -> None:
        """
        Configure AC waveform.

        Phase Marker is intentionally ON by default for this project. The
        marker (a 1 us pulse once per waveform cycle at ``marker_position``
        degrees) is routed to ``SOUR:WAVE:PMAR:OLIN`` on the rear Trigger
        connector (manual ch. 7): connect that line to an external digitiser
        (e.g. PXI-4462 PFI 0) to start acquisition phase-locked to the wave.
        The marker line must NOT equal the trigger-layer output line (default
        2) or the wave external-trigger input line (-221 Settings Conflict).
        """
        if freq <= 0:
            raise ValueError("AC frequency must be > 0.")
        if amplitude < 0:
            raise ValueError("AC amplitude must be >= 0.")
        if not 1 <= int(marker_line) <= 6:
            raise ValueError("Phase-marker trigger line must be 1..6.")

        self.write('SOUR:WAVE:EXTR OFF')
        self.write('SOUR:WAVE:EXTR:ILIN 0')
        # Reserve marker line 1; legacy trigger-layer output must not share it.
        self.write('TRIG:OLIN 2')
        self.write('SOUR:WAVE:RANG BEST')
        self.write('FORM:SREG ASC')
        self.write(f"SOUR:WAVE:FUNC {func.upper()}")
        self.write(f"SOUR:WAVE:FREQ {float(freq):.15g}")
        self.write(f"SOUR:WAVE:AMPL {float(amplitude):.15g}")
        self.write(f"SOUR:WAVE:OFFS {float(offset):.15g}")
        self.write(f"SOUR:WAVE:PMAR:STAT {'ON' if phase_marker else 'OFF'}")
        self.write(f"SOUR:WAVE:PMAR {int(marker_position)}")
        self.write(f"SOUR:WAVE:PMAR:OLIN {int(marker_line)}")
        self.write("SOUR:WAVE:DUR:TIME INF")

    def arm_ac(self) -> None:
        self.write("SOUR:WAVE:ARM")

    def init_ac(self) -> None:
        self.write("SOUR:WAVE:INIT")

    # ---------------- 6221 -> 2182A serial link ----------------

    def send_to_2182(self, command: str) -> None:
        # The 6221 serial-link node is SYST:COMM:SERIAL: (full SERIAL). Using the
        # abbreviation SYST:COMM:SER: is an UNDEFINED HEADER (-113) on this
        # firmware, which silently dropped every 2182A config command (NPLC /
        # range). Hardware-verified 2026-08-31: the SER -> SERIAL correction
        # removes the -113 and makes the 2182A configuration actually take
        # effect. (No BAUD/FLOW commands are sent; the link uses the instrument
        # default serial config, per the official Tektronix Delta example.)
        self.write(f':SYST:COMM:SERIAL:SEND "{command}"')

    def query_2182(self, command: str, timeout_s: float = 4.0, step_s: float = 0.2) -> str:
        """
        Send a query to the 2182A over the 6221 serial link and read the
        response back.

        The 6221 serial link holds a single receive buffer: ``SYST:COMM:SERIAL:SEND``
        is NOT a query, so the 2182A's reply must be fetched with
        ``SYST:COMM:SERIAL:ENT?``. We first drain any stale buffer, then send, then
        poll ENT? until a non-empty response arrives (the 2182A reply latency is
        variable, so a single fixed-wait read often returns empty).
        """
        # Drain any stale bytes so ENT? returns this query's response.
        for _ in range(6):
            try:
                if not self.query(":SYST:COMM:SERIAL:ENT?").strip():
                    break
            except Exception:
                break
            self._wait(0.1)
        self.send_to_2182(command)
        deadline = time.monotonic() + timeout_s
        last = ""
        while time.monotonic() < deadline:
            try:
                r = self.query(":SYST:COMM:SERIAL:ENT?").strip()
            except Exception:
                r = ""
            if r:
                return r
            last = r
            self._wait(step_s)
        return last

    # ---------------- Delta ----------------

    def delta_nvpresent(self, timeout_s: float = 5.0, step_s: float = 0.2) -> bool:
        """
        Return True if the 6221 detects the 2182A.

        Official 6221 SCPI command (matches the PyMeasure Keithley6221 source
        and the Tektronix 622x/2182A reference example): ":SOUR:DELT:NVPR?" --
        the NanoVoltmeter PResent query. The earlier "NVPRES?" / "NVPResent?"
        spellings are an UNDEFINED HEADER (-113) on real firmware, which is
        exactly why Delta never armed. The RS-232 link (19200 baud + XON/XOFF)
        and the Trigger Link cable must both be present.

        Hardware-verified 2026-08-31: NVPR? is a LIVE POLL of the 2182A over
        the serial link, not a latched status bit. While the 2182A is busy
        processing a configuration command it can momentarily report 0 and then
        return to 1 a moment later. A single one-shot read therefore produces
        spurious failures, so this polls until the link settles.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                resp = self.query(":SOUR:DELT:NVPR?").strip()
            except Exception:
                resp = ""
            if resp[:1] == "1":
                return True
            self._wait(step_s)
        return False

    def setup_delta(
        self,
        high: float,
        low: float,
        delay: float = DEFAULT_DELTA_DELAY,
        count: int = DEFAULT_DELTA_COUNT,
        compliance_abort: bool = True,
        cold_switch: bool = False,
        feed: str = "CALC1",
        compliance_v: float = DEFAULT_COMPLIANCE_V,
        voltage_range: float = DEFAULT_VOLTAGE_RANGE,
        nplc: float = DEFAULT_NPLC,
        reset_before: bool = True,
        guard: bool = False,
        low_to_earth: bool = False,
        averaging: bool = False,
        avg_count: int = 10,
        avg_type: str = "REP",
        configure_2182: bool = True,
        auto_range: bool = False,
        delta_unit: str = "V",
    ) -> None:
        """
        Configure the 6221+2182A Delta measurement (6221-mastered).

        Ported from the official Tektronix reference example
        ``Instrument_Examples/Model_622x_2182A/Delta_Measurement_Example.py``
        (github.com/tektronix/keithley), cross-checked against the PyMeasure
        ``Keithley6221`` source. Command order and settle times follow the
        official example exactly -- they matter on real hardware.

        Key points (hardware-verified 2026-08-31):

          * The 6221 is the master. The 2182A is configured ONLY through the
            6221 RS-232 serial link (``SYST:COMM:SERIAL:SEND``) and is triggered
            internally by the 6221 over the Trigger Link.
          * NO trigger source is set on either instrument: ``:TRIG:SOUR`` is
            not part of the delta flow and broke earlier attempts.
          * The official flow performs **no** 2182A presence gate.
            ``:SOUR:DELT:NVPR?`` is a live poll that momentarily reads 0 while
            the 2182A is busy, so it is exposed as a diagnostic only
            (see :meth:`delta_nvpresent`) and never gates a run.
          * Sweep completion is detected via ``STAT:OPER:EVEN?`` bit 1.
            ``:TRAC:POIN:ACT?`` is deliberately NOT used: it hangs on this
            firmware and was the cause of the earlier timeouts.
          * ``:TRAC:POIN`` is deliberately NOT set: the 6221 sizes the delta
            buffer from ``SOUR:DELT:COUNT``, and an explicit ``TRAC:POIN 1``
            is rejected with -222 anyway.
          * A FIXED 2182A voltage range is preferred over auto-range: the HIGH
            and LOW readings must share one range, otherwise autoranging
            between them corrupts the delta.

        Args:
            high / low: source current in A (high > 0, low < 0). The effective
                delta current is (high - low) / 2; resistance = Vdelta / deltaI.
            delay: seconds between the HIGH and LOW source states.
            count: number of delta readings to acquire.
            compliance_abort / cold_switch: 6221 Delta safety options.
            compliance_v: 6221 output voltage compliance (0.1 .. 105 V).
            voltage_range: FIXED 2182A voltage range in V (0.01/0.1/1/10/100).
                Ignored when ``auto_range`` is True.
            nplc: 2182A integration in PLC (~200 ms @ 50 Hz for NPLC=10).
            reset_before: *RST the 6221 (and 2182A) before configuring. Keep
                True for a standalone run; the GUI may pass False for repeated
                points to avoid the reset overhead.
            guard / low_to_earth: 6221 output isolation options (default off).
            averaging: enable 2182A averaging on the 2182A (via serial link).
            avg_count: averaging count when averaging is on (2..300).
            avg_type: "REP" (repeat) or "MOV" (moving) averaging.
            configure_2182: send the 2182A setup through the 6221 serial link.
            auto_range: let the 2182A autorange. NOT recommended for delta.
            delta_unit: reading unit reported by the 6221 for Delta results
                (manual Table 5-1): "V" (Delta voltage, default), "OHMS"
                (V/I = differential resistance dV/dI -- the instrument
                converts internally), "SIEM" (I/V = differential conductance
                dI/dV), or "W" (power). Sent at the END of the sequence
                (i.e. after the *RST above), so the selection always takes
                effect regardless of whether *RST resets UNIT.
        """
        high = float(high)
        low = float(low)
        if not (high > 0):
            raise ValueError("Delta 'high' current must be > 0 A.")
        if not (low < 0):
            raise ValueError("Delta 'low' current must be < 0 A.")
        if high > MAX_SOURCE_CURRENT_A or low < -MAX_SOURCE_CURRENT_A:
            raise ValueError(
                f"Delta high/low must stay within +/-{MAX_SOURCE_CURRENT_A:g} A "
                f"(6221 absolute limit)."
            )
        if delay < 0:
            raise ValueError("delay must be >= 0 s.")
        if count < 1:
            raise ValueError("count must be >= 1.")
        if avg_count < 2 or avg_count > 300:
            raise ValueError("avg_count must be between 2 and 300.")

        # 1) Abort tests in the other modes, then reset the 6221.
        self.write("SOUR:SWE:ABOR")
        self.write("SOUR:WAVE:ABOR")
        # Reset the 6221 and (optionally) re-link the 2182A serial config.
        if reset_before:
            self.write("*RST")
            self._wait(2.0)
            # NOTE: deliberately NO :SYST:COMM:SERIAL:BAUD / :FLOW XON commands.
            # These are NOT in the official Tektronix Delta example and are
            # rejected as -113 "Undefined header" on this firmware. The
            # 6221<->2182A link uses the instrument-default serial config
            # (19200/XON), with 2182A's XON/XOFF flow set from its front panel.

        # 2) Output isolation (default: inner shield to LOW, no low-to-earth).
        self.write(f"OUTP:ISH {'GUARD' if guard else 'OLOW'}")
        self.write(f"OUTP:LTE {'ON' if low_to_earth else 'OFF'}")
        self._wait(0.5)

        # 3) Trace element format. The official example requests
        #    READ,TST,RNUM,SOUR (4 values per reading); this project needs only
        #    the delta voltage, so request READ alone -> one number per reading.
        self.write("FORM:ELEM READ")

        # 4) 6221 Delta parameters. NOTE: :TRAC:POIN is deliberately NOT set --
        #    the 6221 sizes the delta buffer from SOUR:DELT:COUNT, and an
        #    explicit "TRAC:POIN 1" is rejected with -222 on this firmware.
        self.write(f"SOUR:DELT:HIGH {high:.15g}")
        self._wait(0.1)
        self.write(f"SOUR:DELT:LOW {low:.15g}")
        self.write(f"SOUR:DELT:COUNT {int(count)}")
        self.write(f"SOUR:DELT:DEL {float(delay):.15g}")
        self.write(f"SOUR:CURR:COMP {float(compliance_v):.15g}")
        self.write(f"SOUR:CURR:RANG {max(abs(high), abs(low)):.15g}")
        self.write(f":SOUR:DELT:CAB {'ON' if compliance_abort else 'OFF'}")
        self.write(f":SOUR:DELT:CSW {'ON' if cold_switch else 'OFF'}")

        # 5) Configure the 2182A through the 6221 RS-232 serial link.
        #
        #    ARCHITECTURE (verified against the 6221 Reference Manual Table 5-3 and
        #    the official Tektronix Delta example): in Delta mode the 6221 is the
        #    MASTER. Its ":SOUR:DELT:ARM" command performs the arming hand-shake
        #    with the 2182A and puts it into Delta mode over the serial link -- the
        #    host never triggers or sequences the 2182A directly. So the CONTROL
        #    path is fully 6221-owned.
        #
        #    The ONLY thing the host still must do on the 2182A is set its two
        #    MEASUREMENT PARAMETERS -- NPLC and voltage range. These are 2182A-
        #    native settings; the 6221 has NO SCPI to set them, so they must be
        #    relayed through the serial link. (This is exactly "configuring a
        #    voltmeter", not controlling the measurement -- same as DC mode, where
        #    the 2182A is just a benchtop voltmeter on the GPIB bus.)
        #
        #    We deliberately DO NOT send "*RST" to the 2182A: it is redundant
        #    because (a) we re-set NPLC + fixed RANG on every combo, overriding any
        #    stale state, and (b) ":SOUR:DELT:ARM" rebuilds the Delta link each
        #    time. Dropping it also honours the principle that the 2182A should be
        #    touched as little as possible in Delta mode. Any command not genuinely
        #    needed is rejected as -113 on this firmware and, because the serial
        #    link is one-way, lights the 2182A front panel WITHOUT reaching our
        #    logs -- so we keep this block to the bare minimum.
        if configure_2182:
            # --- 2182A-only configuration, relayed through the 6221 serial link ---
            # Only TRUE 2182A commands belong here. Per the official Tektronix
            # Delta example, the 2182A receives *RST, :SENS:VOLT:NPLC and
            # :SENS:VOLT:RANG across the serial link. We keep NPLC + RANG and drop
            # *RST (see note above).
            self.send_to_2182(f":SENS:VOLT:NPLC {float(nplc):g}")
            self._wait(2.0)
            # A FIXED range is REQUIRED for delta: autoranging between the HIGH
            # and LOW readings would corrupt the difference (each reading could hit
            # a different range). The official example always sets a fixed
            # :SENS:VOLT:RANG (0.01/0.1/1/10/100).
            if auto_range:
                self.send_to_2182(":SENS:VOLT:RANG:AUTO ON")
            else:
                self.send_to_2182(f":SENS:VOLT:RANG {float(voltage_range):g}")
            self._wait(2.0)

        # --- 6221-side source/filter AVERAGING ---
        # CRITICAL (root cause of the "-113 once per matrix switch" bug): the
        # official example sends SENS:AVER:WIND / SENS:AVER:TCON / SENS:AVER:STAT
        # / SENS:AVER:COUNT directly to the 6221 (the 6221 is the GPIB device in
        # that example), NOT through the serial link to the 2182A. These are 6221
        # commands. Forwarding SENS:AVER:* to the 2182A over the serial link is an
        # UNDEFINED HEADER (-113) on the 2182A, which lit its front panel once per
        # combo (= once per matrix switch) even though the measurement itself still
        # worked. Route them to the 6221 here so neither instrument errors.
        self.write(":SENS:AVER:WIND 0")
        self._wait(0.5)
        # 6221 averaging uses NUMERIC 0/1 for :SENS:AVER:STAT, matching the
        # official example ("ON"/"OFF" is rejected as -113 on this firmware).
        if averaging:
            self.write(f":SENS:AVER:TCON {avg_type}")
            self.write(f":SENS:AVER:COUNT {int(avg_count)}")
            self.write(":SENS:AVER:STAT 1")
        else:
            self.write(":SENS:AVER:TCON MOV")
            self.write(":SENS:AVER:STAT 0")
        self._wait(2.0)

        # 6) Reading units (manual Table 5-1, sent AFTER the *RST above):
        #    V (Delta voltage, default), OHMS (V/I = differential resistance),
        #    SIEM (I/V = differential conductance), W (power). With OHMS
        #    selected the Trace Buffer readings ARE the differential
        #    resistance in ohms (the 6221 divides by the sourced current
        #    internally).
        self.write(f"UNIT {normalize_delta_unit(delta_unit)}")

    def arm_delta(self, check_nvpresent: bool = False) -> None:
        """
        Arm the Delta measurement (``:SOUR:DELT:ARM``) and let the 6221 settle,
        as the official Tektronix example does (3 s).

        ``check_nvpresent`` defaults to False: the official flow does NOT gate
        on ``:SOUR:DELT:NVPR?``, and that flag is a live poll that can
        momentarily read 0 while the 2182A is busy processing configuration.
        Pass True only for a deliberate connectivity check.
        """
        if check_nvpresent and not self.delta_nvpresent():
            raise Keithley6221Error(
                "6221 reports no suitable 2182A is present. "
                "Check the RS-232 (19200/XON) and Trigger Link connections."
            )
        self.write(":SOUR:DELT:ARM")
        self._wait(3.0)

    def wait_for_sweep_done(self, timeout_s: float = 60.0, poll_s: float = 1.0) -> bool:
        """
        Block until the 6221 reports the delta sweep is complete.

        Uses the official mechanism: the Operation Event register
        (``STAT:OPER:EVEN?``), whose bit 1 means "sweep done". This replaces
        the earlier ``:TRAC:POIN:ACT?`` poll, which hangs on this firmware and
        was the direct cause of the previous timeouts.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self._check_cancel()
            try:
                status = int(float(self.query("STAT:OPER:EVEN?")))
            except Exception:
                status = 0
            if status & (1 << 1):
                return True
            self._wait(poll_s)
        return False

    def is_delta_armed(self) -> bool:
        # Best-effort: not all firmware exposes :SOUR:DELT:ARM?; if the query is
        # rejected we assume the arm write succeeded and continue.
        try:
            return self.query(":SOUR:DELT:ARM?").strip()[:1] == "1"
        except Exception:
            return True

    def start_delta(self) -> None:
        self.write(":INIT:IMM")

    def disarm_delta(self) -> None:
        self.write(":SOUR:SWE:ABOR")

    def abort(self) -> None:
        self.write(":ABOR")

    # ---------------- Trace Buffer ----------------

    def trace_clear(self) -> None:
        self.write(":TRAC:CLE")

    def trace_points(self, points: int) -> None:
        if not 1 <= points <= 65536:
            raise ValueError("6221 trace points must be 1..65536.")
        self.write(f":TRAC:POIN {int(points)}")

    def trace_points_set(self) -> int:
        return int(float(self.query(":TRAC:POIN?")))

    def trace_points_actual(self) -> int:
        return int(float(self.query(":TRAC:POIN:ACT?")))

    def trace_feed(self, feed: str) -> None:
        feed = feed.upper()
        if feed not in {"SENS1", "CALC1", "NONE"}:
            raise ValueError("Trace feed must be SENS1, CALC1 or NONE.")
        self.write(f":TRAC:FEED {feed}")

    def trace_feed_control(self, control: str) -> None:
        control = control.upper()
        if control not in {"NEXT", "NEV"}:
            raise ValueError("Trace feed control must be NEXT or NEV.")

        self.write(f":TRAC:FEED:CONT {control}")

    def trace_data_type(self) -> str:
        return self.query(":TRAC:DATA:TYPE?")

    def trace_data(self) -> str:
        return self.query(":TRAC:DATA?")

    def trace_data_selected(self, start: int, count: int) -> str:
        if start < 1 or count < 1:
            raise ValueError("start and count must be >= 1.")
        return self.query(f":TRAC:DATA:SEL? {int(start)},{int(count)}")

    # ---------------- Complete project Delta run ----------------

    # ------------------------------------------------------------------
    # 2026-11-07: delta execution restored VERBATIM to the proven 1017-era
    # flow (261031buckup snapshot -- the code that ran the 9090-point R-B / 
    # 3400-point R-T delta sweeps on 2026-10-18..27). User decision: no
    # serial-link round-trip verification, no settings-reuse cache, no extra
    # behaviour. Only mechanical change: time.sleep -> self._wait (same
    # durations; keeps engine STOP responsiveness). The post-1017 additions
    # (verify_delta_settings / reuse_2182_settings / _delta_2182_cache) were
    # removed: their per-point 2182A serial round trips have variable ~4 s
    # latency on real hardware and failed every run of WCG 20261106 at its
    # first delta point ('2182A configuration: ""').
    # ------------------------------------------------------------------
    def run_delta_once(
        self,
        high: float,
        low: float,
        delay: float = DEFAULT_DELTA_DELAY,
        count: int = DEFAULT_DELTA_COUNT,
        feed: str = "CALC1",
        timeout_s: float = 60.0,
        configure_2182: bool = True,
        voltage_range: float = DEFAULT_VOLTAGE_RANGE,
        nplc: float = DEFAULT_NPLC,
        compliance_v: float = DEFAULT_COMPLIANCE_V,
        reset_before: bool = True,
        guard: bool = False,
        low_to_earth: bool = False,
        averaging: bool = False,
        avg_count: int = 10,
        auto_range: bool = False,
        delta_unit: str = "V",
    ) -> Dict[str, Any]:
        """
        Perform one verified 6221-controlled Delta acquisition.

        This follows the Keithley-recommended 6221-mastered Delta flow: the
        2182A is configured and triggered entirely through the 6221 (RS-232 +
        Trigger Link). No :TRIG:SOUR is set on either instrument.

        Default:
            COUNT = 1
            DELAY = 100 ms
            2182A NPLC = 10 (~200 ms @ 50 Hz)
            compliance_v = 105 V (6221 max)

        Args:
            feed: accepted for backward compatibility (ignored; the trace is
                always read as the delta voltage via FORM:ELEM READ).
            voltage_range: hint for the 2182A range (AUTO is applied).
            reset_before: *RST the 6221/2182A first. The GUI may pass False on
                repeated points to avoid the ~4 s reset overhead.
            guard / low_to_earth / averaging / avg_count: see setup_delta.
            delta_unit: see setup_delta (manual Table 5-1). With "OHMS" the
                trace readings are already the differential resistance dV/dI
                in ohms, so ``resistance`` equals the reading and
                ``voltage_delta`` is None (no voltage was reported). With
                "V" the readings are Delta volts and ``resistance`` is
                computed as reading / ((HIGH - LOW) / 2). Only V/OHMS carry a
                resistance semantic; SIEM/W leave ``resistance`` None.

        Returns a dict with the raw Trace Buffer response and parsed numbers.
        The GUI reads ``trace_data`` (a single numeric value for count=1) and
        ``data_type``.
        """
        if count < 1:
            raise ValueError("run_delta_once requires count >= 1.")

        # Drain + clear any stale 6221 error-queue entries BEFORE configuring.
        # A transient serial-link error (e.g. -241 "Hardware missing") would
        # otherwise linger and corrupt the subsequent Trace read. The drained
        # errors are returned (pre_errors) so the caller can surface them.
        pre_errors = []
        try:
            pre_errors = self.clear_errors()
        except Exception:
            pre_errors = []

        # Effective delta current = (high - low) / 2. Resistance = Vdelta / deltaI.
        delta_current = (float(high) - float(low)) / 2.0

        self.setup_delta(
            high=high,
            low=low,
            delay=delay,
            count=count,
            feed=feed,
            compliance_v=compliance_v,
            voltage_range=voltage_range,
            nplc=nplc,
            reset_before=reset_before,
            guard=guard,
            low_to_earth=low_to_earth,
            averaging=averaging,
            avg_count=avg_count,
            configure_2182=configure_2182,
            auto_range=auto_range,
            delta_unit=delta_unit,
        )

        # NOTE: there is deliberately NO NVPR? presence gate here (see the
        # setup_delta docstring). The official Tektronix flow arms and starts
        # directly; gating on the live NVPR? poll caused spurious failures.
        self.arm_delta()
        self.start_delta()

        # Wait for completion via the Operation Event register (official
        # mechanism). :TRAC:POIN:ACT? is NOT used -- it hangs on this firmware.
        line_freq = 50.0
        if not self.wait_for_sweep_done(timeout_s=timeout_s):
            # Do not leave the 6221 armed with its output live after a failed
            # acquisition: best-effort abort + disarm before raising.
            try:
                self.abort()
            except Exception:
                pass
            try:
                self.disarm_delta()
            except Exception:
                pass
            raise TimeoutError(
                f"Delta sweep did not complete within {timeout_s:g} s "
                f"(STAT:OPER:EVEN? bit 1 was never set)."
            )
        actual = count

        # Read the delta result. :TRAC:DATA? (PyMeasure delta_values) is the
        # buffer; fall back to :SENS:DATA? (PyMeasure delta_sense, latest
        # reading) if the buffer query is empty or fails.
        # The DATA read comes FIRST: the optional :TRAC:DATA:TYPE? query is not
        # supported by every firmware revision, and if it errors we do not want
        # a bad query to corrupt the response stream before the reading is
        # fetched.
        try:
            raw = self.trace_data()
        except Exception:
            raw = ""
        if not raw.strip():
            try:
                raw = self.query(":SENS:DATA?")
            except Exception:
                raw = ""
        try:
            dtype = self.trace_data_type()
        except Exception:
            dtype = ""

        # Abort the sweep so the 6221 output is not left live (official flow).
        try:
            self.disarm_delta()
        except Exception:
            pass

        # Parse: one reading per point, in the unit selected via delta_unit
        # (V by default; OHMS = differential resistance, see setup_delta).
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        readings = []
        for tok in tokens:
            try:
                readings.append(float(tok))
            except ValueError:
                pass
        unit = normalize_delta_unit(delta_unit)
        reading = readings[0] if readings else None
        if unit == "V":
            voltage_delta = reading
            # Reference Manual section 5: Delta = (X - 2Y + Z) / 4
            # (with alternating cycle sign). For a linear sample this is
            # R * (HIGH - LOW) / 2, not the full voltage difference.
            # Preserve the reported voltage; use the half-span current for R.
            resistance = (voltage_delta / delta_current) \
                if voltage_delta is not None else None
        elif unit == "OHMS":
            # The 6221 already divided by the sourced current: the reading
            # IS dV/dI in ohms. No voltage was reported.
            voltage_delta = None
            resistance = reading
        else:
            # SIEM/W: no direct resistance semantic.
            voltage_delta = None
            resistance = None

        return {
            "expected_count": count,
            "actual_count": actual,
            "data_type": dtype,
            "trace_data": raw,
            "pre_errors": pre_errors,
            "delta_unit": unit,
            "reading": reading,
            "voltage_delta": voltage_delta,
            "resistance": resistance,
            "delta_current": delta_current,
            "readings": readings,
            "nplc": nplc,
            "integration_time_estimate_s_at_50Hz": nplc / line_freq,
            "delta_delay_s": delay,
        }

    # ------------------------------------------------------------------
    # Differential Conductance (DCON): one stepped-bias segment
    # ------------------------------------------------------------------
    #
    # Official 6221 feature (manual ch. 5 "Delta, Pulse Delta, and
    # Differential Conductance", Figure 5-10 + Table 5-4 / SCPI Table B-6;
    # command order follows the official example in that chapter):
    #
    #   * The 6221 outputs a stepped sweep START -> STOP in STEP increments
    #     and adds the fixed differential current DELTA (= dI) around every
    #     bias point. The 2182A (RS-232 + Trigger Link, 6221-mastered - the
    #     SAME wiring and arming handshake as Delta) returns ONE dV per bias
    #     point, from the 3-reading sliding window
    #         dV = (X - 2Y + Z) / 4    with alternating sign (-1)^(n-1)
    #   * The BASIC reading is volts (dV). With OHMS / SIEMENS units the 6221
    #     also reports dR = dV/dI or dG = dI/dV.
    #   * The source range is set automatically to the best fixed range on
    #     ARM (manual note) - no explicit :SOUR:CURR:RANG here.
    #   * DCON is un-armed with SOUR:SWE:ABOR (same command as Delta).
    #
    # This wrapper reads the raw dV (UNIT V) so the caller computes
    # dR = dV/dI and dG = dI/dV itself - transparent and auditable. The trace
    # must hold one dV per bias point, hence TRAC:POIN = len(sequence) (the
    # official example does the same for its 6-point sweep).
    #
    # NOTE: DCON sweep direction follows START/STOP like a normal sweep (the
    # manual only shows ascending examples; a descending START > STOP segment
    # is sent as-is and verified on hardware).

    def run_dcon_segment(
        self,
        start: float,
        stop: float,
        step: float,
        delta: float,
        delay: float = DEFAULT_DELTA_DELAY,
        nplc: float = DEFAULT_NPLC,
        voltage_range: float = DEFAULT_VOLTAGE_RANGE,
        compliance_v: float = DEFAULT_COMPLIANCE_V,
        compliance_abort: bool = True,
        timeout_s: float = 600.0,
        reset_before: bool = True,
        configure_2182: bool = True,
    ) -> Dict[str, Any]:
        """
        Run one 6221 Differential Conductance segment (bias sweep).

        Args:
            start / stop: bias sweep limits (A), either direction.
            step: bias increment (A, > 0).
            delta: differential current dI (A, 0 < delta <= 0.105). Every bias
                point is measured with +/-delta applied around it.
            delay: settling delay per delta level (s).
            nplc / voltage_range: 2182A integration and FIXED voltage range,
                relayed over the 6221 serial link (same as Delta).
            compliance_v: 6221 output voltage compliance.
            compliance_abort: abort the DCON run if the source hits compliance.
            reset_before: *RST the 6221 first (keep False for a re-run of a
                segment to skip the ~4 s reset overhead).
            configure_2182: (re)send NPLC + range to the 2182A over the link.

        Returns:
            dict with ``readings`` (the per-bias dV list, in scan order) plus
            the raw trace / count. dR = dV/dI and dG = dI/dV are left to the
            caller.
        """
        start = float(start)
        stop = float(stop)
        step = float(step)
        delta = float(delta)
        if abs(start) > MAX_SOURCE_CURRENT_A or abs(stop) > MAX_SOURCE_CURRENT_A:
            raise ValueError(
                f"DCON start/stop must stay within +/-{MAX_SOURCE_CURRENT_A:g} A.")
        if step <= 0:
            raise ValueError("DCON step must be > 0 A.")
        if not (0 < delta <= MAX_SOURCE_CURRENT_A):
            raise ValueError(
                f"DCON delta (dI) must be in (0, {MAX_SOURCE_CURRENT_A:g}] A.")
        if delay < 1e-3:
            # Table 5-5: SOUR:DCON:DELay = 1e-3 .. 9999.999 s (min 1 ms).
            raise ValueError("DCON delay must be >= 1 ms (manual limit).")

        # Bias list maps 1:1 onto the trace readings (one dV per bias point).
        #
        # HARDWARE FACT (user-verified 2026-09-04): the 6221 DCON engine does
        # NOT accept a descending sweep - a segment with START > STOP fails at
        # arming with +416 "Step size too small" (a generic config error; the
        # ascending examples are the only documented use). Auto-flip such a
        # segment to an ascending run (lo -> hi) and reverse the readings back
        # to the caller's original order. The differential reading is local
        # (one small-signal +/-dI around each bias), so the flip costs no
        # physics - only the traversal order changes.
        descending = stop < start
        lo, hi = (stop, start) if descending else (start, stop)
        # P1 guard (manual p5-57): bias +/- dI must stay within the 6221
        # source limit. Error +411 "Diff. Conductance step error" corrupts
        # data but the run CONTINUES - fail loudly here instead.
        edge = max(abs(lo), abs(hi))
        if edge + delta > MAX_SOURCE_CURRENT_A:
            raise ValueError(
                f"DCON bias edge {edge:g} A plus delta {delta:g} A exceeds "
                f"the +/-{MAX_SOURCE_CURRENT_A:g} A source limit "
                f"(+411 corrupts data silently); reduce the sweep limits or dI.")
        if step > (hi - lo):
            # Manual p5-55: a step larger than the start-stop interval makes
            # the 6221 readjust the sweep to 100 points. Mirror that here so
            # the bias sequence and TRAC:POIN match what the engine will run.
            # (Equivalent of np.linspace(lo, hi, 100), without the numpy
            # dependency - this was an undefined-name crash waiting to fire.)
            biases = [lo + (hi - lo) * k / 99 for k in range(100)]
        else:
            biases = dcon_bias_sequence(lo, hi, step)   # ascending to 6221
        n_points = len(biases)
        if n_points < 2:
            raise ValueError(
                "DCON needs at least 2 bias points per segment (manual shows "
                "only multi-point sweeps; a single-point segment cannot be "
                "run as Differential Conductance).")

        # Drain + clear any stale 6221 error-queue entries first (same reason
        # as run_delta_once: a transient serial-link error would otherwise
        # linger and corrupt the trace read).
        try:
            self.clear_errors()
        except Exception:
            pass

        # Abort any other mode, then reset.
        self.write("SOUR:SWE:ABOR")
        self.write("SOUR:WAVE:ABOR")
        if reset_before:
            self.write("*RST")
            self._wait(2.0)

        # Output isolation defaults (inner shield -> LO, no low-to-earth).
        self.write("OUTP:ISH OLOW")
        self.write("OUTP:LTE OFF")
        self._wait(0.5)

        # One element per reading: the dV value (official example requests
        # READ,TST,RNUM,SOUR; this project needs only READ - same as Delta).
        self.write("FORM:ELEM READ")

        # DCON parameters (Table 5-4 / B-6). lo/hi: always ascending to the
        # 6221 (descending sweeps are refused at arming with +416).
        self.write(f"SOUR:CURR:COMP {float(compliance_v):.15g}")
        self.write(f"SOUR:DCON:STAR {lo:.15g}")
        self.write(f"SOUR:DCON:STEP {step:.15g}")
        self.write(f"SOUR:DCON:STOP {hi:.15g}")
        self.write(f"SOUR:DCON:DELT {delta:.15g}")
        self.write(f"SOUR:DCON:DEL {float(delay):.15g}")
        self.write(f"SOUR:DCON:CAB {'ON' if compliance_abort else 'OFF'}")

        # 2182A measurement parameters over the serial link (fixed range:
        # the +delta/-delta readings must share one range, same as Delta).
        if configure_2182:
            self.send_to_2182(f":SENS:VOLT:NPLC {float(nplc):g}")
            self._wait(2.0)
            self.send_to_2182(f":SENS:VOLT:RANG {float(voltage_range):g}")
            self._wait(2.0)

        # Units: volts - the BASIC DCON reading is dV. (OHMS/SIEMENS would
        # make the 6221 convert internally; reading dV keeps the conversion
        # transparent and auditable in the GUI.) UNIT is a GLOBAL 6221
        # setting, so this explicitly re-asserts V here: a previous Delta-R
        # run may have left OHMS selected (see setup_delta delta_unit), and
        # the DCON bias/dV mapping in the GUI assumes voltages.
        self.write("UNIT V")
        # The DCON buffer holds one dV per bias point; size it explicitly
        # (official example: TRAC:POIN 6 for a 6-point sweep).
        self.trace_points(n_points)

        # Arm + start (official sequence). The 6221 talks to the 2182A during
        # arming, exactly like Delta.
        self.write(":SOUR:DCON:ARM")
        self._wait(3.0)

        # Fast-fail diagnostics after ARM: a configuration error (e.g. +416
        # "Step size too small" - the step rounded down to zero against the
        # auto-selected source range) lands in the error queue here and makes
        # the 6221 refuse the arm; the sweep never starts and STAT:OPER:EVEN?
        # bit 1 would otherwise never set, stalling the run until the full
        # sweep timeout. Surface the queue NOW for an immediate clear error.
        try:
            errs = []
            for _ in range(8):
                msg = self.query("SYST:ERR?")
                errs.append(msg)
                if _is_ok_zero(msg):
                    break
        except Exception:
            errs = []
        bad = [m for m in errs if m and not _is_ok_zero(m)]
        if bad:
            raise Keithley6221Error(
                "DCON refused to arm: " + "; ".join(bad))
        try:
            armed = self.query(":SOUR:DCON:ARM?").strip()[:1] == "1"
        except Exception:
            armed = True   # ARM? unsupported on some firmware: trust the write
        if not armed:
            raise Keithley6221Error(
                "DCON did not arm (SOUR:DCON:ARM? = 0).")

        self.write(":INIT:IMM")

        if not self.wait_for_sweep_done(timeout_s=timeout_s):
            try:
                self.abort()
            except Exception:
                pass
            try:
                self.disarm_delta()          # SOUR:SWE:ABOR also un-arms DCON
            except Exception:
                pass
            raise TimeoutError(
                f"DCON sweep (start {start:g} A) did not complete within "
                f"{timeout_s:g} s (STAT:OPER:EVEN? bit 1 never set).")

        # Read the per-bias dV values (same read path as run_delta_once).
        try:
            raw = self.trace_data()
        except Exception:
            raw = ""
        if not raw.strip():
            try:
                raw = self.query(":SENS:DATA?")
            except Exception:
                raw = ""
        try:
            dtype = self.trace_data_type()
        except Exception:
            dtype = ""

        # Leave the 6221 un-armed, output not live (official flow).
        try:
            self.disarm_delta()
        except Exception:
            pass

        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        readings = []
        for tok in tokens:
            try:
                readings.append(float(tok))
            except ValueError:
                pass
        if not readings:
            raise RuntimeError(
                f"DCON completed but the trace could not be parsed as "
                f"voltages. TYPE={dtype!r}; RAW={raw!r}")
        if len(readings) < n_points:
            # Some firmware trims the sliding-window head; pad-raise clearly
            # rather than silently misaligning the bias map.
            raise RuntimeError(
                f"DCON returned {len(readings)} dV values for {n_points} bias "
                f"points (raw={raw!r}) - bias/dV mapping would misalign.")

        # Re-map back to the caller's original order when the segment was
        # auto-flipped (readings arrive ascending; the caller expects them in
        # START->STOP order).
        biases_out = biases
        readings_out = readings
        if descending:
            biases_out = list(reversed(biases))
            readings_out = list(reversed(readings))

        return {
            "expected_points": n_points,
            "actual_points": len(readings),
            "biases": biases_out,
            "data_type": dtype,
            "trace_data": raw,
            "readings": readings_out,   # dV per bias point, in START->STOP order
            "descending_auto_flip": descending,
            "dI": delta,
            "nplc": nplc,
            "integration_time_estimate_s_at_50Hz": nplc / 50.0,
            "delta_delay_s": delay,
        }


@dataclass
class DeltaConfig:
    """Project defaults for the 6221+2182A Delta experiment."""

    high_current_a: float = 10e-6
    low_current_a: float = -10e-6
    nplc: float = DEFAULT_NPLC
    delay_s: float = DEFAULT_DELTA_DELAY
    count: int = DEFAULT_DELTA_COUNT
    voltage_range_v: float = DEFAULT_VOLTAGE_RANGE
    trace_feed: str = "CALC1"


# ===========================================================================
# Differential Conductance (DCON) - official 6221 feature
#
# Manual evidence (Model 6220/6221 User's Manual, ch. 5 "Delta, Pulse Delta,
# and Differential Conductance", Figure 5-10; SCPI Table B-6 / Table 5-4):
#   * The 6221 outputs a stepped sweep START->STOP in STEP increments and adds
#     the fixed differential current DELTA (= dI) around every bias point.
#   * The 2182A (RS-232 + Trigger Link, 6221-mastered - the SAME wiring and
#     arming handshake as Delta) returns ONE dV per bias point. Each dV comes
#     from the 3-reading sliding window  dV = (X - 2Y + Z)/4  with an
#     alternating sign (-1)^(n-1) that keeps all readings the same polarity.
#   * The BASIC reading is volts (dV). With OHMS / SIEMENS units selected the
#     6221 also reports dR = dV/dI or dG = dI/dV.
#   * Source range is set automatically to the best fixed range on ARM.
#   * DCON is un-armed with SOUR:SWE:ABOR (same as Delta).
#
# This wrapper reads the raw dV (UNIT V) and lets the caller compute
# dR = dV/dI, dG = dI/dV itself - transparent and auditable.
# ===========================================================================
def dcon_bias_sequence(start: float, stop: float, step: float):
    """Return the DCON bias list START..STOP (either direction) that maps 1:1
    onto the DCON trace readings (one reading per bias point)."""
    direction = 1 if stop >= start else -1
    seq = []
    x = start
    for _ in range(1_000_000):
        if direction > 0 and x > stop + 1e-12:
            break
        if direction < 0 and x < stop - 1e-12:
            break
        seq.append(round(x, 12))
        x += direction * abs(step)
    else:
        raise ValueError(
            "DCON segment too long: more than 1,000,000 points. "
            "Increase the step size or split it into more segments.")
    if not seq or abs(seq[-1] - stop) > 1e-10:
        seq.append(stop)
    return seq


def create_instruments(
    current_source_address: str = "GPIB0::12::INSTR",
    nanovoltmeter_address: str = "GPIB0::3::INSTR",
):
    """
    Create a shared VISA resource manager and both instruments.

    Returns:
        rm, src, dmm
    """
    rm = pyvisa.ResourceManager()
    src = Keithley6221(current_source_address, rm)
    dmm = Keithley2182A(nanovoltmeter_address, rm)
    return rm, src, dmm


if __name__ == "__main__":
    print("Keithley 6221 + 2182A driver loaded.")
    print("Defaults:")
    print(f"  NPLC       = {DEFAULT_NPLC:g} PLC (~{DEFAULT_NPLC/50:.3f} s @ 50 Hz)")
    print(f"  Delta delay = {DEFAULT_DELTA_DELAY:g} s")
    print(f"  Delta count = {DEFAULT_DELTA_COUNT}")
    print(f"  Trace points = {DEFAULT_TRACE_POINTS}")
    print("  Phase Marker = ON by default in AC mode")
