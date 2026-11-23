# -*- coding: utf-8 -*-
"""
NI 4462 finite synchronous capture + exact-frequency joint digital demodulation.

1.7.0: Select 1f+2f or 1f+3f; Hann-weighted joint least squares.
3f mode fits a nuisance 2f term; unselected exported harmonics are None.
PFI marker starts each finite task, but does NOT lock NI and 6221 clocks.
ADC/filter/trigger phase delays are NOT automatically calibrated.
See AUDIT_4462_20260924.md for definitions, numerical tests and bench procedure.
No claim of SR830-equivalent precision or hardware validation is made here.
"""
from __future__ import annotations
import math
import time
from dataclasses import dataclass, field
from typing import Optional, List, Any

import numpy as np
if __package__:
    from .capture_limits import capture_sample_count
    from .input_ranges import DEFAULT_RANGE_V,MAX_INPUT_V,RANGES_V,fixed_range
else:  # Keep the original direct mathematical self-test entry point usable.
    from capture_limits import capture_sample_count
    from input_ranges import DEFAULT_RANGE_V,MAX_INPUT_V,RANGES_V,fixed_range

# This application's selected line; the 6221 factory default is line 3.
DEFAULT_MARKER_LINE = 1

try:                                   # optional hardware dependency
    import nidaqmx
    from nidaqmx.constants import (
        AcquisitionType, Coupling, Edge, TerminalConfiguration,
        VoltageUnits, TimeUnits, TriggerType, ExcitationSource)
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
    v2_peak_v: Optional[float] = 0.0  # None when 3f selected
    x2_v: Optional[float] = 0.0
    y2_v: Optional[float] = 0.0
    phase2_rad: Optional[float] = 0.0
    ratio2: Optional[float] = 0.0   # V2f / V1f
    harmonic_order: int = 2       # selected additional harmonic; 1f always retained
    v3_peak_v: Optional[float] = None
    x3_v: Optional[float] = None
    y3_v: Optional[float] = None
    phase3_rad: Optional[float] = None
    n_samples: int = 0
    fs_hz: float = 0.0
    freq_hz: float = 0.0
    i_peak_a: float = 0.0
    duration_s: float = 0.0
    estimator: str = "weighted_joint_sine_fit"
    dc_v: float = 0.0
    residual_rms_v: float = 0.0
    fit_condition: float = 0.0
    window_enbw_hz: float = 0.0  # two-sided window bandwidth, not SR830 ENBW

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
    return capture_sample_count(fs_hz, freq_hz, duration_s)


def digital_lockin(data, fs_hz, freq_hz, i_peak_a, phase_ref_deg=0.0, window="hann"):
    """Exact-frequency sine/DC fit. For simultaneous 1f/2f use fit_harmonics."""
    return fit_harmonics(data, fs_hz, [freq_hz], i_peak_a, [phase_ref_deg], window)[0]


def fit_harmonics(data, fs_hz, frequencies, i_peak_a, phases, window="hann"):
    """Joint DC + sin/cos weighted least squares (sum(w * residual**2)).
    X multiplies sin(wt+ref), Y multiplies cos(wt+ref); peak volts.
    No integer-bin rounding, mean subtraction, or guessed hardware delay.
    Frequencies must still match the physical source clock.
    """
    data = np.asarray(data, dtype=float).reshape(-1)
    n = len(data)
    if n < 8 or not np.all(np.isfinite(data)):
        raise ValueError("Lock-in requires >=8 finite samples")
    if not math.isfinite(fs_hz) or fs_hz <= 0 or len(phases) != len(frequencies):
        raise ValueError("Invalid sampling rate/reference phases")
    if any(not math.isfinite(f) or not 0 < f < fs_hz/2 for f in frequencies):
        raise ValueError("Reference frequencies must be between DC and Nyquist")
    if any(not math.isfinite(p) for p in phases):
        raise ValueError("Non-finite reference phase")
    t = np.arange(n) / fs_hz
    if str(window).lower() in ("hann", "hanning"):
        w = np.hanning(n)
    elif str(window).lower() == "rect":
        w = np.ones(n)
    else:
        raise ValueError("window must be hann or rect")
    columns = [np.ones(n)]
    for f, p in zip(frequencies, phases):
        angle = 2*np.pi*f*t + math.radians(p)
        columns.extend((np.sin(angle), np.cos(angle)))
    design = np.column_stack(columns)
    root_w = np.sqrt(w)
    coefficients, _, rank, singular = np.linalg.lstsq(
        design*root_w[:, None], data*root_w, rcond=None)
    condition = float(singular[0]/singular[-1])
    if rank != design.shape[1] or condition > 1e8:
        raise ValueError("Ill-conditioned harmonic fit; increase capture duration")
    residual = float(np.sqrt(np.mean((data-design@coefficients)**2)))
    enbw = float(fs_hz*np.dot(w,w)/np.sum(w)**2)
    out = []
    for j, f in enumerate(frequencies):
        x_v, y_v = map(float, coefficients[1+2*j:3+2*j])
        amp = math.hypot(x_v, y_v)
        out.append(LockinResult(
            resistance_ohm=amp/i_peak_a if i_peak_a else float("nan"),
            v_peak_v=amp, x_v=x_v, y_v=y_v, phase_rad=math.atan2(y_v,x_v),
            n_samples=n, fs_hz=fs_hz, freq_hz=f, i_peak_a=i_peak_a,
            duration_s=n/fs_hz, dc_v=float(coefficients[0]),
            residual_rms_v=residual, fit_condition=condition, window_enbw_hz=enbw))
    return out


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
                 voltage_range_v: float = DEFAULT_RANGE_V,
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
        # NI docs). Legacy "auto" is a one-time software prescan, not exposed
        # in Studio. A fixed range is held throughout a Studio run.
        self.range_spec = ('auto' if str(voltage_range_v).lower()=='auto'
                           else str(fixed_range(voltage_range_v)))
        self.voltage_range_v = self.range_spec
        self._sel_range_v = None
        self.trigger_terminal = trigger_terminal
        # Retain the falling edge selected by the supplied original program.
        # No new hardware validation was performed in this revision.
        self.trigger_edge = ((Edge.RISING if str(trigger_edge).lower()
                             in ("rising", "rise")
                             else Edge.FALLING) if _HAS_DAQMX else None)
        self.simulate = bool(simulate)
        self.v_sim_peak_v = float(v_sim_peak_v)
        self.noise_sim_v = float(noise_sim_v)
        self.sim_rng = np.random.default_rng()
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
        self.actual_fs_hz = self.fs_hz
        self.last_capture_metadata = {}
        self.cancel_check=None;self.wait_callback=None;self.waveform_sink=None
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
                    phase_ref_deg: float = 0.0,
                    phase2_ref_deg: float = 0.0,
                    marker_phase_deg: float = 0.0, harmonic_order: int = 2,
                    phase3_ref_deg: float = -180.0) -> "dict[str, LockinResult]":
        """Start ONE marker-triggered finite acquisition sampled on every
        configured AI channel simultaneously, and return one lock-in reading
        per channel: ``{chan_name: LockinResult}`` (ai0 is the first entry).

        Simulate mode synthesises each channel at R_ch * I_AC (see
        ``sim_r_ohm``) plus mains + white noise, so the whole multi-channel
        lock-in path runs without hardware."""
        self.last_capture_metadata={}
        if type(harmonic_order) is not int or harmonic_order not in (2,3):
            raise ValueError("Additional harmonic must be 2 or 3")
        if harmonic_order*self.freq_hz >= self.fs_hz/2:
            raise ValueError("Selected harmonic must be below Nyquist")
        if self.simulate:
            data = np.empty((len(self.channels), self._n))
            for i, ch in enumerate(self.channels):
                vp = self.sim_r_ohm[i] * self.i_peak_a
                t = np.arange(self._n) / self.fs_hz
                sig = vp * np.sin(2 * np.pi * self.freq_hz * t + math.radians(marker_phase_deg))
                v2 = self.sim_v2_ratio[i] * vp
                sig = sig + v2 * np.sin(
                    2 * np.pi * (harmonic_order * self.freq_hz) * t + math.radians(harmonic_order*marker_phase_deg + 30.0))
                sig = sig + vp * 0.5 * np.sin(2 * np.pi * 50.0 * t)
                if self.noise_sim_v:
                    sig = sig + (rng if rng is not None else self.sim_rng).normal(
                        0.0, self.noise_sim_v, self._n)
                data[i] = sig
        else:
            data = self._acquire_hardware()
        data = np.asarray(data, dtype=float)
        if data.shape != (len(self.channels), self._n):
            raise ValueError(f"DAQ channel/sample shape mismatch: {data.shape}")
        if not np.all(np.isfinite(data)):
            raise ValueError("DAQ non-finite samples")
        out = {}
        for i, name in enumerate(self._chan_names):
            # Include 2f as a nuisance term in 3f mode to avoid short-window
            # leakage of a real 2f response into the weak third harmonic.
            harmonics = list(range(1,harmonic_order+1))
            offsets = [phase_ref_deg,phase2_ref_deg,phase3_ref_deg]
            fitted = fit_harmonics(data[i], self.actual_fs_hz,
                [h*self.freq_hz for h in harmonics], self.i_peak_a,
                [h*marker_phase_deg+offsets[h-1] for h in harmonics], window=window)
            r1, rh = fitted[0], fitted[-1]
            r1.channel = name
            r1.harmonic_order = harmonic_order
            if harmonic_order == 2:
                r1.v2_peak_v, r1.x2_v, r1.y2_v = rh.v_peak_v, rh.x_v, rh.y_v
                r1.phase2_rad = rh.phase_rad
                r1.ratio2 = rh.v_peak_v/r1.v_peak_v if r1.v_peak_v > 0 else 0.0
            else:
                r1.v2_peak_v = r1.x2_v = r1.y2_v = r1.phase2_rad = r1.ratio2 = None
                r1.v3_peak_v, r1.x3_v, r1.y3_v = rh.v_peak_v, rh.x_v, rh.y_v
                r1.phase3_rad = rh.phase_rad
            out[name] = r1
        return out

    # ------------------------------------------------------------------
    def _effective_range_v(self) -> float:
        """Fixed range (numeric spec) or software-autorange result."""
        spec = str(self.range_spec)
        if spec.lower() != "auto":
            return fixed_range(spec)
        if self._sel_range_v is None:
            self._sel_range_v = self._probe_range_v()
            self.log(f"auto-range: selected +-{self._sel_range_v:g} V pk")
        return self._sel_range_v

    def _probe_range_v(self) -> float:
        """Legacy one-time prescan of >=1 cycle at the largest rated range.
        No hardware overload status is available at 42.4 V. Never interpret
        the ADC's larger internal span as a permitted input voltage.
        """
        n = int(math.ceil(self.fs_hz * max(0.25,1.0/self.freq_hz)))
        term_conf = (TerminalConfiguration.DIFF
                     if str(self.terminal).lower() in ("diff", "differential")
                     else TerminalConfiguration.PSEUDO_DIFF)
        with nidaqmx.Task() as task:
            for name in self._chan_names:
                task.ai_channels.add_ai_voltage_chan(
                    name, min_val=-MAX_INPUT_V, max_val=MAX_INPUT_V,
                    units=VoltageUnits.VOLTS, terminal_config=term_conf)
            for ch in task.ai_channels:
                # Do not silently ignore a failed electrical configuration.
                ch.ai_coupling = (Coupling.AC if self.coupling == "ac" else Coupling.DC)
                ch.ai_excit_src = ExcitationSource.NONE
            task.timing.cfg_samp_clk_timing(
                self.fs_hz, sample_mode=AcquisitionType.FINITE,
                samps_per_chan=n)
            data = np.asarray(task.read(number_of_samples_per_channel=n,
                                        timeout=n/self.fs_hz+5.0))
        if data.shape != (len(self.channels),n) or not np.all(np.isfinite(data)):
            raise ValueError('4462 auto-range prescan invalid or missing channel')
        peak = float(np.max(np.abs(data)))
        if peak >= MAX_INPUT_V*0.999:
            raise ValueError('4462 auto-range prescan exceeds rated input limit')
        for rng in RANGES_V:
            if peak <= 0.9 * rng:
                return rng
        raise ValueError('4462 auto-range has insufficient headroom at maximum range')

    def _acquire_hardware(self) -> np.ndarray:
        """Real DAQmx finite acquisition with PFI start trigger.

        Each 4462 BNC is one channel: center is positive, shield is negative.
        Never interpret adjacent channel BNCs as one differential input pair.
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
                # Do not silently ignore a failed electrical configuration.
                ch.ai_coupling = (Coupling.AC if self.coupling == "ac" else Coupling.DC)
                ch.ai_excit_src = ExcitationSource.NONE
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
            task.start()
            self.actual_fs_hz = float(task.timing.samp_clk_rate)
            ranges = [(float(ch.ai_min), float(ch.ai_max)) for ch in task.ai_channels]
            if len(ranges)!=len(self.channels) or any(
                    not (math.isfinite(low) and math.isfinite(high) and low<0<high)
                    for low,high in ranges):
                raise ValueError('4462 invalid input-range readback')
            # AI.Min/Max return coerced limits. Independently cap the guard
            # at the analog front-end rating even if a driver reports +/-100.
            guard_ranges=[(max(low,-MAX_INPUT_V),min(high,MAX_INPUT_V)) for low,high in ranges]
            overload_supported=(rng_v<MAX_INPUT_V and all(max(-lo,hi)<MAX_INPUT_V for lo,hi in ranges))
            self.last_capture_metadata = dict(requested_fs_hz=self.fs_hz,
                actual_fs_hz=self.actual_fs_hz, n_samples=self._n,
                physical_channels=list(self._chan_names), input_ranges_v=ranges,
                requested_input_range_v=rng_v,guard_ranges_v=guard_ranges,
                hardware_overload_supported=overload_supported,
                trigger_source=str(self.trigger_terminal), trigger_edge=str(self.trigger_edge),
                delay_compensation="none; calibrate each selected harmonic on hardware")
            # Read-only diagnostics: unsupported properties are recorded, never
            # replaced with an assumed delay or used to change NI filter settings.
            diagnostics = {}
            for name, ch in zip(self._chan_names, task.ai_channels):
                diagnostics[name] = {}
                for prop in ("ai_gain", "ai_filter_delay", "ai_filter_delay_units",
                             "ai_enhanced_alias_rejection_enable"):
                    try:
                        diagnostics[name][prop] = str(getattr(ch, prop))
                    except Exception as exc:
                        diagnostics[name][prop] = "unavailable: " + str(exc)
            self.last_capture_metadata["channel_diagnostics"] = diagnostics
            self.log(f"4462 capture: {self.last_capture_metadata}")
            # Allow the actual record plus up to one cycle for the next marker.
            # The record can exceed duration_s when at least one cycle is needed.
            read_timeout = max(float(self.duration_s) * 3.0 + 5.0,
                               self._n / self.actual_fs_hz + 1.0 / self.freq_hz + 5.0)
            self.last_capture_metadata["read_timeout_s"] = read_timeout
            if self.cancel_check is not None:
                deadline=time.monotonic()+read_timeout
                while not task.is_task_done():
                    self.cancel_check()
                    if time.monotonic()>=deadline:raise TimeoutError('4462 capture timeout')
                    (self.wait_callback or time.sleep)(0.05)
            data = task.read(number_of_samples_per_channel=self._n,
                             timeout=read_timeout)
            data = np.asarray(data, dtype=float)
            if data.shape != (len(self.channels), self._n):
                raise ValueError(f"DAQ channel/sample shape mismatch: {data.shape}")
            if not np.all(np.isfinite(data)):
                raise ValueError('DAQ non-finite samples')
            if self.waveform_sink is not None:
                self.last_capture_metadata['waveform_file']=self.waveform_sink(data,self.actual_fs_hz,self._chan_names)
            peaks=np.max(np.abs(data),axis=1).tolist()
            usage=[float(max(np.max(data[i])/hi,np.min(data[i])/lo,0.0))
                   for i,(lo,hi) in enumerate(guard_ranges)]
            self.last_capture_metadata.update(signal_peak_abs_v=peaks,
                signal_mean_v=np.mean(data,axis=1).tolist(),range_usage_fraction=usage)
            # DAQmx latches analog/digital overload independently of sampled
            # voltage. Read Exists exactly once, then Channels, before close.
            if overload_supported:
                try:
                    overloaded = bool(task.in_stream.overloaded_chans_exist)
                    overloaded_channels = list(task.in_stream.overloaded_chans) if overloaded else []
                except Exception as exc:
                    self.last_capture_metadata["overload_status"] = "unavailable: " + str(exc)
                    raise ValueError("4462 hardware overload status unavailable; capture invalid: "
                                     + str(exc)) from exc
                self.last_capture_metadata['overload_status']='detected' if overloaded else 'clear'
            else:
                overloaded=None;overloaded_channels=None
                self.last_capture_metadata['overload_status']='unsupported_42.4V_range; waveform_guard_only'
            self.last_capture_metadata["overloaded_channels"] = overloaded_channels
            self.last_capture_metadata["overload_detected"] = overloaded
            self.log(f"4462 input monitor: peak_V={peaks}, range_usage={usage}, "
                     f"overload={self.last_capture_metadata['overload_status']}")
            if overloaded:
                raise ValueError("4462 hardware overload: " + ", ".join(overloaded_channels))
            for i, (low, high) in enumerate(guard_ranges):
                # Conservative rail guard; external/preamp clipping below the
                # DAQ rails cannot be diagnosed from this threshold alone.
                if np.any(data[i] <= low*0.999) or np.any(data[i] >= high*0.999):
                    raise ValueError(f"{self._chan_names[i]} near input rail / possible clipping")
        # DAQmx returns shape (n_ch, n) for multi-channel finite reads.
        return data


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
