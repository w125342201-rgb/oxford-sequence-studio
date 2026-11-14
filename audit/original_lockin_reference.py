# -*- coding: utf-8 -*-
"""
PXI-4462 + 6221-AC digital lock-in measurement layer
====================================================

Hardware (official specs, 2026-09-04):
  * NI PXI-4462 (4x 24-bit AI, up to 204.8 kS/s, ranges +-0.316 .. +-42.4 V pk,
    differential/pseudo-differential, AC/DC coupling, IEPE excitation,
    front-panel PFI 0 digital-trigger input). In a PXI chassis (PXI-1036)
    remote-controlled over MXI-Express (PXI-8360); DAQmx sees it as
    ``PXI<chassis>Slot<k>``.
  * Keithley 6221 AC waveform: I(t) = I_AC*sin(2*pi*f*t), phase marker = one
    1 us pulse per cycle at a selectable phase, routed to Trigger-Link line
    ``PMAR:OLIN`` (default 3). Wire that line to the 4462 PFI 0 front BNC:
    the rising edge starts the finite acquisition, so the sampled window is
    phase-locked to the excitation.

Digital lock-in (this module):
  * sample fs (Hz) for a nominal 1 s -> the window holds f * 1 = integer
    number of excitation cycles for integer f, so EVERY integer-Hz
    interferer (50/100/150... Hz mains harmonics) lands exactly on its own
    FFT bin and does not leak into the f bin (rectangular window, no
    scalloping). Magnitude at bin k=f is exact.
  * R = V_peak / I_peak; X/Y and phase (relative to the window start =
    the marker edge) are reported too.

DAQmx absent / device offline: use simulate=True (pure numpy waveform +
noise) to exercise the full lock-in path - mirrors the MR matrix "simulate"
philosophy. Importing this module NEVER requires nidaqmx; only instantiating
Pxi4462Lockin(real) does.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, List, Any

import numpy as np

# 6221 phase-marker default output line on its rear trigger connector.
DEFAULT_MARKER_LINE = 1

try:                                   # optional hardware dependency
    import nidaqmx
    from nidaqmx.constants import (
        AcquisitionType, Coupling, Edge, TerminalConfiguration,
        VoltageUnits, TimeUnits, TriggerType)
    # nidaqmx 1.x names the coupling enum "Coupling" (InputCoupling is a
    # legacy alias absent from 1.6.0 - using it silently disabled the DAQ
    # branch). Alias for clarity at call sites:
    InputCoupling = Coupling
    _HAS_DAQMX = True
except Exception:                      # pragma: no cover
    nidaqmx = None
    _HAS_DAQMX = False


@dataclass
class LockinResult:
    """One digital lock-in reading."""

    resistance_ohm: float          # V_peak / I_peak
    v_peak_v: float                # fundamental amplitude at f
    x_v: float                     # in-phase component (0 deg ref = window start)
    y_v: float                     # quadrature component
    phase_rad: float               # atan2(Y, X) relative to window start
    channel: str = ""              # AI channel name this reading came from
    # second harmonic (2f) of the same window - nonlinearity probe:
    v2_peak_v: float = 0.0         # amplitude at 2f
    x2_v: float = 0.0              # in-phase component at 2f
    y2_v: float = 0.0              # quadrature component at 2f
    phase2_rad: float = 0.0        # phase at 2f vs the same window start
    ratio2: float = 0.0            # V2f / V1f (dimensionless nonlinearity)
    n_samples: int = 0
    fs_hz: float = 0.0
    freq_hz: float = 0.0
    i_peak_a: float = 0.0
    duration_s: float = 0.0

    def as_csv_row(self, i_peak_a: Optional[float] = None) -> List[str]:
        r = self.resistance_ohm
        return [
            "" if r is None or r != r else f"{r:.12g}",
            f"{self.v_peak_v:.12g}",
            f"{self.x_v:.12g}",
            f"{self.y_v:.12g}",
            f"{self.phase_rad:.9g}",
        ]


def _n_samples_integral_cycles(fs_hz: float, freq_hz: float,
                               duration_s: float = 1.0) -> int:
    """Return an integer sample count whose window holds an integer number
    of excitation cycles and whose duration is as close to ``duration_s`` as
    possible (for integer f and fs a multiple of f this equals fs*duration)."""
    n0 = max(8, int(round(fs_hz * duration_s)))
    cycles = max(1, int(round(n0 * freq_hz / fs_hz)))
    n = int(round(cycles * fs_hz / freq_hz))
    return n


def digital_lockin(data: np.ndarray, fs_hz: float, freq_hz: float,
                   i_peak_a: float, phase_ref_deg: float = 0.0,
                   window: str = "hann") -> LockinResult:
    """Single-bin DFT digital lock-in.

    window="hann" (default): suppresses mains-drift leakage by >40 dB
    relative to the rectangular window far sidelobes (50.0x Hz wander no
    longer leaks into the f bin); the coherent gain sum(w) is divided out,
    so amplitudes stay exact. window="rect" keeps the zero-leakage behaviour
    for perfectly integer-cycle windows. Data must hold an integral number
    of cycles at ``freq_hz`` either way.
    """
    data = np.asarray(data, dtype=float).reshape(-1)
    n = len(data)
    if n < 8:
        raise ValueError("digital_lockin needs >= 8 samples.")
    x = data - float(np.mean(data))            # remove DC offset
    k = int(round(freq_hz * n / fs_hz))
    if k < 1 or k >= n // 2:
        raise ValueError(
            f"freq {freq_hz:g} Hz does not fit the {n}-sample window "
            f"(fs {fs_hz:g}).")
    n_ar = np.arange(n)
    if str(window).lower() in ("hann", "hanning"):
        w = 0.5 - 0.5 * np.cos(2 * np.pi * n_ar / (n - 1))
    else:
        w = np.ones(n)
    cg = float(np.sum(w))                      # coherent gain
    # correlation with the bin complex exponential (Goertzel-style single bin)
    ph = np.exp(-2j * np.pi * k * n_ar / n)
    c = complex(np.sum(x * w * ph))          # complex amplitude (keeps imag)
    # V_peak of the sinusoid = 2*|c|/sum(w)
    amp = 2.0 * abs(c) / cg
    ang = math.atan2(c.imag, c.real)           # phase of cosine phasor in rad
    # window-start-relative phase of sin(2*pi*f*t + phi): phi = ang + pi/2
    phi = ang + math.pi / 2.0
    # reference phase (marker edge defines t=0; user may add a fixed offset)
    phi -= math.radians(phase_ref_deg)
    x_v = amp * math.cos(phi)
    y_v = amp * math.sin(phi)
    r = (amp / i_peak_a) if i_peak_a else float("nan")
    return LockinResult(
        resistance_ohm=r,
        v_peak_v=amp,
        x_v=x_v,
        y_v=y_v,
        phase_rad=phi,
        n_samples=n,
        fs_hz=fs_hz,
        freq_hz=freq_hz,
        i_peak_a=i_peak_a,
        duration_s=n / fs_hz,
    )


def synthesize(fs_hz: float, duration_s: float, freq_hz: float,
               v_peak_v: float, phase_deg: float = 0.0,
               mains_v: float = 0.0, noise_std_v: float = 0.0,
               rng=None) -> np.ndarray:
    """Simulate a 4462-style capture: signal + mains + white noise."""
    rng = rng if rng is not None else np.random.default_rng(0)
    n = max(8, int(round(fs_hz * duration_s)))
    t = np.arange(n) / fs_hz
    x = v_peak_v * np.sin(2 * np.pi * freq_hz * t
                          + math.radians(phase_deg))
    if mains_v:
        x += mains_v * np.sin(2 * np.pi * 50.0 * t)
    if noise_std_v:
        x += rng.normal(0.0, noise_std_v, n)
    return x


def discover_device() -> Optional[str]:
    """Name of the first DAQmx device whose product type contains '4462',
    or None (driver missing / chassis offline)."""
    if not _HAS_DAQMX:
        return None
    try:
        from nidaqmx.system import System
        for dev in System.local().devices:
            if "4462" in (dev.product_type or ""):
                return dev.name
    except Exception:
        pass
    return None


class Pxi4462Lockin:
    """Finite AI acquisition from a PXI-4462, started by the 6221 phase
    marker on PFI 0, reduced with the digital lock-in above.

    With ``simulate=True`` no hardware is touched: acquisitions come from
    :func:`synthesize` and the full lock-in path still runs.
    """

    def __init__(self, freq_hz: float = 13.0, i_peak_a: float = 10e-6,
                 fs_hz: float = 51200.0, duration_s: float = 1.0,
                 channel: Optional[str] = None,
                 channels: Optional[list] = None,
                 terminal: str = "diff", coupling: str = "dc",
                 voltage_range_v: float = 1.0,
                 trigger_terminal: str = "PFI0",
                 trigger_edge: str = "falling",
                 simulate: bool = False,
                 v_sim_peak_v: float = 30e-6,
                 noise_sim_v: float = 5e-6,
                 sim_r_ohm: Optional[list] = None,
                 sim_v2_ratio: Optional[list] = None,
                 log=None):
        """channel = single channel name OR device name ('' -> auto-discover);
        channels = explicit AI index list, e.g. [0,1,2,3] (default ai0..ai3);
        sim_r_ohm = per-channel resistance at f used by the simulate path;
        sim_v2_ratio = per-channel V2f/V1f injected by the simulate path."""
        self.freq_hz = float(freq_hz)
        self.i_peak_a = float(i_peak_a)
        self.fs_hz = float(fs_hz)
        self.duration_s = float(duration_s)
        self.channel = channel
        self.channels = [int(c) for c in (channels if channels else [0, 1, 2, 3])]
        self.terminal = terminal.lower()
        self.coupling = coupling.lower()
        # PXI-4462 has six software-selectable ranges (no native autorange in
        # NI docs). "auto" = probe the signal peak with a 0.2 s software-
        # triggered pre-scan at +/-10 V, then pick the smallest non-saturating
        # range for the real acquisition (software autorange).
        self.range_spec = str(voltage_range_v)
        self.voltage_range_v = self.range_spec
        self._sel_range_v = None
        self.trigger_terminal = trigger_terminal
        # 622x trigger-link pulses are ACTIVE-LOW (its input spec is a
        # falling-edge TTL pulse; the marker output shares that logic), so
        # the 4462 must start on the FALLING edge - verified on hardware
        # 2026-09-04 (RISING never fired).
        self.trigger_edge = (Edge.RISING if str(trigger_edge).lower()
                             in ("rising", "rise")
                             else Edge.FALLING)
        self.simulate = bool(simulate)
        self.v_sim_peak_v = float(v_sim_peak_v)
        self.noise_sim_v = float(noise_sim_v)
        self.sim_r_ohm = (sim_r_ohm if sim_r_ohm is not None
                          else [3.0 + float(i) for i in range(len(self.channels))])
        if len(self.sim_r_ohm) != len(self.channels):
            raise ValueError("sim_r_ohm must have one entry per channel.")
        self.sim_v2_ratio = (sim_v2_ratio if sim_v2_ratio is not None
                             else [0.02 + 0.01 * i
                                   for i in range(len(self.channels))])
        if len(self.sim_v2_ratio) != len(self.channels):
            raise ValueError("sim_v2_ratio must have one entry per channel.")
        self.log = log or (lambda m: None)
        self.device: Optional[str] = None
        self._n = 0
        self._chan_names: list = []      # "PXI1Slot2/ai0" style per channel

        if not self.simulate:
            if not _HAS_DAQMX:
                raise RuntimeError(
                    "nidaqmx is not installed in this Python. Install it or "
                    "tick 'DAQ simulate'.")
            dev = self.channel or discover_device()
            if not dev:
                raise RuntimeError(
                    "No PXI-4462 found through NI-DAQmx. Power on the "
                    "PXI-1036 chassis (PXI-8360 link) and check MAX, or tick "
                    "'DAQ simulate'.")
            if "/" in dev:               # user gave a full channel name
                base = dev.rsplit("/", 1)[0]
            else:
                base = dev
            self.device = base
            self._chan_names = [f"{base}/ai{c}" for c in self.channels]
            self._n = _n_samples_integral_cycles(
                self.fs_hz, self.freq_hz, self.duration_s)
        else:
            self.device = "SIM"
            self._chan_names = [f"SIM/ai{c}" for c in self.channels]
            self._n = _n_samples_integral_cycles(
                self.fs_hz, self.freq_hz, self.duration_s)
            self.log("DAQ simulate: synthetic 4462 acquisition "
                     f"(fs={self.fs_hz:g}, N={self._n}, ch="
                     + ",".join(str(c) for c in self.channels)
                     + f", R={self.sim_r_ohm}, noise={self.noise_sim_v:g} V rms)")

    # ------------------------------------------------------------------
    def acquire_one(self, v_expected_peak_v: Optional[float] = None,
                    rng=None, window: str = "hann",
                    phase_ref_deg: float = 0.0) -> "dict[str, LockinResult]":
        """Start ONE marker-triggered finite acquisition sampled on every
        configured AI channel simultaneously, and return one lock-in reading
        per channel: ``{chan_name: LockinResult}`` (ai0 is the first entry).

        Simulate mode synthesises each channel at R_ch * I_AC (see
        ``sim_r_ohm``) plus mains + white noise, so the whole multi-channel
        lock-in path runs without hardware."""
        if self.simulate:
            data = np.empty((len(self.channels), self._n))
            for i, ch in enumerate(self.channels):
                vp = self.sim_r_ohm[i] * self.i_peak_a
                t = np.arange(self._n) / self.fs_hz
                sig = vp * np.sin(2 * np.pi * self.freq_hz * t)
                v2 = self.sim_v2_ratio[i] * vp
                sig = sig + v2 * np.sin(
                    2 * np.pi * (2 * self.freq_hz) * t + math.radians(30.0))
                sig = sig + vp * 0.5 * np.sin(2 * np.pi * 50.0 * t)
                if self.noise_sim_v:
                    sig = sig + np.random.default_rng(
                        0 if rng is None else None).normal(
                        0.0, self.noise_sim_v, self._n)
                data[i] = sig
        else:
            data = self._acquire_hardware()
        out = {}
        for i, name in enumerate(self._chan_names):
            r1 = digital_lockin(data[i], self.fs_hz, self.freq_hz,
                                self.i_peak_a, window=window,
                                phase_ref_deg=phase_ref_deg)
            r2 = digital_lockin(data[i], self.fs_hz, 2.0 * self.freq_hz,
                                self.i_peak_a, window=window)
            r1.channel = name
            r1.v2_peak_v = r2.v_peak_v
            r1.x2_v = r2.x_v
            r1.y2_v = r2.y_v
            r1.phase2_rad = r2.phase_rad
            r1.ratio2 = (r2.v_peak_v / r1.v_peak_v
                         if r1.v_peak_v > 0 else 0.0)
            out[name] = r1
        return out

    # ------------------------------------------------------------------
    def _effective_range_v(self) -> float:
        """Fixed range (numeric spec) or software-autorange result."""
        spec = str(self.range_spec)
        if spec.lower() != "auto":
            return float(spec)
        if self._sel_range_v is None:
            self._sel_range_v = self._probe_range_v()
            self.log(f"auto-range: selected +-{self._sel_range_v:g} V pk")
        return self._sel_range_v

    def _probe_range_v(self) -> float:
        """0.25 s software-triggered pre-scan at +-10 V; pick the smallest
        range that fits the peak with 10% headroom."""
        n = int(round(self.fs_hz * 0.25))
        term_conf = (TerminalConfiguration.DIFF
                     if str(self.terminal).lower() in ("diff", "differential")
                     else TerminalConfiguration.PSEUDO_DIFF)
        with nidaqmx.Task() as task:
            for name in self._chan_names:
                task.ai_channels.add_ai_voltage_chan(
                    name, min_val=-10.0, max_val=10.0,
                    units=VoltageUnits.VOLTS, terminal_config=term_conf)
            for ch in task.ai_channels:
                try:
                    ch.ai_coupling = (Coupling.AC if self.coupling == "ac"
                                      else Coupling.DC)
                except Exception:
                    pass
                try:
                    ch.iepe_excit_enable = False
                except Exception:
                    pass
            task.timing.cfg_samp_clk_timing(
                self.fs_hz, sample_mode=AcquisitionType.FINITE,
                samps_per_chan=n)
            data = np.asarray(task.read(number_of_samples_per_channel=n,
                                        timeout=3.0))
        peak = float(np.max(np.abs(data))) if data.size else 0.0
        for rng in (0.316, 1.0, 3.16, 10.0):
            if peak <= 0.9 * rng:
                return rng
        return 10.0

    def _acquire_hardware(self) -> np.ndarray:
        """Real DAQmx finite acquisition with PFI start trigger.

        TerminalConfig.RSE treats AI- as local ground reference; for a 4-wire
        sample voltage use 'diff' (AI+/AI- on adjacent BNCs) - set in the GUI.
        """
        base = self.device
        # PXI-4462 supports ONLY differential / pseudodifferential (RSE is
        # rejected by the hardware - verified 2026-09-04). Pseudodifferential
        # = single-ended signal on AI+ referenced to the channel's AI- (a
        # grounded return is fine); differential = true AI+/AI- pair.
        # nidaqmx 1.6.0 enum names: DIFF / PSEUDO_DIFF / NRSE / RSE.
        if self.terminal in ("diff", "differential"):
            term_conf = TerminalConfiguration.DIFF
        elif self.terminal in ("pseudo", "pdiff", "pseudo-diff",
                               "pseudodifferential", "rse"):
            if self.terminal == "rse":
                self.log("Warning: PXI-4462 has no RSE mode; using "
                         "pseudodifferential (AI- to ground).")
            term_conf = TerminalConfiguration.PSEUDO_DIFF
        else:
            raise ValueError(f"Unsupported 4462 terminal mode: "
                             f"{self.terminal!r} (use diff or pseudo-diff).")
        rng_v = self._effective_range_v()
        with nidaqmx.Task() as task:
            # One channel per add() call (multi-channel comma strings are
            # rejected by create_ai_voltage_chan; verified on hardware
            # 2026-09-04, nidaqmx 1.6.0).
            for name in self._chan_names:
                task.ai_channels.add_ai_voltage_chan(
                    name,
                    min_val=-rng_v,
                    max_val=rng_v,
                    units=VoltageUnits.VOLTS,
                    terminal_config=term_conf,
                )
            # Per-channel coupling + IEPE OFF (4462 properties; set via the
            # channel attribute, not the add() kwargs).
            for ch in task.ai_channels:
                try:
                    ch.ai_coupling = (Coupling.AC if self.coupling == "ac"
                                      else Coupling.DC)
                except Exception:
                    pass
                try:
                    ch.iepe_excit_enable = False
                except Exception:
                    pass
            task.timing.cfg_samp_clk_timing(
                self.fs_hz,
                sample_mode=AcquisitionType.FINITE,
                samps_per_chan=self._n,
            )
            # 6221 phase-marker edge starts the acquisition - unless
            # trigger_terminal is None/"off", in which case the task starts
            # on SOFTWARE (amplitude-only lock-in; no phase reference).
            if (self.trigger_terminal and str(self.trigger_terminal).lower()
                    not in ("none", "off", "")):
                src = f"/{base}/{self.trigger_terminal}"
                try:
                    task.triggers.start_trigger.cfg_dig_edge_start_trig(
                        src, self.trigger_edge)
                except Exception:
                    task.triggers.start_trigger.trig_type = (
                        TriggerType.DIGITAL_EDGE)
                    task.triggers.start_trigger.dig_edge_src = src
                    task.triggers.start_trigger.dig_edge_edge = self.trigger_edge
            data = task.read(number_of_samples_per_channel=self._n,
                             timeout=float(self.duration_s) * 3.0 + 5.0)
        # DAQmx returns shape (n_ch, n) for multi-channel finite reads.
        return np.asarray(data, dtype=float).reshape(len(self.channels), self._n)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Quick self-test of the pure lock-in math against a synthetic signal.
    fs, f, T = 51200.0, 13.0, 1.0
    vp_true = 30e-6
    res = digital_lockin(synthesize(fs, T, f, vp_true, phase_deg=37.0,
                                    mains_v=vp_true * 0.5,
                                    noise_std_v=5e-6),
                         fs, f, i_peak_a=10e-6)
    print(f"recovered V_peak={res.v_peak_v:.6e} (true {vp_true:.6e}) "
          f"R={res.resistance_ohm:.6f} ohm @10uA  phase={math.degrees(res.phase_rad):.2f} deg")
