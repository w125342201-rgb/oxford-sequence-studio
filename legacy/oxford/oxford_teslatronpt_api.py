# -*- coding: utf-8 -*-
"""
Oxford Instruments TeslatronPT Python API
=========================================

Based directly on the latest verified TeslatronPT GUI/hardware definition.

Hardware topology
-----------------
iTC-VTI   192.168.1.10:7020
    DB6.T1  -> VTI bath temperature
    DB3.P1  -> exchange-gas pressure

iTC-Probe 192.168.1.11:7020
    DB8.T1  -> sample/probe temperature
    DB8.T1  -> sample temperature control loop

iPS       192.168.1.12:7020
    GRPZ    -> magnet group

Linked temperature control
--------------------------
User target T is the SAMPLE/PROBE temperature.

    Probe TSET = T
    VTI   TSET = T - offset

    T < 10 K   -> offset = 0.2 K
    T >= 10 K  -> offset = 1.0 K

Exchange-gas rule
-----------------
The automatic-pressure controller uses ACTUAL probe temperature.

    low-temperature regime : 4 mbar
    high-temperature regime: 10 mbar

A +/-0.5 K hysteresis is used around 10 K to avoid repeatedly changing
pressure while running repeated R-T scans around the transition.

Temperature ramp
----------------
The two temperature loops are controlled together:

    Probe RSET = VTI RSET = rate
    Probe RENA = VTI RENA = enabled

Magnet
------
    RFST = field ramp rate (T/min)
    FSET = target field (T)
    RTOS = ramp to target
    HOLD = hold
    RTOZ = ramp to zero

Maximum field ramp rate used by the lab GUI:
    0.237 T/min

Communication
-------------
Oxford Mercury controllers use raw TCP sockets on port 7020 with LF
termination. This module intentionally does not require VISA.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import socket
import threading
import time
import math
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Machine configuration
# ---------------------------------------------------------------------------

ITC_VTI_HOST = "192.168.1.10"
ITC_PROBE_HOST = "192.168.1.11"
IPS_HOST = "192.168.1.12"
MERCURY_PORT = 7020

VTI_MODULE = "DB6"
PROBE_MODULE = "DB8"
PRESSURE_MODULE = "DB3"
MAGNET_GROUP = "GRPZ"

SAMPLE_T_SWITCH_K = 10.0
PRESSURE_BELOW_10K_MBAR = 4.0
PRESSURE_ABOVE_10K_MBAR = 10.0
VTI_OFFSET_BELOW_10K_K = 0.2
VTI_OFFSET_ABOVE_10K_K = 1.0
AUTO_PRESSURE_HYST_K = 0.5

MAX_FIELD_RAMP_T_PER_MIN = 0.237
# Software ceiling for the field SETPOINT (T). This is a last-resort guard
# against typos (e.g. 1.2 vs 12). The installed magnet is rated 14 T
# (user-confirmed 2026-08-30). The iPS applies its own hardware limit
# regardless.
MAX_FIELD_T = 14.0
MAX_TEMP_RAMP_K_PER_MIN = 100.0

# Plan-A verified set_field constants.
# FSET readback tolerance (T): the iPS stores targets to >=5 decimal digits,
# so anything larger than 1e-4 T means the set did not stick.
FSET_READBACK_TOL_T = 1e-4
# Arrival band (T): factory current_epsilon = 0.1 A and the field conversion
# factor is 8.4237 A/T (J1119 factory config), i.e. the iPS itself considers
# the ramp finished within ~0.0119 T of the target (fine-trim band).
EPSILON_ARRIVAL_TOL_T = 0.012

def normalize_action(value):
    """Normalize known ACTN tokens; preserve the raw reply separately."""
    token=str(value or '').strip().upper()
    return {'HOLD':'HOLD','RTOS':'RTOS','TO SET':'RTOS','TOSET':'RTOS',
            'RTOZ':'RTOZ','TO ZERO':'RTOZ','TOZERO':'RTOZ',
            'CLAMP':'CLMP','CLMP':'CLMP','PAUSE':'PAUS','PAUS':'PAUS'}.get(token,'UNKNOWN')


# Fallback ramp rate (T/min) for set_field_verified(ramp=None): the magnet's
# residual RFST must never be reused blindly - a stale zero RFST is the known
# root cause of "FSET accepted but the field never ramps".
DEFAULT_FIELD_RAMP_T_PER_MIN = 0.237

DEFAULT_TEMP_RAMP_K_PER_MIN = 5.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class OxfordMercuryError(RuntimeError):
    """Base class for Oxford Mercury communication/control errors."""


class MercuryCommunicationError(OxfordMercuryError):
    """TCP communication failure."""


class MercuryInvalidResponseError(OxfordMercuryError):
    """Mercury returned INVALID or an otherwise unusable response."""


class TeslatronControlError(OxfordMercuryError):
    """High-level TeslatronPT control error."""


# ---------------------------------------------------------------------------
# Low-level Mercury TCP transport
# ---------------------------------------------------------------------------

_VALUE_RE = re.compile(r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)")


class MercuryController:
    """
    Thread-safe raw TCP client for one Oxford Mercury controller.

    Protocol:
        query -> command + '\\n'
        reply -> one LF-terminated line

    A controller maintains one persistent TCP connection. If a query fails,
    the connection is recreated once and the same command is retried.
    """

    def __init__(self, host: str, port: int = MERCURY_PORT, timeout: float = 5.0):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self._sock: Optional[socket.socket] = None
        self._lock = threading.RLock()
        # Receive buffer: bytes after the first LF of a reply are kept for the
        # next read instead of being discarded (response-stream robustness).
        self._rxbuf = bytearray()
        # Auto-reconnect on communication failure. Disabled during orderly
        # shutdown so an in-flight query cannot resurrect a closed connection
        # and re-send a command after the program has exited (see
        # TeslatronPT.disable_reconnect).
        self._reconnect_enabled = True
        self.connect()

    def connect(self) -> None:
        with self._lock:
            self.close()
            try:
                self._sock = socket.create_connection(
                    (self.host, self.port),
                    timeout=self.timeout,
                )
                self._sock.settimeout(self.timeout)
            except OSError as exc:
                raise MercuryCommunicationError(
                    f"Cannot connect to {self.host}:{self.port}: {exc}"
                ) from exc

    def set_reconnect(self, enabled: bool) -> None:
        """Enable/disable automatic reconnection on communication failure.

        Disabled during orderly shutdown: an in-flight query must fail fast
        instead of resurrecting a closed connection (see
        TeslatronPT.disable_reconnect).
        """
        with self._lock:
            self._reconnect_enabled = bool(enabled)

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
            self._rxbuf = bytearray()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _readline(self) -> str:
        if self._sock is None:
            raise MercuryCommunicationError(
                f"{self.host}:{self.port} is not connected"
            )

        try:
            while b"\n" not in self._rxbuf:
                chunk = self._sock.recv(4096)
                if not chunk:
                    # A connection that is accepted by the TCP handshake but then
                    # closed by the server without sending any data almost always
                    # means a *different* client already holds the controller's
                    # single persistent 7020 connection (Mercury iTC / iPS allow
                    # exactly one client per port). The usual culprit is the
                    # Oxford GUI or another control program still running on the same
                    # machine or elsewhere on the network. Close that client
                    # first, then retry.
                    raise MercuryCommunicationError(
                        f"{self.host}:{self.port} closed the connection. "
                        f"This usually means another client (e.g. the Oxford "
                        f"GUI or another control program) already holds the controller's "
                        f"single 7020 connection - Mercury allows only one "
                        f"client per port. Close that client and retry."
                    )
                self._rxbuf.extend(chunk)
        except socket.timeout as exc:
            raise MercuryCommunicationError(
                f"Timeout reading from {self.host}:{self.port}"
            ) from exc
        except OSError as exc:
            raise MercuryCommunicationError(
                f"Read failed from {self.host}:{self.port}: {exc}"
            ) from exc

        # Keep any bytes after the first LF for the next read (previously they
        # were silently discarded, which could desynchronise the reply stream).
        line, _, rest = self._rxbuf.partition(b"\n")
        self._rxbuf = bytearray(rest)
        return line.decode("ascii", errors="replace").strip()

    def query(self, command: str) -> str:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Mercury command must be a non-empty string.")

        payload = (command.strip() + "\n").encode("ascii")

        with self._lock:
            for attempt in range(2):
                if self._sock is None:
                    if not self._reconnect_enabled:
                        raise MercuryCommunicationError(
                            f"{self.host}:{self.port} reconnect is disabled "
                            f"(shutdown in progress)")
                    self.connect()

                try:
                    self._sock.sendall(payload)
                    return self._readline()
                except (OSError, MercuryCommunicationError):
                    self.close()
                    if not self._reconnect_enabled:
                        raise
                    if attempt == 0:
                        # NOTE: the failed command is re-sent verbatim. All
                        # commands issued through this API (READ/*, *IDN? and
                        # the SET/ACTN set FSET/RFST/TSET/RSET/RENA/RTOS/HOLD/
                        # RTOZ/CLMP/PAUS) are idempotent, so a duplicate after
                        # an ambiguous read-timeout is harmless. Do NOT route
                        # non-idempotent commands through this retry path.
                        continue
                    raise

        raise MercuryCommunicationError("Unreachable query path.")

    @staticmethod
    def check_valid(response: str) -> str:
        response = response.strip()
        if (
            response == "INVALID"
            or response.endswith(":INVALID")
            or ":INVALID:" in response
        ):
            raise MercuryInvalidResponseError(
                f"Mercury returned INVALID: {response}"
            )
        return response

    @classmethod
    def parse_value(cls, response: str) -> float:
        response = cls.check_valid(response)

        parts = response.split(":")
        if "VALID" in parts:
            idx = parts.index("VALID")
            if idx == 0:
                raise MercuryInvalidResponseError(
                    f"Malformed Mercury response: {response}"
                )
            token = parts[idx - 1]
        else:
            token = parts[-1]

        match = _VALUE_RE.search(token)
        if not match:
            raise MercuryInvalidResponseError(
                f"Cannot parse numeric Mercury response: {response}"
            )
        return float(match.group(1))

    @classmethod
    def parse_status(cls, response: str) -> str:
        """Parse a string-valued status token (e.g. ACTN HOLD/RTOS/CLMP).

        Reply shapes that must both be accepted:
          STAT:DEV:GRPZ:PSU:ACTN:HOLD:VALID   (some simulators/firmware)
          STAT:DEV:GRPZ:PSU:ACTN:HOLD         (REAL iPS - no trailing VALID)
        Returns the token before VALID when present, else the last field.
        """
        response = cls.check_valid(response)
        parts = response.split(":")
        if "VALID" in parts:
            idx = parts.index("VALID")
            if idx == 0:
                raise MercuryInvalidResponseError(
                    f"Malformed Mercury response: {response}"
                )
            token = parts[idx - 1].strip()
        else:
            token = parts[-1].strip()
        if not token:
            raise MercuryInvalidResponseError(
                f"Cannot parse string Mercury response: {response}"
            )
        return token

    def read_group_status(self, group: str, block: str, field: str) -> str:
        command = f"READ:DEV:{group}:{block}:{field}"
        return self.parse_status(self.query(command))

    def idn(self) -> str:
        return self.query("*IDN?")

    def read_signal(self, module: str, channel: str, block: str, field: str) -> float:
        command = f"READ:DEV:{module}.{channel}:{block}:SIG:{field}"
        return self.parse_value(self.query(command))

    def read_group_signal(self, group: str, block: str, field: str) -> float:
        command = f"READ:DEV:{group}:{block}:SIG:{field}"
        return self.parse_value(self.query(command))

    def set_signal(
        self,
        module: str,
        channel: str,
        block: str,
        sub: str,
        field: str,
        value,
    ) -> str:
        command = (
            f"SET:DEV:{module}.{channel}:{block}:{sub}:{field}:{value}"
        )
        return self.check_valid(self.query(command))

    def set_group_signal(
        self,
        group: str,
        block: str,
        sub: str,
        field: str,
        value,
    ) -> str:
        command = f"SET:DEV:{group}:{block}:{sub}:{field}:{value}"
        return self.check_valid(self.query(command))

    def set_action(self, group: str, action: str) -> str:
        action = action.upper()
        allowed = {"HOLD", "RTOS", "RTOZ", "CLMP", "PAUS"}
        if action not in allowed:
            raise ValueError(f"Unsupported magnet action: {action}")
        return self.check_valid(
            self.query(f"SET:DEV:{group}:PSU:ACTN:{action}")
        )


# ---------------------------------------------------------------------------
# Temperature / pressure / magnet data objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ThermalRule:
    """Temperature-to-offset/pressure rule used by the lab."""

    switch_temperature_k: float = SAMPLE_T_SWITCH_K
    low_pressure_mbar: float = PRESSURE_BELOW_10K_MBAR
    high_pressure_mbar: float = PRESSURE_ABOVE_10K_MBAR
    low_offset_k: float = VTI_OFFSET_BELOW_10K_K
    high_offset_k: float = VTI_OFFSET_ABOVE_10K_K
    hysteresis_k: float = AUTO_PRESSURE_HYST_K

    def offset(self, sample_temperature_k: float) -> float:
        return (
            self.low_offset_k
            if sample_temperature_k < self.switch_temperature_k
            else self.high_offset_k
        )

    def pressure_for_setpoint(self, sample_temperature_k: float) -> float:
        return (
            self.low_pressure_mbar
            if sample_temperature_k < self.switch_temperature_k
            else self.high_pressure_mbar
        )

    def pressure_regime(self, actual_sample_temperature_k: float) -> Optional[str]:
        low_edge = self.switch_temperature_k - self.hysteresis_k
        high_edge = self.switch_temperature_k + self.hysteresis_k

        if actual_sample_temperature_k < low_edge:
            return "low"
        if actual_sample_temperature_k > high_edge:
            return "high"
        return None


@dataclass(frozen=True)
class TeslatronReadings:
    vti_temperature_k: float
    probe_temperature_k: float
    pressure_mbar: float
    field_t: float
    target_field_t: float
    field_ramp_rate_t_per_min: float


@dataclass(frozen=True)
class TemperatureRampConfig:
    rate_k_per_min: float = DEFAULT_TEMP_RAMP_K_PER_MIN
    enabled: bool = False


# ---------------------------------------------------------------------------
# High-level TeslatronPT API
# ---------------------------------------------------------------------------

class TeslatronPT:
    """
    High-level API matching the latest TeslatronPT GUI.

    The class deliberately exposes the three Mercury controllers separately,
    because they are physically independent instruments.
    """

    def __init__(
        self,
        itc_vti_host: str = ITC_VTI_HOST,
        itc_probe_host: str = ITC_PROBE_HOST,
        ips_host: str = IPS_HOST,
        port: int = MERCURY_PORT,
        timeout: float = 5.0,
        thermal_rule: Optional[ThermalRule] = None,
        connect_vti: bool = True,
        connect_probe: bool = True,
        connect_ips: bool = True,
    ):
        """
        connect_vti / connect_probe / connect_ips allow connecting only a
        subset of the three controllers (e.g. magnet-only scripts). Disabled
        controllers are left as None; any method that needs them raises
        TeslatronControlError.
        """
        self.itc_vti: Optional[MercuryController] = None
        self.itc_probe: Optional[MercuryController] = None
        self.ips: Optional[MercuryController] = None
        try:
            if connect_vti:
                self.itc_vti = MercuryController(itc_vti_host, port, timeout)
            if connect_probe:
                self.itc_probe = MercuryController(itc_probe_host, port, timeout)
            if connect_ips:
                self.ips = MercuryController(ips_host, port, timeout)
        except Exception:
            self.close()
            raise

        self.thermal_rule = thermal_rule or ThermalRule()

    @staticmethod
    def _require(controller: Optional[MercuryController], name: str) -> MercuryController:
        if controller is None:
            raise TeslatronControlError(
                f"{name} is not connected (disabled via connect_* flag)."
            )
        return controller

    def close(self) -> None:
        for ctl in (self.itc_vti, self.itc_probe, self.ips):
            if ctl is not None:
                ctl.close()

    def reconnect_disconnected(self) -> None:
        """Reopen dead sockets without replaying any instrument command."""
        for ctl in (self.itc_vti,self.itc_probe,self.ips):
            if ctl is not None and ctl._sock is None:
                ctl.connect()

    def disable_reconnect(self) -> None:
        """Prevent every connected controller from auto-reconnecting.

        Call BEFORE close() during orderly shutdown: any query still in
        flight after the dispatcher join will fail fast instead of
        resurrecting the connection and re-sending a command.
        """
        for ctl in (self.itc_vti, self.itc_probe, self.ips):
            if ctl is not None:
                ctl.set_reconnect(False)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ------------------------------------------------------------------
    # Controller identification
    # ------------------------------------------------------------------

    def idn_vti(self) -> str:
        return self._require(self.itc_vti, "iTC-VTI").idn()

    def idn_probe(self) -> str:
        return self._require(self.itc_probe, "iTC-Probe").idn()

    def idn_ips(self) -> str:
        return self._require(self.ips, "iPS").idn()

    def identify_all(self) -> dict[str, str]:
        return {
            "iTC-VTI": self.idn_vti(),
            "iTC-Probe": self.idn_probe(),
            "iPS": self.idn_ips(),
        }

    # ------------------------------------------------------------------
    # Live readings
    # ------------------------------------------------------------------

    def vti_temperature(self) -> float:
        return self._require(self.itc_vti, "iTC-VTI").read_signal(
            VTI_MODULE, "T1", "TEMP", "TEMP")

    def probe_temperature(self) -> float:
        return self._require(self.itc_probe, "iTC-Probe").read_signal(
            PROBE_MODULE, "T1", "TEMP", "TEMP")

    def pressure(self) -> float:
        return self._require(self.itc_vti, "iTC-VTI").read_signal(
            PRESSURE_MODULE, "P1", "PRES", "PRES")

    def field(self) -> float:
        return self._require(self.ips, "iPS").read_group_signal(
            MAGNET_GROUP, "PSU", "FLD")

    def target_field(self) -> float:
        return self._require(self.ips, "iPS").read_group_signal(
            MAGNET_GROUP, "PSU", "FSET")

    def field_ramp_rate(self) -> float:
        return self._require(self.ips, "iPS").read_group_signal(
            MAGNET_GROUP, "PSU", "RFST")

    def magnet_action(self) -> str:
        """Read the magnet group ACTN state (HOLD/RTOS/RTOZ/CLMP)."""
        return self._require(self.ips, "iPS").read_group_status(
            MAGNET_GROUP, "PSU", "ACTN")

    def readings(self, *, probe_temperature_k=None, allow_partial=False) -> TeslatronReadings:
        def read_numeric(fn):
            try:return fn()
            except MercuryInvalidResponseError:
                if not allow_partial:raise
                return float('nan')
        return TeslatronReadings(
            vti_temperature_k=read_numeric(self.vti_temperature),
            probe_temperature_k=read_numeric(self.probe_temperature) if probe_temperature_k is None else probe_temperature_k,
            pressure_mbar=read_numeric(self.pressure),
            field_t=read_numeric(self.field),
            target_field_t=read_numeric(self.target_field),
            field_ramp_rate_t_per_min=read_numeric(self.field_ramp_rate),
        )

    # ------------------------------------------------------------------
    # Temperature control
    # ------------------------------------------------------------------

    def set_probe_temperature(self, kelvin: float) -> str:
        self._validate_temperature(kelvin)
        return self._require(self.itc_probe, "iTC-Probe").set_signal(
            PROBE_MODULE,
            "T1",
            "TEMP",
            "LOOP",
            "TSET",
            f"{kelvin:.8g}",
        )

    def set_vti_temperature(self, kelvin: float) -> str:
        self._validate_temperature(kelvin)
        return self._require(self.itc_vti, "iTC-VTI").set_signal(
            VTI_MODULE,
            "T1",
            "TEMP",
            "LOOP",
            "TSET",
            f"{kelvin:.8g}",
        )

    def set_sample_temperature(self, kelvin: float) -> tuple[str, str]:
        """
        Set the sample temperature with permanent VTI/probe linkage.

        Probe:
            TSET = T

        VTI:
            TSET = T - empirical offset
        """
        self._validate_temperature(kelvin)

        offset = self.thermal_rule.offset(kelvin)
        vti_target = round(kelvin - offset, 10)
        # Validate BOTH setpoints before touching any hardware: previously a
        # rejection of the VTI target (e.g. T so low that T-offset <= 0) left
        # the probe setpoint already applied, silently breaking the linkage.
        self._validate_temperature(vti_target)

        # Keep the two controllers physically synchronized at the API level.
        # The two writes cannot be atomic (two independent TCP devices); if
        # the VTI write fails after the probe write succeeded, say so
        # explicitly — a silent half-applied linkage is the worst outcome.
        probe_reply = self.set_probe_temperature(kelvin)
        try:
            vti_reply = self.set_vti_temperature(vti_target)
        except Exception as exc:
            raise TeslatronControlError(
                f"PARTIAL FAILURE: probe TSET {kelvin:g} K applied, "
                f"but VTI TSET {vti_target:g} K failed: {exc}"
            ) from exc
        return probe_reply, vti_reply

    def temperature_offset_for(self, sample_temperature_k: float) -> float:
        return self.thermal_rule.offset(sample_temperature_k)

    def expected_vti_temperature(self, sample_temperature_k: float) -> float:
        return sample_temperature_k - self.thermal_rule.offset(
            sample_temperature_k
        )

    # ------------------------------------------------------------------
    # Temperature ramp — deliberately unified for the two loops
    # ------------------------------------------------------------------

    def set_temperature_ramp_rate(self, k_per_min: float, link_vti: bool = True) -> tuple[str, str]:
        if not 0 < k_per_min <= MAX_TEMP_RAMP_K_PER_MIN:
            raise ValueError(
                f"Temperature ramp rate must be >0 and <= "
                f"{MAX_TEMP_RAMP_K_PER_MIN:g} K/min."
            )

        probe_reply = self._require(self.itc_probe, "iTC-Probe").set_signal(
            PROBE_MODULE,
            "T1",
            "TEMP",
            "LOOP",
            "RSET",
            f"{k_per_min:.6g}",
        )
        if not link_vti:
            return probe_reply, ""
        vti_reply = self._require(self.itc_vti, "iTC-VTI").set_signal(
            VTI_MODULE,
            "T1",
            "TEMP",
            "LOOP",
            "RSET",
            f"{k_per_min:.6g}",
        )
        return probe_reply, vti_reply

    def set_temperature_ramp_enabled(self, enabled: bool, link_vti: bool = True) -> tuple[str, str]:
        value = "ON" if enabled else "OFF"

        probe_reply = self._require(self.itc_probe, "iTC-Probe").set_signal(
            PROBE_MODULE,
            "T1",
            "TEMP",
            "LOOP",
            "RENA",
            value,
        )
        if not link_vti:
            return probe_reply, ""
        vti_reply = self._require(self.itc_vti, "iTC-VTI").set_signal(
            VTI_MODULE,
            "T1",
            "TEMP",
            "LOOP",
            "RENA",
            value,
        )
        return probe_reply, vti_reply

    def configure_temperature_ramp(
        self,
        rate_k_per_min: float = DEFAULT_TEMP_RAMP_K_PER_MIN,
        enabled: bool = False,
        link_vti: bool = True,
    ) -> TemperatureRampConfig:
        self.set_temperature_ramp_rate(rate_k_per_min, link_vti=link_vti)
        self.set_temperature_ramp_enabled(enabled, link_vti=link_vti)
        return TemperatureRampConfig(rate_k_per_min, enabled)

    # ------------------------------------------------------------------
    # Exchange-gas pressure
    # ------------------------------------------------------------------

    def set_pressure(self, mbar: float) -> str:
        if not 0 <= mbar <= 2000:
            raise ValueError("Pressure must be 0..2000 mbar.")
        # NOTE: on this Mercury iTC the exchange-gas needle-valve control loop
        # is the *TEMP* loop on DB3.P1. DB3.P1:PRES:SIG:PRES is only the measured
        # read-back; the setpoint must be written to the TEMP loop (PRES:LOOP
        # does not exist as a control loop, so writing it is silently ignored).
        return self._require(self.itc_vti, "iTC-VTI").set_signal(
            PRESSURE_MODULE,
            "P1",
            "TEMP",
            "LOOP",
            "TSET",
            f"{mbar:.8g}",
        )

    def automatic_pressure_target(self, actual_probe_temperature_k: float) -> Optional[float]:
        regime = self.thermal_rule.pressure_regime(actual_probe_temperature_k)
        if regime == "low":
            return self.thermal_rule.low_pressure_mbar
        if regime == "high":
            return self.thermal_rule.high_pressure_mbar
        return None

    # ------------------------------------------------------------------
    # Linked thermal-coupling automation
    # ------------------------------------------------------------------

    def enforce_thermal_link(
        self,
        actual_probe_temperature_k: float,
        probe_target_temperature_k: Optional[float] = None,
        auto_pressure: bool = True,
        previous_regime: Optional[str] = None,
    ) -> tuple[Optional[str], list[str]]:
        """
        Evaluate the automatic thermal-coupling state.

        Pressure hysteresis uses ACTUAL probe temperature. VTI offset always
        uses the requested probe target, matching set_sample_temperature().

        Returns:
            (new_regime, actions)

        If the temperature remains inside the hysteresis dead band or in the
        same regime, no hardware command is issued.

        The linked VTI temperature control is maintained when the regime
        changes, independently of whether auto-pressure is enabled.
        """
        regime = self.thermal_rule.pressure_regime(actual_probe_temperature_k)

        if regime is None or regime == previous_regime:
            return previous_regime, []

        if probe_target_temperature_k is None:
            probe_target_temperature_k = actual_probe_temperature_k

        self._validate_temperature(probe_target_temperature_k)

        offset = self.thermal_rule.offset(probe_target_temperature_k)
        vti_target = round(probe_target_temperature_k - offset, 10)
        self._validate_temperature(vti_target)

        actions: list[str] = []

        if auto_pressure:
            pressure = (
                self.thermal_rule.low_pressure_mbar
                if regime == "low"
                else self.thermal_rule.high_pressure_mbar
            )
            self.set_pressure(pressure)
            actions.append(f"pressure={pressure:g} mbar")

        # Temperature linkage is always maintained, independent of auto-pressure.
        self.set_vti_temperature(vti_target)
        actions.append(f"VTI TSET={vti_target:g} K")

        return regime, actions

    # ------------------------------------------------------------------
    # Magnet
    # ------------------------------------------------------------------

    def set_current_ramp_rate(self, a_per_min: float) -> str:
        if a_per_min <= 0:
            raise ValueError("Current ramp rate must be >0 A/min.")
        return self._require(self.ips, "iPS").set_group_signal(
            MAGNET_GROUP,
            "PSU",
            "SIG",
            "RCST",
            f"{a_per_min:.10g}",
        )

    def set_field_ramp_rate(self, t_per_min: float) -> str:
        if not 0 < t_per_min <= MAX_FIELD_RAMP_T_PER_MIN:
            raise ValueError(
                f"Field ramp rate must be >0 and <= "
                f"{MAX_FIELD_RAMP_T_PER_MIN:g} T/min."
            )
        return self._require(self.ips, "iPS").set_group_signal(
            MAGNET_GROUP,
            "PSU",
            "SIG",
            "RFST",
            f"{t_per_min:.10g}",
        )

    def set_field(self, tesla: float, ramp_rate_t_per_min: Optional[float] = None):
        """Compatibility wrapper: delegates to the verified field command.

        Previously this was an UNVERIFIED raw sequence (FSET write + RTOS
        with no read-back and no confirmation) - the "command accepted but
        never executed" failure class this project fixed (see
        ``set_field_verified``).  All ramp paths now go through the verified
        chain; this method exists so older scripts keep working with the
        same (verified) semantics.

        Returns the ``set_field_verified`` result dict (previously a list of
        raw reply strings was returned; the dict supersedes it).
        """
        if not -MAX_FIELD_T <= tesla <= MAX_FIELD_T:
            raise ValueError(
                f"Field must be between -{MAX_FIELD_T:g} and +{MAX_FIELD_T:g} T "
                f"(software ceiling MAX_FIELD_T; set it to the installed "
                f"magnet's rated maximum)."
            )
        return self.set_field_verified(
            tesla, ramp_rate_t_per_min=ramp_rate_t_per_min)

    def set_field_verified(
        self,
        tesla: float,
        ramp_rate_t_per_min: Optional[float] = None,
        *,
        readback_retries: int = 3,
        arrival_tolerance_t: float = 0.0002,
        verify_settle_s: float = 1.0,
        retry_interval_s: float = 60.0,
        wait_for_front_panel: bool = False,
        wait: Optional[Callable[[float], None]] = None,
        unclamp_settle_s: float = 2.0,
        log: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """Verified field command: write -> wait -> read back -> limited retry.

        Implements the officially documented iPS behaviour only (no hardware
        guessing):

        1. If ACTN == CLMP: send HOLD first and wait.  The handbook states a
           clamped output must be unclamped ("Hold") before ramping - the
           crow-bar relays physically open at this point.  RTOS from CLMP
           does not start a ramp (same handling as the QCoDeS MercuryiPS
           driver).
        2. SET SIG:RFST <rate> - an explicit non-zero rate every time.  When
           ramp_rate_t_per_min is None the magnet's residual RFST is read
           and validated; an unusable (zero/unreadable) value falls back to
           DEFAULT_FIELD_RAMP_T_PER_MIN instead of being reused.
        3. SET SIG:FSET <target>, wait, READ SIG:FSET back and compare.
           Official basis: "the control program must assume that data can
           be occasionally corrupted" (handbook ch.7) and the SIG SWHT
           entry is annotated "check before set" (ch.11).  Retry a limited
           number of times, then raise.
        4. SET ACTN:RTOS, wait, READ ACTN.  Confirmed started when ACTN
           reads RTOS ("To Set").  If the field is already inside the
           requested measurement tolerance, HOLD/CLMP can count as arrival.
           A broader historical epsilon heuristic only labels a no-op; the
           caller MUST check arrival_verified before accepting the result.

        Raises TeslatronControlError when a step cannot be verified after
        `readback_retries` attempts. Failed attempts wait `retry_interval_s`
        (default 60 s); successful command readback still waits 1 s by default.
        With `wait_for_front_panel`, exhausted FSET/RTOS verification returns
        `field_wait_reason` instead of raising; the caller MUST wait for actual
        field arrival and stable HOLD/CLMP before any measurement. No further
        magnet commands are sent in that waiting phase.
        `wait` may be supplied for interruptible waits. RFST is set but only logged (the rate
        limit tables may coerce it; the handbook says a too-fast requested
        rate is clamped, not rejected).
        """

        # Callback permits cancellation during cooldown; do not catch its errors.
        _wait = wait if wait is not None else time.sleep

        def _log(message: str) -> None:
            if log is not None:
                try:
                    log(message)
                except Exception:
                    pass

        if not -MAX_FIELD_T <= tesla <= MAX_FIELD_T:
            raise ValueError(
                f"Field must be between -{MAX_FIELD_T:g} and +{MAX_FIELD_T:g} T."
            )

        if not math.isfinite(arrival_tolerance_t) or arrival_tolerance_t <= 0:
            raise ValueError("arrival_tolerance_t must be finite and positive")
        ips = self._require(self.ips, "iPS")
        result: dict = {
            "arrival_tolerance_t": arrival_tolerance_t,
            "target_t": tesla,
            "ramp_rate_t_per_min": ramp_rate_t_per_min,
            "initial_action": None,
            "unclamped": False,
            "fset_readback_t": None,
            "fset_attempts": 0,
            "rtos_attempts": 0,
        }

        # ---- step 1: pre-check the activity state ------------------------
        try:
            action = self.magnet_action()
        except (MercuryCommunicationError, MercuryInvalidResponseError) as exc:
            # A failed pre-check must not block the command itself; the
            # RTOS verification below is the real gate.
            _log(f"iPS ACTN pre-check failed ({exc}); continuing.")
            action = "UNKNOWN"
        result["initial_action"] = action

        if normalize_action(action) == "CLMP":
            _log("iPS is CLMPed; sending HOLD to unclamp before ramping.")
            self.hold()
            _wait(unclamp_settle_s)
            result["unclamped"] = True

        # ---- step 2: explicit non-zero ramp rate --------------------------
        if ramp_rate_t_per_min is None:
            # Never reuse the magnet's residual RFST blindly: a stale zero
            # RFST is the known root cause of "FSET accepted but the field
            # never ramps". Read it, validate it, fall back to the default.
            try:
                residual = self.field_ramp_rate()
            except (MercuryCommunicationError, MercuryInvalidResponseError):
                residual = None
            if residual is not None and 0 < residual <= MAX_FIELD_RAMP_T_PER_MIN:
                ramp_rate_t_per_min = residual
            else:
                _log(f"iPS residual RFST unusable ({residual!r}); falling "
                     f"back to {DEFAULT_FIELD_RAMP_T_PER_MIN:g} T/min.")
                ramp_rate_t_per_min = DEFAULT_FIELD_RAMP_T_PER_MIN
            result["ramp_rate_t_per_min"] = ramp_rate_t_per_min
        self.set_field_ramp_rate(ramp_rate_t_per_min)
        try:
            rfst = self.field_ramp_rate()
            _log(f"iPS RFST readback = {rfst:g} T/min "
                 f"(requested {ramp_rate_t_per_min:g}).")
        except (MercuryCommunicationError, MercuryInvalidResponseError):
            _log("iPS RFST readback unavailable; continuing.")

        # ---- step 3: FSET write -> wait -> read back ----------------------
        fset_rb = None
        last_exc: Optional[Exception] = None
        for attempt in range(1, max(1, readback_retries) + 1):
            result["fset_attempts"] = attempt
            try:
                ips.set_group_signal(
                    MAGNET_GROUP, "PSU", "SIG", "FSET", f"{tesla:.10g}")
                _wait(verify_settle_s)
                fset_rb = self.target_field()
                result["fset_readback_t"] = fset_rb
                if abs(fset_rb - tesla) <= FSET_READBACK_TOL_T:
                    if attempt > 1:
                        _log(f"FSET verified on attempt {attempt}: "
                             f"{fset_rb:.6f} T.")
                    break
                last_exc = ValueError(
                    f"FSET readback {fset_rb:.6f} T != target {tesla:.6f} T")
            except (MercuryCommunicationError,
                    MercuryInvalidResponseError) as exc:
                last_exc = exc
            _log(f"FSET not verified (attempt {attempt}/"
                 f"{readback_retries}): {last_exc}.")
            if attempt < max(1, readback_retries):
                _log(f"等待 {retry_interval_s:g} 秒后重新设置磁场命令 "
                     f"({attempt + 1}/{readback_retries}).")
                _wait(retry_interval_s)
        else:
            if wait_for_front_panel:
                result["field_wait_reason"] = f"FSET 设置未确认：{last_exc}"
                return result
            raise TeslatronControlError(
                f"iPS FSET never verified after {readback_retries} attempts "
                f"(target {tesla:g} T): {last_exc}")

        # ---- step 4: RTOS -> confirm the ramp actually started ------------
        last_exc = None
        for attempt in range(1, max(1, readback_retries) + 1):
            result["rtos_attempts"] = attempt
            try:
                ips.set_action(MAGNET_GROUP, "RTOS")
                _wait(verify_settle_s)
                action = self.magnet_action()
                # Canonical token RTOS (handbook command table) or the
                # front-panel label "TO SET" - both mean "ramp running".
                action_u = normalize_action(action)
                result["action_readback_raw"] = action
                result["action_readback"] = action_u
                if action_u == "RTOS":
                    result["started"] = True
                    return result
                # A stopped output is NOT proof of arrival. Preserve both the
                # observed field and the requested measurement tolerance.
                actual = self.field()
                result['field_readback_t'] = actual
                if action_u in ('HOLD','CLMP') and abs(actual-tesla) <= max(EPSILON_ARRIVAL_TOL_T,arrival_tolerance_t):
                    result['started'] = False
                    result['no_op_ramp'] = True
                    result['arrival_verified'] = abs(actual-tesla) <= arrival_tolerance_t
                    _log(f'No running ramp observed: field={actual:g} T, target={tesla:g} T, tolerance={arrival_tolerance_t:g} T, arrival_verified={result["arrival_verified"]}.')
                    return result
                last_exc = ValueError(
                    f"ACTN = {action} after RTOS (expected RTOS)")
            except (MercuryCommunicationError,
                    MercuryInvalidResponseError) as exc:
                last_exc = exc
            _log(f"RTOS start not confirmed (attempt {attempt}/"
                 f"{readback_retries}): {last_exc}.")
            if attempt < max(1, readback_retries):
                _log(f"等待 {retry_interval_s:g} 秒后重新设置磁场命令 "
                     f"({attempt + 1}/{readback_retries}).")
                _wait(retry_interval_s)

        if wait_for_front_panel:
            result["field_wait_reason"] = f"RTOS 励磁启动未确认：{last_exc}"
            return result
        raise TeslatronControlError(
            f"iPS RTOS never confirmed after {readback_retries} attempts "
            f"(target {tesla:g} T): {last_exc}")

    def hold(self) -> str:
        return self._require(self.ips, "iPS").set_action(MAGNET_GROUP, "HOLD")

    def ramp_zero(self) -> str:
        return self._require(self.ips, "iPS").set_action(MAGNET_GROUP, "RTOZ")

    def clamp(self) -> str:
        return self._require(self.ips, "iPS").set_action(MAGNET_GROUP, "CLMP")

    def pause(self) -> str:
        return self._require(self.ips, "iPS").set_action(MAGNET_GROUP, "PAUS")

    # ------------------------------------------------------------------
    # Safety / validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_temperature(kelvin: float) -> None:
        if not 0 < kelvin <= 300:
            raise ValueError("Temperature must be between 0 and 300 K.")


__all__ = [
    "ITC_VTI_HOST",
    "ITC_PROBE_HOST",
    "IPS_HOST",
    "MERCURY_PORT",
    "VTI_MODULE",
    "PROBE_MODULE",
    "PRESSURE_MODULE",
    "MAGNET_GROUP",
    "SAMPLE_T_SWITCH_K",
    "MAX_FIELD_RAMP_T_PER_MIN",
    "MAX_FIELD_T",
    "MAX_TEMP_RAMP_K_PER_MIN",
    "DEFAULT_TEMP_RAMP_K_PER_MIN",
    "OxfordMercuryError",
    "MercuryCommunicationError",
    "MercuryInvalidResponseError",
    "TeslatronControlError",
    "MercuryController",
    "ThermalRule",
    "TeslatronReadings",
    "TemperatureRampConfig",
    "TeslatronPT",
]


if __name__ == "__main__":
    print("Oxford TeslatronPT API")
    print(f"VTI iTC   : {ITC_VTI_HOST}:{MERCURY_PORT}")
    print(f"Probe iTC : {ITC_PROBE_HOST}:{MERCURY_PORT}")
    print(f"Magnet iPS: {IPS_HOST}:{MERCURY_PORT}")
    print(f"Magnet group: {MAGNET_GROUP}")
    print(
        f"Default thermal rule: "
        f"T<10K -> 4 mbar / ΔT=0.2K; "
        f"T>=10K -> 10 mbar / ΔT=1.0K"
    )
    print(
        f"Field ramp ceiling: {MAX_FIELD_RAMP_T_PER_MIN:g} T/min"
    )
