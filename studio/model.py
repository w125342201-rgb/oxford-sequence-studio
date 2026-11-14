"""Declarative sequence model. All units are SI except K, T, mbar, degrees."""
import copy
import math
import uuid
from decimal import Decimal

VERSION = 7
# Hardware ceilings and default operating points. Step defaults reference these
# constants instead of repeating literals, so "default equals maximum" cannot
# drift when a limit is edited.
FIELD_MAX_RATE_T_PER_MIN = 0.237     # factory magnet fast-rate limit (MRFa = 2 A/min)
SOURCE_MAX_CURRENT_A = 0.105         # 6221 absolute source limit (100 mA nominal, 105 mA overload)
SOURCE_MAX_COMPLIANCE_V = 105.0      # 6221 output compliance ceiling
DEFAULT_SOURCE_CURRENT_A = 0.1       # fixed excitation for new measurement steps
DEFAULT_FIELD_TOLERANCE_T = 0.01     # field readback tolerance for new field steps
CONFIG = {
    'vti_host': '192.168.1.113', 'probe_host': '192.168.1.114',
    'ips_host': '192.168.1.115', 'port': 7020, 'io_timeout_s': 5.0,
    'source_address': 'GPIB0::14::INSTR',
    'matrix_resource': 'GPIB0::20::INSTR',
    'matrix_rows': [1, 2, 3, 4], 'rotator_port': 'COM3',
    'measurement_device': '2182a',
    'link_vti_temperature': True,
    'auto_pressure_enabled': True, 'auto_pressure_low_mbar': 3.0, 'auto_pressure_high_mbar': 10.0,
    'enable_source': True, 'enable_matrix': False,
    'enable_rotator': False,
    'matrix_each_point': False, 'point_routes': [{'name':'A','columns':[12,9,11,10]},{'name':'B','columns':[8,5,7,6]}],
    'min_temperature_k': 1.5, 'max_temperature_k': 300.0,
    'max_field_t': 14.0, 'max_field_rate': FIELD_MAX_RATE_T_PER_MIN,
    'max_temperature_rate': 10.0, 'max_pressure_mbar': 20.0,
    'min_angle_deg': 0.0, 'max_angle_deg': 360.0, 'max_rotation_speed': 1.0, 'rotation_margin_s': 1.5,
    'max_current_a': SOURCE_MAX_CURRENT_A, 'compliance_v': SOURCE_MAX_COMPLIANCE_V,
    'matrix_off_delay_s': 0.5, 'matrix_settle_s': 0.5, 'max_expanded_steps': 100000,
}
# label, default parameters; every node has its own persisted settings.
SPECS = {
    'temperature': ('设置温度', dict(target=4.0, rate=1.0, tolerance=0.02, stable_s=30.0, timeout_s=3600.0)),
    'field': ('设置磁场', dict(target=0.0, rate=FIELD_MAX_RATE_T_PER_MIN, tolerance=DEFAULT_FIELD_TOLERANCE_T, stable_s=5.0, timeout_s=3600.0)),
    'pressure': ('设置气压', dict(target=4.0, tolerance=0.2, stable_s=5.0, timeout_s=300.0)),
    'angle': ('设置角度', dict(target=0.0, rate=0.5, tolerance=0.05, stable_s=1.0, timeout_s=900.0)),
    'wait': ('等待', dict(seconds=10.0)),
    'route': ('切换四端电极', dict(name='通道 A', columns=[12, 9, 11, 10])),
    'channels': ('循环电极组合', dict(routes=[{'name':'通道 A','columns':[12,9,11,10]}, {'name':'通道 B','columns':[8,5,7,6]}])),
    'repeat': ('重复子序列', dict(count=2)),
    'sweep': ('扫描环境参数', dict(axis='temperature', values=[4.0,4.2,4.4], rate=1.0, tolerance=0.02, stable_s=30.0, timeout_s=3600.0)),
    'resistance': ('电压采集（所选设备）', dict(mode='delta', current_a=DEFAULT_SOURCE_CURRENT_A, samples=1, interval_s=0.0, settle_s=0.2, nplc=5.0, voltage_range_v=0.1, delta_delay_s=0.1, timeout_s=60.0, delta_divisor=1.0, frequency_hz=13.0, harmonic_order=2, phase_deg=0.0, phase2_deg=-90.0, phase3_deg=-180.0, marker_phase_deg=90, daq_device='', daq_channels=[0,1,2,3], daq_fs_hz=51200.0, daq_duration_s=1.0, daq_range_v=0.316, daq_coupling='dc', daq_save_waveforms=False)),
}
CONTAINERS = {'sweep','repeat','channels'}
LABELS = {
 'target':'目标值（K / T / mbar / °）','rate':'变化速率（K/min、T/min、°/s）',
 'tolerance':'稳定容差（对应物理单位）','stable_s':'连续稳定时间 / s','timeout_s':'等待提示阈值 / s（到时继续等待）',
 'axis':'扫描量：temperature / field / angle',
 'values':'扫描点列表','seconds':'等待时间 / s','count':'重复次数','name':'电极组合名称',
 'columns':'电极 [I+, I−, V+, V−]；多pin示例 [[12,11],9,8,7]','routes':'电极组合列表（JSON）',
 'mode':'本步骤测量模式（全部步骤跟随左侧设备选择）','current_a':'测量电流 / A（PXI 为峰值）',
 'samples':'重复采样次数','interval_s':'采样间隔 / s',
 'settle_s':'电流稳定等待 / s','nplc':'2182A NPLC','voltage_range_v':'2182A 电压量程 / V',
 'delta_delay_s':'Delta 换向延迟 / s','delta_divisor':'兼容字段（固定为1，不改变Delta电压）',
 'frequency_hz':'交流频率 / Hz','harmonic_order':'附加谐波（一次谐波始终采集）','phase_deg':'1f 解调附加相位 / °（默认 0）','phase2_deg':'2f 解调附加相位 / °（热信号默认 −90）','phase3_deg':'3f 解调附加相位 / °（自加热默认 −180）','marker_phase_deg':'6221 Phase marker 触发位置 / °（默认 90，软件自动补偿）',
 'daq_device':'NI 设备名称（空白自动发现）','daq_channels':'NI 同步采集通道（固定 ai0–ai3）',
 'daq_save_waveforms':'保存原始四路波形（NPZ，可能占用大量磁盘）',
 'daq_fs_hz':'NI 采样率 / Hz','daq_duration_s':'NI 每点采集时长 / s','daq_range_v':'4462 输入量程（四路统一，峰值；含直流与全部谐波）',
 'daq_coupling':'输入耦合（DC 默认；AC 为 3.4 Hz 高通，会改变 1f/2f/3f 幅相，需自行标定）',
}

def node(kind, **params):
    return {'id':uuid.uuid4().hex, 'kind':kind, 'enabled':True,
            'params':dict(copy.deepcopy(SPECS[kind][1]), **params), 'children':[]}

def clone(n):
    n=copy.deepcopy(n)
    n['id']=uuid.uuid4().hex
    n['children']=[clone(c) for c in n['children']]
    return n

def number(x):
    if isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x):
        raise ValueError(f'需要有限数值，收到 {x!r}')
    return float(x)

AXIS_DIGITS = {'temperature': 1, 'field': 3, 'angle': 1}

def span(start, stop, step, axis=None, include_stop=True):
    """Exact decimal grid; optional short final interval, never silent rounding."""
    start, stop, step = map(number, (start, stop, step))
    if step <= 0: raise ValueError('步长必须为正；方向由起止值决定')
    if type(include_stop) is not bool: raise ValueError('包含终点必须为布尔值')
    a, b, delta = map(lambda v: Decimal(str(v)), (start, stop, step))
    if axis is not None:
        quantum = Decimal(1).scaleb(-AXIS_DIGITS[axis])
        for label, value in (('起点', a), ('终点', b), ('步长', delta)):
            if value % quantum:
                raise ValueError(f'{label} {value} 必须为 {quantum} 的整数倍（{axis} 点列精度）；不会自动修改输入')
    count = int(abs(b-a)/delta)
    if count > 100000: raise ValueError('扫描点数超过 100000')
    sign = 1 if b >= a else -1
    exact = [a + sign*delta*i for i in range(count+1)]
    if include_stop and exact[-1] != b: exact.append(b)
    if len(exact) > 100000: raise ValueError('扫描点数超过 100000')
    return [0.0 if v == 0 else float(v) for v in exact]


def pin_groups(v):
    """Validate four terminal groups; each pin belongs to exactly one port."""
    if not isinstance(v, list) or len(v) != 4:
        raise ValueError('四端映射需要 [I+, I−, V+, V−] 四项；每项为 pin 或非空 pin 列表')
    groups = [x if isinstance(x, list) else [x] for x in v]
    flat = [pin for group in groups for pin in group]
    if any(not group for group in groups) or any(type(pin) != int or not 1 <= pin <= 24 for pin in flat):
        raise ValueError('每个口至少一个 pin；pin 必须为 1–24 整数')
    if len(set(flat)) != len(flat):
        raise ValueError('pin 不能重复：同一个 pin 只能分配给一个口')
    return groups

def columns(v, high=24):
    if high == 24:
        pin_groups(v)
    elif not isinstance(v,list) or len(v)!=4 or any(type(x)!=int or not 1<=x<=high for x in v) or len(set(v))!=4:
        raise ValueError(f'四个接口必须为四个不同的 1–{high} 整数')

def parse_pins(text):
    import re
    value = text.strip()
    if value.startswith('[') and value.endswith(']'):
        value = value[1:-1].strip()
    parts = re.split(r'[,，+;；\s]+', value)
    if not parts or any(not re.fullmatch(r'[0-9]+', part) for part in parts):
        raise ValueError('请输入 pin 编号，例如 12 或 12,11')
    pins = [int(part) for part in parts]
    if any(not 1 <= pin <= 24 for pin in pins) or len(set(pins)) != len(pins):
        raise ValueError('pin 必须为不重复的 1–24 整数')
    return pins[0] if len(pins) == 1 else pins

def format_pins(value, separator=','):
    return separator.join(map(str, value)) if isinstance(value, list) else str(value)

def validate_config(c):
    if set(c)!=set(CONFIG): raise ValueError('设备配置字段缺失或未知')
    for k,v in CONFIG.items():
        if isinstance(v,bool):
            if type(c[k])!=bool: raise ValueError(f'{k} 必须为布尔值')
        elif isinstance(v,(int,float)):
            number(c[k])
        elif isinstance(v,str) and not isinstance(c[k],str): raise ValueError(k)
    if c['measurement_device'] not in ('2182a','pxi'):raise ValueError('测量设备必须为2182A或PXI-4462')
    if c['measurement_device']=='pxi' and c['enable_matrix']:raise ValueError('4462连续输出模式不允许启用矩阵切换；请关闭矩阵，使用固定接线')
    columns(c['matrix_rows'],6)
    validate_routes(c['point_routes'])
    if c['enable_matrix'] and not c['enable_source']:raise ValueError('启用矩阵必须启用6221')
    if not 1<=c['port']<=65535 or int(c['port'])!=c['port']: raise ValueError('无效 TCP 端口')
    if not 0<c['max_current_a']<=SOURCE_MAX_CURRENT_A: raise ValueError(f'电流限值必须在 (0,{SOURCE_MAX_CURRENT_A:g}] A')
    if not 0.1<=c['compliance_v']<=SOURCE_MAX_COMPLIANCE_V: raise ValueError('Compliance 必须在 [0.1,105] V')
    if not 0<c['min_temperature_k']<c['max_temperature_k']<=300: raise ValueError('温度范围须在 (0,300] K')
    if not 0<=c['rotation_margin_s']<=3600:raise ValueError('旋转静默等待余量须在0–3600秒')
    if not 0<c['max_temperature_rate']<=100:raise ValueError('温度速率上限须在 (0,100] K/min')
    if not 0<c['max_field_t']<=14: raise ValueError('磁场限值须在 (0,14] T')
    if not 0<c['max_field_rate']<=FIELD_MAX_RATE_T_PER_MIN: raise ValueError(f'本机磁场速率上限 {FIELD_MAX_RATE_T_PER_MIN:g} T/min')
    if not 0<c['max_pressure_mbar']<=2000: raise ValueError('气压限值无效')
    for key in ('auto_pressure_low_mbar','auto_pressure_high_mbar'):
        if not 0<c[key]<=c['max_pressure_mbar']:raise ValueError(key+' 必须大于0且不超过气压限值')
    if not c['min_angle_deg']<c['max_angle_deg']: raise ValueError('角度上下限无效')
    for k in ['io_timeout_s','max_rotation_speed','max_temperature_rate','max_expanded_steps']:
        if c[k]<=0: raise ValueError(k+' 必须大于0')
    if c['matrix_settle_s']<0 or c['matrix_off_delay_s']<0: raise ValueError('矩阵切换前后等待必须非负')
    import re
    match=re.fullmatch(r'GPIB\d*::(\d{1,2})(?:::(\d{1,2}))?::INSTR',c['matrix_resource'],re.I)
    if not match or any(int(v)>30 for v in match.groups() if v is not None):
        raise ValueError('矩阵仅支持GPIB地址，例如 GPIB0::20::INSTR；地址须为0–30')
    addresses=[c['source_address'] if c['enable_source'] else None,c['matrix_resource'] if c['enable_matrix'] else None]
    addresses=[a.strip() for a in addresses if a is not None]
    if any(not a for a in addresses) or len(addresses)!=len(set(addresses)): raise ValueError('启用设备的 VISA 地址不能为空或重复')

def validate_routes(routes):
    if not isinstance(routes,list) or not routes:raise ValueError('电极组合表不能为空')
    names=[]
    for r in routes:
        if not isinstance(r,dict) or set(r)!={'name','columns'} or not isinstance(r['name'],str) or not r['name'].strip():raise ValueError('电极组合需有名称和columns')
        columns(r['columns']);names.append(r['name'])
    if len(names)!=len(set(names)):raise ValueError('电极组合名称须唯一，避免数据混淆')

def check_currents(kind,p,c):
    value=number(p['current_a'])
    if value==0 or (p.get('mode') in ('delta','pxi')) and value<0:
        raise ValueError('测量激励必须非零；交流/Delta使用正幅值')
    if abs(value)>c['max_current_a']*(1+1e-12):raise ValueError('固定测量电流超过上限')

def upgrade_document(d):
    d=copy.deepcopy(d)
    d.setdefault('config',{}).setdefault('link_vti_temperature',True)
    def reject_removed(nodes):
        for n in nodes:
            if n['kind']=='resistance' and n.get('params',{}).get('mode')=='dc':raise ValueError('DC测量已删除；旧DC步骤不能自动转换为Delta')
            if n['kind']=='sweep' and n.get('params',{}).get('axis')=='pressure':raise ValueError('气压扫描已移除；旧方案不会自动删除或改成其他扫描，请重新设计该步骤')
            if n['kind']=='sr830':raise ValueError('SR830支持已删除；含SR830的旧方案不能导入，不会自动删步骤')
            reject_removed(n['children'])
    reject_removed(d.get('nodes',[]))
    if d.get('config',{}).get('measurement_device')=='sr830':raise ValueError('旧方案选择SR830，本版已删除该设备')
    if d.get('schema_version') not in (1,2,3,4,5,6,7):raise ValueError('不支持的方案版本')
    if d['schema_version']==1:
        d['config'].setdefault('matrix_each_point',False)
        d['config'].setdefault('point_routes',copy.deepcopy(CONFIG['point_routes']))
        def walk(nodes):
            for n in nodes:
                if n['kind']=='resistance':n['params'].setdefault('bias_a',0.0)
                walk(n['children'])
        walk(d['nodes']);d['schema_version']=2
    if d['schema_version']==2:
        c=d['config']
        if c.get('matrix_backend','visa')!='visa':
            raise ValueError('旧方案使用串口矩阵，不能自动迁移接线。请保留原文件副本，确认实际GPIB地址后将matrix_backend改为visa，并填写matrix_resource，再导入。')
        c.pop('matrix_backend',None);c.pop('matrix_port',None)
        d['schema_version']=3
    if d['schema_version']==3:
        def migrate(nodes):
            for n in nodes:
                k=n['kind'];p=n['params']
                if k in ('iv','dcon','current_loop'):
                    raise ValueError(f'旧方案包含已移除步骤 {k}，请使用专用程序或手动重新设计；不会自动删除步骤')
                if k=='resistance':
                    if p.get('bias_a',0)!=0:raise ValueError('旧方案包含非零直流偏置，不能自动转换为固定激励电阻测量')
                    p.pop('bias_a',None)
                migrate(n['children'])
        migrate(d['nodes']);d['schema_version']=4
    if d['schema_version']==4:
        c=d['config'];modes=[]
        def modes_in(nodes,active=True):
            for n in nodes:
                enabled=active and n['enabled']
                if enabled and n['kind']=='resistance':
                    mode=n['params']['mode']
                    if mode not in modes:modes.append(mode)
                modes_in(n['children'],enabled)
        modes_in(d['nodes'])
        mode=modes[0] if modes else 'delta'
        c.setdefault('measurement_device','pxi' if mode=='pxi' else '2182a')
        c.setdefault('dmm_mode',mode if mode in ('dc','delta') else 'delta')
        c.pop('enable_dmm',None);c.pop('enable_sr830',None)
        d['schema_version']=5
        d['migration_notes']='旧方案已按第一个启用测量步骤选择设备；所有步骤和标记线原值保留。混用设备/模式的方案需分开运行。PXI新建默认线1；旧方案若为线3，请核实接线后手动修改。'
    if d['schema_version']==5:
        def fixed_triggers(nodes):
            for n in nodes:
                if n['kind']=='resistance':
                    p=n['params']
                    for key,value in {'marker_line':1,'daq_trigger':'PFI0','daq_edge':'falling'}.items():
                        if p.get('mode')=='pxi' and key in p and p[key]!=value:
                            raise ValueError('旧PXI方案触发设置不同于固定线1/PFI0/下降沿；请核对接线后在旧版修改并重新保存，不能自动转换')
                        p.pop(key,None)
                fixed_triggers(n['children'])
        fixed_triggers(d['nodes']);d['config'].pop('sr830_address',None)
        d['schema_version']=6
        d['migration_notes']='旧方案已升级；采集步骤、地址、电极与环境点列保留。触发设置现在按模式固定，界面不再提供选择。混用设备/模式仍需分别运行。'
    if d['schema_version']==6:
        c=d['config']
        if c.get('measurement_device')=='2182a' and c.get('dmm_mode')=='dc':raise ValueError('DC测量已删除；请选择新的Delta方案')
        c.pop('dmm_mode',None);c.pop('dmm_address',None)
        c.setdefault('auto_pressure_enabled',True)
        # Existing automatic pressure behavior used the legacy 4/10 mbar rule.
        c.setdefault('auto_pressure_low_mbar',4.0);c.setdefault('auto_pressure_high_mbar',10.0)
        c.setdefault('matrix_off_delay_s',0.0)
        d['schema_version']=7
        d['migration_notes']='旧方案保留原自动气压4/10 mbar及原切换等待；新建默认3/10 mbar。请在气压/矩阵设置窗口核对。4462现采用整段持续输出，矩阵必须关闭；DC已移除。'
    removed_pressure_flags=False
    def remove_pressure_flags(nodes):
        nonlocal removed_pressure_flags
        for n in nodes:
            if 'auto_pressure' in n.get('params',{}):
                n['params'].pop('auto_pressure')
                removed_pressure_flags=True
            remove_pressure_flags(n.get('children',[]))
    remove_pressure_flags(d['nodes'])
    if removed_pressure_flags:
        d['migration_notes']=d.get('migration_notes','')+' 自动气压统一由左侧设置控制，已移除旧步骤独立开关。'
    corrected=False
    def normalize_delta(nodes):
        nonlocal corrected
        for n in nodes:
            if n['kind']=='resistance':
                p=n['params']
                if p.get('delta_divisor',1)!=1:corrected=True
                p['delta_divisor']=1.0
            normalize_delta(n.get('children',[]))
    normalize_delta(d['nodes'])
    if corrected:d['migration_notes']=d.get('migration_notes','')+' Delta已移除额外除2，新采集按V/I保存电阻；已有数据文件不变。'
    d['config'].setdefault('rotation_margin_s',1.5)
    normalize_pxi_channels(d['nodes'])
    return d

def selected_mode(c):
    try:return {'2182a':'delta','pxi':'pxi'}[c['measurement_device']]
    except (KeyError,TypeError):raise ValueError('测量设备必须为2182A或PXI-4462') from None

def set_measurement_device(nodes,c,device):
    """An explicit UI selection applies to every step, including disabled ones.

    Retain both instruments' stored parameters so switching back is lossless.
    Loading a file alone does not call this: its saved settings remain intact.
    """
    mode=selected_mode({'measurement_device':device})
    changed=0
    def walk(items):
        nonlocal changed
        for n in items:
            if n['kind']=='resistance':
                p=n['params']
                changed+=p.get('mode')!=mode
                p['mode']=mode
                if mode=='pxi':p['daq_channels']=[0,1,2,3]
            walk(n.get('children',[]))
    walk(nodes)
    c['measurement_device']=device
    return changed

def require_measurement_device(c,p):
    mode=selected_mode(c)
    if p.get('mode')!=mode:
        name='2182A Delta' if mode=='delta' else 'PXI-4462'
        raise ValueError(f"测量步骤 {p.get('mode')} 与已选设备 {name} 不符；请先断开连接，在左侧重新选择测量设备以同步全部采集步骤")

def validate_device_selection(nodes,c):
    """Check executable steps in simulation and hardware before any I/O."""
    selected_mode(c)
    for n in nodes:
        if not n['enabled']:continue
        if n['kind']=='resistance':require_measurement_device(c,n['params'])
        validate_device_selection(n['children'],c)

def normalize_pxi_channels(nodes):
    """All current and imported PXI sequences acquire all four AI inputs."""
    for n in nodes:
        if n['kind']=='resistance':
            # Schemes predating the explicit field used marker=0. New nodes
            # still get the explicit 90-degree preference from SPECS.
            n['params'].setdefault('marker_phase_deg',0)
            n['params'].setdefault('phase2_deg',0.0)
            n['params'].setdefault('harmonic_order',2)
            n['params'].setdefault('phase3_deg',-180.0)
            n['params'].setdefault('daq_coupling','dc')
            n['params'].setdefault('daq_save_waveforms',False)
            if n['params'].get('mode')=='pxi':n['params']['daq_channels']=[0,1,2,3]
        normalize_pxi_channels(n.get('children',[]))

def pxi_run_params(nodes):
    """PXI 整段运行契约（D-024）。

    频率/1f 相位/谐波/marker/DAQ 必须全程一致；**电流允许逐步骤不同**，
    由引擎在步骤边界重配（Engine.measurement → _pxi_changed）。
    返回首个启用 PXI 步骤的完整参数，其 current_a 仅作为开跑时的初始激励。
    """
    measurements=[]
    def walk(items):
        for n in items:
            if not n['enabled']:continue
            if n['kind']=='resistance':measurements.append(n['params'])
            walk(n['children'])
    walk(nodes)
    pxi=[copy.deepcopy(p) for p in measurements if p['mode']=='pxi']
    for p in pxi:
        for key,value in (('marker_phase_deg',0),('phase2_deg',0.),('phase3_deg',-180.),('harmonic_order',2),('daq_save_waveforms',False)):
            p.setdefault(key,value)
    if not pxi:return None
    if len(pxi)!=len(measurements):raise ValueError('4462持续输出不能与Delta混合运行，请分开保存方案')
    keys=('frequency_hz','phase_deg','harmonic_order','marker_phase_deg','daq_device','daq_fs_hz','daq_duration_s','daq_range_v','daq_coupling','daq_save_waveforms')
    keys=keys+(('phase3_deg',) if pxi[0].get('harmonic_order',2)==3 else ('phase2_deg',))
    if any(any(p.get(k,0)!=pxi[0].get(k,0) for k in keys) for p in pxi[1:]):raise ValueError('4462持续输出要求整段序列使用相同频率/谐波/相位/DAQ设置（电流可按步骤不同）；请分别运行不同设置')
    result=copy.deepcopy(pxi[0]);result.setdefault('marker_phase_deg',0);result.setdefault('phase2_deg',0.0);result.setdefault('harmonic_order',2);result.setdefault('phase3_deg',-180.0);result['daq_channels']=[0,1,2,3]
    return result

def pxi_voltage_columns(harmonic_order=2):
    """CSV suffix and result-key pairs; never relabel 3f as 2f."""
    h=harmonic_order
    return [('V','v_peak_v'),('X_V','x_v'),('Y_V','y_v'),
            (f'{h}f_V',f'v{h}_peak_v'),(f'{h}f_X_V',f'x{h}_v'),(f'{h}f_Y_V',f'y{h}_v')]

def measurement_node(c,current_a=None):
    mode=selected_mode(c)
    n=node('resistance',mode=mode)
    if current_a is not None:n['params']['current_a']=current_a
    return n

def validate(nodes,c,hardware=False):
    validate_config(c)
    if not isinstance(nodes,list) or not nodes: raise ValueError('序列不能为空')
    ids=set()
    def val(n,depth,active=True):
        if depth>16: raise ValueError('嵌套超过16层')
        if set(n)!={'id','kind','enabled','params','children'}: raise ValueError('节点结构无效')
        if not isinstance(n['id'],str) or not n['id'] or n['id'] in ids: raise ValueError('节点ID重复或无效')
        ids.add(n['id'])
        k=n['kind']; p=n['params']
        active=active and n['enabled']; hw=hardware and active
        if k not in SPECS or type(n['enabled'])!=bool or not isinstance(n['children'],list): raise ValueError('节点类型无效')
        if set(p)!=set(SPECS[k][1]): raise ValueError(f'{k}: 参数字段缺失或未知')
        if k not in CONTAINERS and n['children']: raise ValueError('只有循环节点允许子步骤')
        for key,default in SPECS[k][1].items():
            v=p[key]
            if isinstance(default,bool):
                if type(v)!=bool: raise ValueError(key+' 必须为布尔值')
            elif isinstance(default,(int,float)): number(v)
            elif isinstance(default,str) and not isinstance(v,str): raise ValueError(key+' 必须为字符串')
            elif isinstance(default,list) and not isinstance(v,list): raise ValueError(key+' 必须为列表')
        # Removed features remain structural errors, including in saved disabled nodes.
        if k=='resistance' and p['mode'] not in ('delta','pxi'):raise ValueError('只支持Delta或4462测量，DC已删除')
        if k=='sweep' and p['axis']=='pressure':raise ValueError('气压扫描已移除；仅允许温度、磁场、角度扫描')
        if not active:
            for child in n['children']:val(child,depth+1,False)
            return
        from .instrument_options import validate_discrete
        validate_discrete(p)
        for key in ['stable_s','seconds','interval_s','settle_s']:
            if key in p and p[key]<0: raise ValueError(key+' 必须非负')
        for key in ['timeout_s','tolerance','nplc','voltage_range_v','delta_delay_s','frequency_hz','daq_fs_hz','daq_duration_s','daq_range_v']:
            if key in p and p[key]<=0: raise ValueError(key+' 必须为正')
        for key in ['count','samples']:
            if key in p and (type(p[key])!=int or not 1<=p[key]<=100000): raise ValueError(key+' 必须为 1–100000 整数')
        if k in ('temperature','field','pressure','angle','sweep'):
            axis=p['axis'] if k=='sweep' else k
            if k=='sweep' and axis=='pressure':raise ValueError('气压扫描已移除；仅允许温度、磁场、角度扫描')
            if axis not in ('temperature','field','pressure','angle'): raise ValueError('未知扫描量')
            if axis=='pressure' and c['auto_pressure_enabled']:
                raise ValueError('手动设置气压前，请在左侧自动气压设置中关闭自动气压')
            values=p['values'] if k=='sweep' else [p['target']]
            if not isinstance(values,list) or not values: raise ValueError('扫描点不能为空')
            lo,hi={'temperature':(c['min_temperature_k'],c['max_temperature_k']),'field':(-c['max_field_t'],c['max_field_t']),'pressure':(0,c['max_pressure_mbar']),'angle':(c['min_angle_deg'],c['max_angle_deg'])}[axis]
            if any(not lo<=number(v)<=hi for v in values): raise ValueError(f'{axis} 超出允许范围 [{lo},{hi}]')
            if axis!='pressure':
                top={'temperature':c['max_temperature_rate'],'field':c['max_field_rate'],'angle':c['max_rotation_speed']}[axis]
                if not 0<p['rate']<=top: raise ValueError(f'{axis} 速率须在 (0,{top}]')
            if hw and axis=='angle' and not c['enable_rotator']: raise ValueError('未启用旋转杆')
        if k in ('route','channels'):
            routes=p['routes'] if k=='channels' else [p]
            if not routes: raise ValueError('电极列表不能为空')
            for r in routes:
                if not isinstance(r,dict) or set(r)!={'name','columns'} or not isinstance(r['name'],str): raise ValueError('电极格式应为 name / columns')
                columns(r['columns'])
            if active and c['enable_matrix'] and c['matrix_each_point']:raise ValueError('已选择每点遍历电极表，请删除/禁用显式电极切换或电极循环，避免路由被覆盖')
        if k=='resistance':
            if hw:require_measurement_device(c,p)
            if hw and not c['enable_source']: raise ValueError('未启用6221')
            if p['delta_divisor']!=1:raise ValueError('Delta固定按V/I计算；请重新导入旧方案以移除额外除数')
            check_currents(k,p,c)
            if 'nplc' in p and not 0.01<=p['nplc']<=50: raise ValueError('NPLC须在0.01–50')
            if 'delta_delay_s' in p and not 0.001<=p['delta_delay_s']<=9999: raise ValueError('Delta延迟范围无效')
            if p['mode']=='pxi':
                marker=number(p.get('marker_phase_deg',90))
                if not 0<=marker<=360 or not marker.is_integer():raise ValueError('Phase marker 须为 0–360 的整数角度')
                number(p.get('phase2_deg',0.0));number(p.get('phase3_deg',-180.0));number(p['phase_deg'])
                h=p.get('harmonic_order',2)
                if type(h) is not int or h not in (2,3):raise ValueError('附加谐波只能选择2或3')
                from legacy.pxi4462.capture_limits import capture_sample_count
                capture_sample_count(p['daq_fs_hz'],p['frequency_hz'],p['daq_duration_s'])
                if not p['daq_channels'] or any(type(v)!=int or v not in range(4) for v in p['daq_channels']) or len(set(p['daq_channels']))!=len(p['daq_channels']): raise ValueError('PXI通道须为不重复的0–3')
                if not 1000<=p['daq_fs_hz']<=204800 or p['frequency_hz']*2*h>=p['daq_fs_hz']: raise ValueError('PXI采样率/频率无效，须容纳所选谐波')
        for child in n['children']: val(child, depth+1, active)
        if active and k in CONTAINERS and not n['children']: raise ValueError('循环节点必须加入子步骤')
    for n in nodes: val(n, 0)
    if pxi_run_params(nodes) and c['enable_matrix']:raise ValueError('4462持续输出不允许矩阵切换')
    count=workload(nodes,c)['operations']
    if count>c['max_expanded_steps']: raise ValueError('展开后的执行操作数超过上限（包含环境设定、等待、路由和采集）；可调整序列最大执行操作数')
    if not count: raise ValueError('没有启用的步骤')
    return count

def workload(nodes,c):
    """Count logical operations, acquisitions and table rows, not SCPI commands.
    Call after validation. Containers add no fictitious execution steps.
    """
    total=dict(operations=0,acquisitions=0,csv_rows=0)
    for n in nodes:
        if not n['enabled']:continue
        k,p=n['kind'],n['params']
        if k in CONTAINERS:
            item=workload(n['children'],c)
            mult=p['count'] if k=='repeat' else len(p['values']) if k=='sweep' else len(p['routes']) if c['enable_matrix'] else 1
            item={key:val*mult for key,val in item.items()}
            if k=='sweep' or (k=='channels' and c['enable_matrix']):item['operations']+=mult
        elif k=='resistance':
            routes=len(c['point_routes']) if c['enable_matrix'] and c['matrix_each_point'] else 1
            acquisitions=p['samples']*routes
            route_operations=acquisitions if c['enable_matrix'] and c['matrix_each_point'] else 0
            item=dict(operations=acquisitions+route_operations,acquisitions=acquisitions,csv_rows=p['samples'])
        else:item=dict(operations=1,acquisitions=0,csv_rows=0)
        for key in total:total[key]+=item[key]
    return total

def example():
    # 50 mT steps keep every step well above the 10 mT default tolerance, so the
    # built-in demo cannot record a mid-ramp field on real hardware.
    t=node('sweep',axis='temperature',values=[3.5,4.0,4.5],stable_s=1.0)
    b=node('sweep',axis='field',values=[0.0,0.05,0.10,0.05,0.0],rate=FIELD_MAX_RATE_T_PER_MIN,tolerance=DEFAULT_FIELD_TOLERANCE_T,stable_s=0.5)
    b['children']=[node('resistance')];t['children']=[b]
    return [t]
