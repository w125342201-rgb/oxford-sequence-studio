"""Physical labels and exact persisted/driver values; no hardware imports.

Sources: provided legacy drivers; Keithley 2182A/6221 manuals; NI 446x
specifications; Keithley PA-374 Rev B pinout. See README_使用说明.md.
"""
from legacy.pxi4462.input_ranges import RANGES_V,range_label

def unit_label(value, unit):
    for factor,prefix in [(1,''),(1e-3,'m'),(1e-6,'µ'),(1e-9,'n')]:
        if value >= factor*(1-1e-12):
            return f'{value/factor:g} {prefix}{unit}'
    return f'{value:g} {unit}'

OPTIONS = {
    'harmonic_order': [('二次谐波 2f',2),('三次谐波 3f',3)],
    'measurement_device': [('2182A Delta','2182a'),('PXI-4462（持续交流输出）','pxi')],
    'voltage_range_v': [(unit_label(v,'V'),v) for v in [.01,.1,1.,10.,100.]],
    'daq_range_v': [(range_label(v),v) for v in RANGES_V],
    'daq_coupling': [('DC 耦合（默认）','dc'),('AC 耦合（−3 dB @ 3.4 Hz）','ac')],
}

def display_value(key,value):
    return next((label for label,v in OPTIONS[key] if v == value),f'不支持的旧值：{value}')

def stored_value(key,label):
    for text,value in OPTIONS[key]:
        if text == label:return value
    raise ValueError(f'{key}: 请重新选择受支持的硬件参数')

def validate_discrete(params):
    for key,options in OPTIONS.items():
        if key in params and (isinstance(params[key],bool) or params[key] not in [v for _,v in options]):
            raise ValueError(f'{key}: 不支持的离散值 {params[key]!r}；请在下拉框重新选择')

def wiring_hint(mode):
    if mode=='delta':
        return 'Delta：6221与2182A接RS232交叉线（null-modem）和8501整根Trigger Link；电脑GPIB接6221。触发由Delta自动管理，无需选线。'
    if mode=='pxi':
        return ('4462：6221线1接PFI0（SMB），样品电压接AI；程序固定线1、PFI0、下降沿。6221用GPIB接电脑。\n'
                '一次谐波始终采集；附加谐波可选2f或3f。相位默认：marker=90°、1f=0°、2f=−90°、3f=−180°。'
                '软件按谐波次数补偿marker；每次谐波相位独立设置，不改变6221触发。'
                '3f默认使理想低频自加热且dR/dT为正的响应落在正X3；实际热滞后仍需校准。'
                '导入方案保留原相位，可按“应用热测量默认相位”更新本步骤。\n'
                '输入量程是四路统一的总波形峰值上限，包含直流偏置、1f及其他谐波；不是锁相灵敏度。'
                '最小±316 mV，新建测量默认此档。旧方案保留原量程。±42.4 V档不支持硬件过载检测，软件仅检查已采到的波形。\n'
                '输入耦合默认 DC。选 AC 时在 ADC 前加 3.4 Hz 高通，可去掉低温恒温器的直流背景、'
                '把量程留给交流信号；但它只处理 0 Hz 附近，不去除 f 上的串扰/共模/触发泄漏。'
                '13 Hz 下 1f 约衰减 3.3%、相移 14.7°，2f/3f 各不相同——只适合“只要电流激发响应、'
                '不需要直流偏置”，且需用标准电阻或已知谐波注入重新标定幅相。')
    raise ValueError('不支持的测量模式')


def visible_fields(params,mode=None):
    """Only expose parameters used by the current device/mode; retain stored values."""
    keys=set(params)
    if 'measurement_device' in params:
        return keys
    if 'voltage_range_v' in params:
        mode=mode or params.get('mode','delta')
        daq={k for k in keys if k.startswith('daq_')}
        ac={'frequency_hz','harmonic_order','phase_deg','phase2_deg','phase3_deg','marker_phase_deg'}
        delta={'delta_delay_s','delta_divisor'}
        dmm={'nplc','voltage_range_v'}
        if mode=='pxi':
            keys-=delta|dmm
            keys.discard('timeout_s')
            keys.discard('phase3_deg' if params.get('harmonic_order',2)==2 else 'phase2_deg')
        else:keys-=daq|ac
    return keys
