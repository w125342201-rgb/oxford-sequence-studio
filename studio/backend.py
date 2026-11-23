"""Single-owner instrument adapters; imports of hardware drivers are lazy."""
import copy
from .model import pin_groups,require_measurement_device,DEFAULT_FIELD_TOLERANCE_T
import math
import sys
import time
import uuid
import os
from pathlib import Path

LEGACY=Path(__file__).resolve().parents[1]/'legacy'

def is_timeout(exc):
    """Recognize wrapped Python/VISA/DAQ timeout errors without importing drivers."""
    seen=set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc,TimeoutError):return True
        if getattr(exc,'error_code',None) in (-1073807339,-200284,-200474):return True
        msg=str(exc).lower()
        if any(x in msg for x in ('timeout','timed out','超时')):return True
        exc=exc.__cause__ or exc.__context__
    return False

def is_environment_io_error(exc):
    return is_timeout(exc) or type(exc).__name__ in ('MercuryCommunicationError','MercuryInvalidResponseError')

class MeasurementError(Exception):
    """A completed/expired acquisition has no usable reading; routing is still known."""

class SimBackend:
    simulated=True
    def __init__(self,c,log):
        self.c=c;self.measurement_device=c['measurement_device'];self.log=log;self.env=dict(temperature=4.2,vti=4.0,field=0.0,pressure=4.0,angle=0.0)
        self.route_name='直接接线';self.route_columns=[];self.output=False;self.target=None
        self.auto_pressure=c['auto_pressure_enabled'];self.serial=0;self.temp_target=None
        from legacy.oxford.oxford_teslatronpt_api import ThermalRule
        self.thermal_rule=ThermalRule(low_pressure_mbar=c['auto_pressure_low_mbar'],high_pressure_mbar=c['auto_pressure_high_mbar'])
    def connect(self): self.log('模拟设备已连接：不会访问网络、VISA、串口或DAQ')
    def snapshot(self): return dict(self.env,magnet_action='HOLD',time=time.time())
    def set_environment(self,axis,target,p):
        if axis=='pressure' and self.auto_pressure:
            raise ValueError('手动设置气压前，请在左侧自动气压设置中关闭自动气压')
        self.env[axis]=target
        if axis=='temperature':
            self.temp_target=target
            if self.c['link_vti_temperature']:
                self.env['vti']=round(target-self.thermal_rule.offset(target),10)
            self.auto_pressure=self.c['auto_pressure_enabled']
            self.service()
    def service(self):
        if self.auto_pressure:
            actual=self.env['temperature']
            regime=self.thermal_rule.pressure_regime(actual)
            if regime=='low':self.env['pressure']=self.thermal_rule.low_pressure_mbar
            elif regime=='high':self.env['pressure']=self.thermal_rule.high_pressure_mbar
    def off(self):self.output=False
    def hold(self): pass
    def close(self): self.off()
    def route(self,p):
        pin_groups(p['columns'])
        self.off();self.route_name=p['name'];self.route_columns=copy.deepcopy(p['columns'])
    def begin(self,kind,p):
        if kind=='resistance':require_measurement_device({'measurement_device':self.measurement_device},p)
        self.output=True
    def measure(self,kind,p,bias,wait):
        if kind=='resistance':require_measurement_device({'measurement_device':self.measurement_device},p)
        wait(min(p.get('settle_s',0.1),0.03))
        self.serial+=1
        tc=4.05-abs(self.env['field'])*3
        r=(6.0+0.03*sum(pin for value in self.route_columns for pin in (value if isinstance(value,list) else [value])))/(1+math.exp(max(-100,min(100,(tc-self.env['temperature'])/0.12))))
        if kind=='resistance' and p['mode']=='delta':
            raw=bias*r
            return dict(current_a=bias,voltage_v=raw,resistance_ohm=r,delta_divisor=1.0,dc_bias_a=0.0,raw=str(raw))
        if kind=='resistance':
            h=p.get('harmonic_order',2)
            channels={str(ch):dict(v_peak_v=(r+ch)*abs(bias),resistance_ohm=r+ch,
                x_v=(r+ch)*bias,y_v=0,harmonic_order=h,
                **{f'v{h}_peak_v':0,f'x{h}_v':0,f'y{h}_v':0}) for ch in p['daq_channels']}
            return dict(current_a=bias,voltage_v=r*bias,resistance_ohm=r,dc_bias_a=0.0,channels=channels,raw=channels)
        raise ValueError('不支持的测量类型')

class HardwareBackend:
    simulated=False
    def __init__(self,c,log):
        self.c=c;self.measurement_device=c['measurement_device'];self.log=log;self.pt=self.src=self.matrix=self.rot=self.rm=None
        self.angle_cache=None;self.angle_read_time=None;self.rotation_until=0;self.rotation_target=None;self.rotation_error=None
        self.daq=None;self.route_name='直接接线';self.route_columns=[]
        self.temp_target=None;self.auto_pressure=c['auto_pressure_enabled'];self.regime=None;self.last_service=0
    def connect(self):
        from .model import validate_config
        validate_config(self.c)
        if self.c['measurement_device']!=self.measurement_device:
            raise ValueError('连接配置中的测量设备已改变；请重新建立连接')
        name='2182A Delta（通过6221控制）' if self.measurement_device=='2182a' else 'PXI-4462（6221交流激励）'
        self.log('本次连接锁定测量设备：'+name)
        sys.path.insert(0,str(LEGACY)) if str(LEGACY) not in sys.path else None
        from oxford.oxford_teslatronpt_api import TeslatronPT,ThermalRule
        c=self.c
        try:
            self.pt=TeslatronPT(itc_vti_host=c['vti_host'],itc_probe_host=c['probe_host'],ips_host=c['ips_host'],port=int(c['port']),timeout=c['io_timeout_s'],thermal_rule=ThermalRule(low_pressure_mbar=c['auto_pressure_low_mbar'],high_pressure_mbar=c['auto_pressure_high_mbar']))
            self.pt.disable_reconnect()
            self.log('Oxford: '+str(self.pt.identify_all()))
            if c['enable_source']:
                import pyvisa
                self.rm=pyvisa.ResourceManager()
                from keithely6221_2182A.keithley_6221_2182a_driver import Keithley6221
                timeout=int(c['io_timeout_s']*1000)
                self.src=Keithley6221(c['source_address'],rm=self.rm,timeout_ms=timeout)
                self.log('6221: '+self.src.idn());self.off()
            if c['enable_matrix']:
                if self.src is None: raise ValueError('启用矩阵前必须启用6221')
                self.off()
                time.sleep(c['matrix_off_delay_s'])
                from matrix.matrix_switch import MatrixSwitch
                self.matrix=MatrixSwitch(resource=c['matrix_resource'],backend='visa',timeout=c['io_timeout_s'],allow_row_sharing=True)
                self.matrix.open(sync=True)
                time.sleep(c['matrix_settle_s'])
            if c['enable_rotator']:
                from rotater.rotator_interface import Rotator
                self.rot=Rotator(c['rotator_port'],angle_limits=(c['min_angle_deg'],c['max_angle_deg']),speed_limits=(0.001,c['max_rotation_speed']),timeout=c['io_timeout_s'])
            if self.rot:self.refresh_angle()
            self.log('真实设备连接完成；电流源已关闭，矩阵（如启用）已全断开')
            self.log('测量链路将在开始采集时校验：'+name)
        except Exception:
            self.close()
            raise
    def invalidate_measurement_cache(self):
        if self.src is not None:self.src._delta_2182_cache=None
    def reconnect_transport(self):
        self.invalidate_measurement_cache()
        if self.pt and hasattr(self.pt,'reconnect_disconnected'):
            self.pt.reconnect_disconnected()
    def snapshot(self):
        cached=getattr(self,'_probe_sample',None)
        if cached and time.monotonic()-cached[0]<1:
            r=self.pt.readings(probe_temperature_k=cached[1],allow_partial=True)
        else:r=self.pt.readings(allow_partial=True)
        return dict(temperature=r.probe_temperature_k,vti=r.vti_temperature_k,field=r.field_t,pressure=r.pressure_mbar,angle=self.angle_cache,angle_read_time=self.angle_read_time,angle_status=('角度未确认（请停止后检查旋转杆）' if self.rotation_error else '运动中/尚未确认' if self.rotation_target is not None else '上次静止回读'),rotation_error=self.rotation_error,rotation_target=self.rotation_target,magnet_action=self.pt.magnet_action(),time=time.time())
    def service(self):
        if not self.rotation_error and self.rotation_target is not None and time.monotonic()>=self.rotation_until:
            try:self.refresh_angle()
            except Exception as exc:
                self.rotation_error='角度回读未确认: '+str(exc)
                self.log(self.rotation_error+'；自动查询已停止，确认静止后可人工复核')
        if (self.temp_target is not None or self.auto_pressure) and time.monotonic()-self.last_service>=1:
            self.last_service=time.monotonic()
            actual=self.pt.probe_temperature()
            if not isinstance(actual,(int,float)) or not math.isfinite(actual) or abs(actual)>=1e30:
                raise ValueError('Probe 温度无效，暂停热联动并等待有效回读')
            self._probe_sample=(time.monotonic(),actual)
            if self.temp_target is None or not self.c['link_vti_temperature']:
                if not self.auto_pressure:return
                regime=self.pt.thermal_rule.pressure_regime(actual)
                if regime is not None and regime!=self.regime:
                    pressure=self.pt.automatic_pressure_target(actual)
                    self.pt.set_pressure(pressure);self.regime=regime
                    self.log(f'自动气压: {pressure:g} mbar')
                return
            self.regime,actions=self.pt.enforce_thermal_link(actual,probe_target_temperature_k=self.temp_target,auto_pressure=self.auto_pressure,previous_regime=self.regime)
            if actions:self.log('热耦合: '+', '.join(actions))
    def set_environment(self,axis,target,p):
        if axis=='temperature':
            self.pt.configure_temperature_ramp(rate_k_per_min=p['rate'],enabled=True,link_vti=self.c['link_vti_temperature'])
            if self.c['link_vti_temperature']:
                self.pt.set_sample_temperature(target)
            else:
                self.pt.set_probe_temperature(target)
            self.log('控温：'+('Probe + VTI 联动' if self.c['link_vti_temperature'] else '仅 Probe')+f'，目标 {target:g} K')
            self.temp_target=target;self.auto_pressure=self.c['auto_pressure_enabled'];self.regime=None
            self.service()
        elif axis=='field':
            return self.pt.set_field_verified(target,ramp_rate_t_per_min=p['rate'],log=self.log,
                retry_interval_s=60.0,wait=getattr(self,'field_wait',None),
                wait_for_front_panel=True,arrival_tolerance_t=p.get('tolerance',DEFAULT_FIELD_TOLERANCE_T))
        elif axis=='pressure':
            if self.auto_pressure:
                raise ValueError('手动设置气压前，请在左侧自动气压设置中关闭自动气压')
            self.pt.set_pressure(target)
        else:
            if self.rot is None: raise RuntimeError('旋转杆未连接')
            if self.rotation_target is not None:raise RuntimeError('先等待上一次旋转结束并确认角度')
            current=self.refresh_angle()
            duration=abs(target-current)/p['rate']+self.c['rotation_margin_s']
            self.rotation_target=target;self.rotation_tolerance=p['tolerance']
            self.rotation_until=time.monotonic()+duration;self.rotation_error=None
            try:self.rot.rotate_to_degree(target,p['rate'])
            except Exception as e:
                self.rotation_error='旋转命令发送失败，位置状态未知: '+str(e)
                if not is_timeout(e):raise
                self.log(self.rotation_error+'；等待回读确认，不重发运动命令')
            self.log(f'旋转目标 {target}°，静默等待 {duration:.1f}s 后单次回读；期间不查询角度')
    def refresh_angle(self):
        if self.rot is None:raise RuntimeError('旋转杆未连接')
        if self.rotation_target is not None and time.monotonic()<self.rotation_until:
            raise RuntimeError('旋转计时等待尚未结束，禁止查询')
        actual=self.rot.read_deg()
        if not isinstance(actual,(int,float)) or not math.isfinite(actual):raise ValueError('角度回读不是有限数值')
        self.angle_cache=actual;self.angle_read_time=time.time()
        if self.rotation_target is not None:
            if abs(actual-self.rotation_target)>self.rotation_tolerance:
                self.rotation_error=f'到点后角度校验失败：目标{self.rotation_target}，回读{actual}，未自动重发运动命令'
                self.log(self.rotation_error+'；保持等待，不自动重查。请停止后检查旋转杆。')
                return actual
            self.rotation_target=None;self.rotation_error=None
        return actual
    def ensure_rotation_idle(self):
        if self.rotation_target is not None or self.rotation_error:
            raise RuntimeError('旋转尚未确认完成，禁止开始采集或切换电极')
    def off(self):
        """Fail closed: relay routing must not proceed if any shutdown stage fails."""
        if self.src is None:return
        errors=[]
        for name,fn in [('wave abort',lambda:self.src.write('SOUR:WAVE:ABOR')),('sweep abort',self.src.abort),('output off',self.src.output_off),('zero current',lambda:self.src.set_current(0.0))]:
            try:fn()
            except Exception as e:errors.append(f'{name}: {e}')
        confirmed=False;last_error=''
        for attempt in range(3):
            try:
                raw=self.src.query(':OUTP?')
                reply=raw.strip().upper()
                try:value=float(reply)
                except ValueError:value=None
                confirmed=reply=='OFF' or (value is not None and math.isfinite(value) and value==0)
                last_error='6221输出状态未确认为关闭: '+repr(raw)
                if confirmed:break
            except Exception as exc:last_error='无法核实输出状态: '+repr(exc)
            self.log(f'关源确认第 {attempt+1}/3 次：{last_error}')
            if attempt<2:time.sleep(0.1)  # bounded cleanup retry, even after Stop
        if not confirmed:errors.append(last_error)
        if errors:
            self.invalidate_measurement_cache()
            raise RuntimeError('关闭电流源未完成；禁止切换电极：'+'; '.join(errors))
    def hold(self):
        if self.pt:self.pt.hold()
    def close(self):
        errors=[]
        try:
            self.off()
            if self.matrix is not None and self.src is not None:
                time.sleep(self.c['matrix_off_delay_s'])
                self.matrix.open_all()
        except Exception as e:
            errors.append('关闭时输出/矩阵清理失败: '+str(e));self.log(errors[-1])
        for name in ['rot','matrix','src','pt','rm']:
            obj=getattr(self,name)
            if obj:
                try:obj.close()
                except Exception as e:
                    errors.append(f'{name} 关闭失败: {e}');self.log(errors[-1])
                setattr(self,name,None)
        return errors
    def route(self,p):
        self.ensure_rotation_idle()
        if not self.matrix or not self.src:raise RuntimeError('矩阵/电流源未连接')
        pin_groups(p['columns'])
        self.off()
        self.matrix.open_all()
        self.matrix.route_4terminal(*p['columns'],rows=tuple(self.c['matrix_rows']),open_others=True)
        self.route_name=p['name'];self.route_columns=copy.deepcopy(p['columns'])
        self.log('矩阵命令已发送（原协议无继电器硬件回读）: '+self.matrix.as_frame_text())
    def begin(self,kind,p):
        if kind=='resistance':require_measurement_device({'measurement_device':self.measurement_device},p)
        self.ensure_rotation_idle()
        if self.src is None:raise RuntimeError('6221未连接')
        self.off();self.daq=None
        self.src.verification_log=self.log
        self.src.wait_callback=getattr(self,'io_wait',None)
        self.src.cancel_check=getattr(self,'cancel_check',None)
        if kind=='resistance' and p['mode']=='pxi':
            self.src.set_compliance(self.c['compliance_v'])
            self.src._delta_2182_cache=None
            old_errors=self.src.clear_errors()
            self.log('6221 初始化前错误队列: '+str(old_errors))
            ac=p['current_a']
            from pxi4462.pxi4462_lockin import Pxi4462Lockin
            self.daq=Pxi4462Lockin(freq_hz=p['frequency_hz'],i_peak_a=ac,fs_hz=p['daq_fs_hz'],duration_s=p['daq_duration_s'],channel=p['daq_device'],channels=[0,1,2,3],voltage_range_v=p['daq_range_v'],coupling=p.get('daq_coupling','dc'),trigger_terminal='PFI0',trigger_edge='falling',simulate=False,log=self.log)
            self.log('4462 输入耦合：'+('AC（-3 dB @ 3.4 Hz；需重新标定 1f/2f/3f 幅相）' if p.get('daq_coupling','dc')=='ac' else 'DC'))
            self.src.configure_ac(freq=p['frequency_hz'],amplitude=ac,offset=0.0,phase_marker=True,marker_line=1,marker_position=p.get('marker_phase_deg',90))
            self.source_settings=self.src.verify_ac(p['frequency_hz'],ac,p.get('marker_phase_deg',90),self.c['compliance_v'])
            self.src.query('STAT:MEAS:EVEN?')
            self.src.arm_ac();self.src.check_errors('AC arm');self.src.init_ac()
            self.src.source_health(require_wave=True)
            self.daq.cancel_check=getattr(self,'cancel_check',None)
            self.daq.wait_callback=getattr(self,'io_wait',None)
            if p.get('daq_save_waveforms',False):self.daq.waveform_sink=self.save_waveform
            self.log('4462相位未做自动硬件延迟校准；X/Y使用保存的参考相位。')
    def save_waveform(self,data,fs,channels):
        import numpy as np
        directory=Path(self.raw_directory);directory.mkdir(parents=True,exist_ok=True)
        path=directory/(uuid.uuid4().hex+'.npz')
        temporary=path.with_suffix('.tmp')
        with temporary.open('wb') as f:
            np.savez_compressed(f,voltage_v=data,fs_hz=fs,channels=np.array(channels))
            f.flush();os.fsync(f.fileno())
        temporary.replace(path)
        return 'waveforms/'+path.name

    def measure(self,kind,p,bias,wait):
        if kind=='resistance':require_measurement_device({'measurement_device':self.measurement_device},p)
        if kind=='resistance' and p['mode']=='delta':
            try:
                r=self.src.run_delta_once(high=0.0+bias,low=0.0-bias,delay=p['delta_delay_s'],count=1,timeout_s=p['timeout_s'],configure_2182=True,voltage_range=p['voltage_range_v'],nplc=p['nplc'],compliance_v=self.c['compliance_v'],reset_before=False,delta_unit='V')
            except (TimeoutError,ValueError) as exc:
                raise MeasurementError('Delta 采集无效: '+str(exc)) from exc
            try:
                readings=r['readings']
                if len(readings)!=1 or r.get('actual_count',1)!=1:
                    raise ValueError('Delta返回点数不是1')
                v=float(readings[0])
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                raise MeasurementError('Delta 无效读数: '+repr(r)) from exc
            return dict(current_a=bias,voltage_v=v,resistance_ohm=v/bias,delta_divisor=1.0,dc_bias_a=0.0,raw=r)
        if kind=='resistance':
            wait(p['settle_s'])
            if self.src is not None:self.src.source_health(require_wave=True)
            from dataclasses import asdict
            try:
                results=self.daq.acquire_one(phase_ref_deg=p['phase_deg'],phase2_ref_deg=p.get('phase2_deg',0.0),marker_phase_deg=p.get('marker_phase_deg',90),harmonic_order=p.get('harmonic_order',2),phase3_ref_deg=p.get('phase3_deg',-180.0))
            except ValueError as exc:
                raise MeasurementError('4462 无效采集: '+str(exc)) from exc
            health=self.src.source_health(require_wave=True) if self.src is not None else {}
            first=next(iter(results.values()))
            raw={k:asdict(v) for k,v in results.items()}
            # The DAQ returns physical names (e.g. PXI1Slot2/ai0), whereas
            # Studio uses logical channel numbers ("0") for CSV columns.
            channels={str(int(k.rsplit('/ai',1)[1])):v for k,v in raw.items()}
            return dict(current_a=bias,voltage_v=first.v_peak_v,resistance_ohm=first.resistance_ohm,dc_bias_a=0.0,x_v=first.x_v,y_v=first.y_v,channels=channels,raw=raw,
                daq_metadata=getattr(self.daq,'last_capture_metadata',{}),source_health=health,
                verified_source_settings=getattr(self,'source_settings',{}))
        raise ValueError('不支持的测量类型')
