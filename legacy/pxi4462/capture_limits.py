"""Bound finite capture allocation without importing NumPy or NI drivers."""
import math
MAX_SAMPLES_PER_CHANNEL = 2_000_000
MAX_CAPTURE_SECONDS = 600.0

def capture_sample_count(fs_hz, freq_hz, duration_s):
    if any(not math.isfinite(v) or v<=0 for v in (fs_hz,freq_hz,duration_s)):
        raise ValueError('采样率、频率及采集时长必须为有限正数')
    if duration_s>MAX_CAPTURE_SECONDS or 1/freq_hz>MAX_CAPTURE_SECONDS:
        raise ValueError('单点采集/单周期不能超过600秒')
    n0=max(8,round(fs_hz*duration_s))
    cycles=max(1,round(n0*freq_hz/fs_hz))
    n=round(cycles*fs_hz/freq_hz)
    if not 8<=n<=MAX_SAMPLES_PER_CHANNEL or n/fs_hz>MAX_CAPTURE_SECONDS:
        raise ValueError('单通道采集须为8–2000000点且不超过600秒；请调整采样率/时长')
    return n
