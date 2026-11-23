"""NI 4461/4462 nominal peak input ranges (373770k, p.2).

The -20 dB setting is rated only to 42.4 Vpk, not its implied ADC span.
Hardware overload detection is unsupported on this highest range (371235h).
"""
RANGE_GAIN_DB = ((0.316,30), (1.0,20), (3.16,10), (10.0,0), (31.6,-10), (42.4,-20))
RANGES_V = tuple(v for v,_ in RANGE_GAIN_DB)
DEFAULT_RANGE_V = 0.316
MAX_INPUT_V = 42.4

def fixed_range(value):
    if isinstance(value,bool):
        raise ValueError('4462 input range must be a nominal peak voltage')
    try:
        v=float(value)
    except (TypeError,ValueError) as exc:
        raise ValueError('4462 input range must be numeric') from exc
    if v not in RANGES_V:
        raise ValueError(f'4462 supported peak input ranges: {RANGES_V}; got {value!r}')
    return v

def range_label(v):
    label='±316 mVpk' if v==0.316 else f'±{v:g} Vpk'
    return label+('（无硬件过载检测）' if v==42.4 else '')
