"""Deterministic nested execution, cancellation, durable raw + tabular output."""
import copy
from collections import deque
import csv
from .backend import MeasurementError, is_timeout, is_environment_io_error
from legacy.oxford.oxford_teslatronpt_api import normalize_action
from .model import format_pins,pxi_voltage_columns,workload
import json
import math
import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .model import VERSION,validate,check_currents,pxi_run_params,normalize_pxi_channels,validate_device_selection

class Stopped(Exception): pass

def finite_tree(value):
    if isinstance(value,(int,float)) and (not math.isfinite(value) or abs(value)>=1e30):
        raise ValueError('采集返回非有限数值或仪表超量程标志')
    if isinstance(value,dict):
        for v in value.values(): finite_tree(v)
    elif isinstance(value,(list,tuple)):
        for v in value:finite_tree(v)

class Engine:
    def __init__(self,backend,config,emit=lambda *a:None):
        self.b=backend;self.c=copy.deepcopy(config);self.emit=emit
        self.stop=threading.Event();self.pause=threading.Event()
        self.b.field_wait=self.wait
        self.b.cancel_check=self.check;self.b.io_wait=self.io_wait
        self._pausing=False;self.pause_generation=0;self.measurement_restart=None
        self.active=False;self.state='idle';self.file=None;self.journal=None;self.run_dir=None
        self.invalid_points=0;self.points=0;self.csv_rows=0;self.last_poll=0;self.last_environment=None;self.path='';self.logical_point=0;self.route_index=0
        self.continuous_pxi=None
        self.pxi_applied=None
        self.read_failures=0
        self.required_environment=set();self.environment_overrides={};self.consecutive_invalid=0
        self.table_row=None;self.table_position=None;self.table_point=None
    def log(self,msg):
        self.emit('log',msg)
        if self.journal:self.event('log',{'message':msg})
    def event(self,kind,data):
        if self.journal:
            self.journal.write(json.dumps({'time':datetime.now(timezone.utc).isoformat(),'event':kind,'data':data},ensure_ascii=False,default=str,allow_nan=False)+'\n')
            self.journal.flush()
            os.fsync(self.journal.fileno())
    def check(self):
        if self.stop.is_set():raise Stopped('用户请求停止')
    def io_wait(self,seconds):
        """Instrument setup wait: stop-aware, no reentrant instrument I/O."""
        self.check()
        self.stop.wait(seconds)
        self.check()
    def retry_io(self,label,operation,invalid=False):
        """Retry bounded I/O timeouts forever at this exact operation; Stop wins."""
        attempts=0;started=time.monotonic()
        while True:
            self.check()
            if not self._pausing and self.pause.is_set():self.gate()
            try:
                if attempts and hasattr(self.b,'reconnect_transport'):self.b.reconnect_transport()
                result=operation()
                if attempts:
                    self.log(label+' 已恢复，继续当前步骤')
                    self.emit('state','运行中')
                return result
            except Stopped:raise
            except Exception as exc:
                if not (is_timeout(exc) or type(exc).__name__ in ('MercuryCommunicationError','MercuryInvalidResponseError') or (invalid and isinstance(exc,(ValueError,MeasurementError)))):raise
                attempts+=1;self.read_failures+=1
                self.log(f'{label} 等待恢复（第 {attempts} 次）：{exc}；保留当前进度，5 秒后重查，无总超时')
                elapsed=time.monotonic()-started
                self.emit('state',f'{label} 中断，已等待 {elapsed/60:.1f} 分钟（保留进度）')
                self.event('io_wait',dict(label=label,attempts=attempts,elapsed_s=elapsed,path=self.path,error=f'{type(exc).__name__}: {exc}'))
                self.emit('stale',None)
                self.check()
                
                for _ in range(50):
                    self.check()
                    if not self._pausing and self.pause.is_set():self.gate()
                    self.stop.wait(0.1)
                self.check()

    def clean_environment(self,env,require=False):
        env=dict(env);bad=[]
        for key in ('temperature','vti','field','pressure','angle'):
            value=env.get(key)
            if value is None and key=='angle' and not self.c['enable_rotator']:continue
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or abs(value)>=1e30:
                env[key]=None;bad.append(key)
        required=set(self.required_environment)
        if self.c['auto_pressure_enabled']:required.update(('temperature','pressure'))
        if getattr(self.b,'temp_target',None) is not None:
            required.add('temperature')
            if self.c['link_vti_temperature']:required.add('vti')
        if require and (required.intersection(bad) or all(env.get(k) is None for k in ('temperature','vti','field','pressure'))):
            raise ValueError('控制所需环境读数无效: '+', '.join(sorted(required.intersection(bad) or bad)))
        env['invalid_channels']=bad
        finite_tree(env)
        return env

    def poll(self,force=False):
        if force or self.b.simulated or self.last_environment is None or time.monotonic()-self.last_poll>=1:
            def read_environment():
                self.b.service();e=self.clean_environment(self.b.snapshot(),require=True)
                return e
            e=self.retry_io('环境回读',read_environment,invalid=True)
            self.last_poll=time.monotonic();self.last_environment=e;self.emit('environment',e)
        return self.last_environment
    def wait(self,seconds):
        deadline=time.monotonic()+seconds
        while True:
            self.check()
            if not self._pausing and self.pause.is_set():
                before=time.monotonic();self.gate();deadline+=time.monotonic()-before
            left=deadline-time.monotonic()
            if left<=0:return
            self.poll()
            self.stop.wait(min(0.1,left))
    def gate(self,restart=None):
        self.check()
        if not self.pause.is_set() or self._pausing:return
        self._pausing=True
        try:
            self.retry_io('关闭电流源',self.b.off)
            if hasattr(self.b,'invalidate_measurement_cache'):self.b.invalidate_measurement_cache()
            self.state='paused';self.pause_generation+=1
            self.emit('state','已暂停（电流源关闭；环境控制继续）')
            self.event('pause',{'path':self.path})
            # No recursive environment reads: a disconnected controller must
            # not prevent Resume/Stop while the source is already off.
            while self.pause.is_set():self.io_wait(0.1)
            self.check();self.state='running'
            if self.pxi_applied:self.retry_io('准备连续采集',lambda:self.b.begin('resistance',self.pxi_applied))
            elif restart or self.measurement_restart:self.retry_io('恢复测量',restart or self.measurement_restart)
            self.emit('state','运行中');self.event('resume',{'path':self.path})
        finally:self._pausing=False
    def environment(self,axis,target,p):
        self.required_environment.add(axis);self.environment_overrides.pop(axis,None)
        return self._environment(axis,target,p)

    def _environment(self,axis,target,p):
        self.check()
        if not self.continuous_pxi:self.retry_io('关闭电流源',self.b.off)
        # A timed-out field/rotation command may already be running: observe,
        # never replay a motion command merely because its reply was lost.
        try:
            if axis in ('field','angle'):result=self.b.set_environment(axis,target,p)
            else:result=self.retry_io('设置'+axis,lambda:self.b.set_environment(axis,target,p))
        except Exception as exc:
            if not is_environment_io_error(exc):raise
            result={'field_wait_reason':str(exc)}
            self.log(f'{axis} 命令状态未确认；等待实际到位，不重发运动命令：{exc}')
        self.last_environment=None
        try:finite_tree(result);logged_result=result
        except ValueError:logged_result={'invalid_numeric_result':repr(result)}
        self.event('set_environment',dict(axis=axis,target=target,params=p,result=logged_result,path=self.path))
        if axis=='field' and isinstance(result,dict) and result.get('no_op_ramp'):
            actual=result.get('field_readback_t')
            if not isinstance(actual,(int,float)) or not math.isfinite(actual) or abs(actual)>=1e30:
                actual=self.poll(force=True)['field']
            if actual is None or not math.isfinite(actual) or abs(actual-target)>p['tolerance']:
                self.event('field_not_started',dict(target=target,actual=actual,tolerance=p['tolerance'],result=logged_result,path=self.path))
                result['field_wait_reason']='磁场未启动且未达测量容差；保留进度等待恢复，不自动放宽容差'
        start=time.monotonic();stable=None;field_waiting=False;last_field_notice=None;timeout_logged=False;read_failures=self.read_failures;pause_generation=self.pause_generation;last_heartbeat=start-60;last_notice_time=-float('inf');trend=deque()
        def field_wait(reason, action='UNKNOWN', actual=None, in_tolerance=False):
            nonlocal field_waiting,last_field_notice,last_notice_time
            notice='settling' if in_tolerance else action
            if last_field_notice==notice and time.monotonic()-last_notice_time<60:return
            last_notice_time=time.monotonic();last_field_notice=notice;field_waiting=True
            elapsed=(time.monotonic()-start)/60
            if in_tolerance:
                message='磁场已在容差内，等待连续稳定；无需操作前面板'
            elif action=='RTOS':
                estimate=abs(target-actual)/p['rate'] if actual is not None else None
                suffix=f'，按设定速率估算剩余 {estimate:.1f} min（非保证值）' if estimate is not None else ''
                message=f'励磁进行中，已等待 {elapsed:.1f} min{suffix}；程序持续监测，无需操作前面板'
            elif action=='RTOZ':
                message='电源正在回零，尚未完成本步骤目标；程序持续监测，不重发运动命令'
            elif action in ('PAUS','PAUSE'):
                message='电源处于暂停态，请检查原因并在励磁电源前面板恢复；程序持续监测'
            elif action in ('HOLD','CLMP'):
                message='电源处于保持/钳位态，但磁场未到位；请检查目标值、容差和电源状态'
            else:
                message=f'磁场状态未知或启动未确认（状态 {action}，原始回读 {(self.last_environment or {}).get("magnet_action")!r}）；不重发运动命令，等待状态回读恢复'
            self.emit('state',message+'（实验进度保留）')
            self.log(f'磁场等待：{reason}；目标 {target:g} T。{message}。')
            self.event('field_wait',dict(target=target,actual=actual,action=action,reason=reason,path=self.path))
        if axis=='field' and isinstance(result,dict) and result.get('field_wait_reason'):
            field_wait(result['field_wait_reason'])
        while True:
            self.gate()
            e=self.poll();actual=e[axis]
            now=time.monotonic()
            if now-last_heartbeat>=60:
                self.event('environment_wait',dict(axis=axis,target=target,actual=actual,action=normalize_action(e.get('magnet_action')),action_raw=e.get('magnet_action'),elapsed_s=now-start,path=self.path,rotation_error=e.get('rotation_error')))
                last_heartbeat=now
            if axis=='angle' and e.get('rotation_error'):
                self.emit('state','角度未确认：保持等待，不采集；请停止后检查旋转杆')
            if pause_generation!=self.pause_generation:
                stable=None;pause_generation=self.pause_generation
            if self.read_failures!=read_failures:
                stable=None;read_failures=self.read_failures
            if actual is None:
                stable=None
                self.log(axis+' 没有有效回读，保留当前位置等待恢复')
                self.wait(5.0);continue
            ok=abs(actual-target)<=p['tolerance']
            if axis=='angle' and not self.b.simulated:
                ok=ok and self.b.rotation_target is None
            if axis=='field':
                action=normalize_action(e.get('magnet_action'))
                ok=ok and action in ('HOLD','CLMP')
            if ok:
                stable=stable if stable is not None else time.monotonic()
                required=min(p['stable_s'],0.05) if self.b.simulated else p['stable_s']
                if time.monotonic()-stable>=required:
                    if field_waiting:
                        self.log(f'磁场已到达 {target:g} T 并稳定，自动继续当前实验。')
                        self.event('field_recovered',dict(target=target,actual=actual,path=self.path))
                        self.emit('state','运行中')
                    return
            else:stable=None
            if axis=='field':
                if time.monotonic()-start>p['timeout_s']:
                    field_wait(f'超过 {p["timeout_s"]} s 尚未稳定，实际 {actual:g} T',action,actual,ok)
                elif not ok and (field_waiting or action in ('PAUS','PAUSE','HOLD','CLMP')):
                    field_wait(f'电源状态 {action}，实际 {actual:g} T，尚未到位',action,actual)
            elif time.monotonic()-start>p['timeout_s'] and not timeout_logged:
                timeout_logged=True
                self.log(f'{axis} 尚未稳定，目标 {target}，实际 {actual}；继续无限等待，不结束实验')
            trend.append((now,actual))
            while len(trend)>1 and now-trend[0][0]>60:trend.popleft()
            delta=actual-trend[0][1];window=now-trend[0][0]
            self.emit('progress',f'{axis}: 当前 {actual:.6g} / 目标 {target:.6g} | 残差 {actual-target:+.4g} | 已等 {(now-start)/60:.1f} min | 近{window:.0f}s变化 {delta:+.4g}')
            self.wait(0.05 if self.b.simulated else 0.5)
    def route(self,p):
        self.check()
        if not self.c['enable_matrix']:
            self.b.route_name='直接接线';self.b.route_columns=[]
            self.log('矩阵未启用：跳过路由命令');return
        if self.continuous_pxi:raise RuntimeError('4462持续输出期间禁止切换矩阵')
        self.retry_io('关闭电流源',self.b.off);self.check()
        self.event('matrix_off_wait',{'seconds':self.c['matrix_off_delay_s']})
        self.wait(self.c['matrix_off_delay_s'])
        self.event('matrix_off_wait_end',{})
        self.retry_io('设置矩阵路由',lambda:self.b.route(p))
        self.event('matrix_on_wait',{'seconds':self.c['matrix_settle_s']})
        self.wait(self.c['matrix_settle_s'])
        self.event('matrix_on_wait_end',{})
        self.emit('route',dict(name=self.b.route_name,columns=self.b.route_columns))
        self.event('route_command',p)
    def acquire_delta(self,n,bias):
        """Invalid acquisition becomes NULL; environment readiness still blocks."""
        self.check();data=None
        self.poll(force=True)  # Do not start a new acquisition with broken environment I/O.
        started=datetime.now(timezone.utc).isoformat()
        try:
            data=self.b.measure(n['kind'],n['params'],bias,self.wait)
            try:
                finite_tree(data)
                if self.continuous_pxi:
                    channels=data.get('channels',{}) if isinstance(data,dict) else {}
                    for ch in self.continuous_pxi['daq_channels']:
                        channel=channels.get(str(ch),channels.get(ch,{}))
                        for _,key in pxi_voltage_columns(self.continuous_pxi.get('harmonic_order',2)):
                            if isinstance(channel.get(key),bool) or not isinstance(channel.get(key),(int,float)):
                                raise ValueError(f'ai{ch} 缺少有效 {key}')
                value=data.get('voltage_v') if isinstance(data,dict) else None
                if isinstance(value,bool) or not isinstance(value,(int,float)):
                    raise ValueError('电压读数缺失或不是数值')
            except ValueError as exc:raise MeasurementError(str(exc)) from exc
            self.consecutive_invalid=0
            self.emit('quality',dict(total=self.invalid_points,consecutive=0))
            data['acquisition_start_utc']=started
            data['acquisition_end_utc']=datetime.now(timezone.utc).isoformat()
            return data
        except Stopped:raise
        except Exception as exc:
            if not (isinstance(exc,MeasurementError) or is_timeout(exc)):raise
            self.check()
            if not self.continuous_pxi:
                self.retry_io('关闭电流源',self.b.off)
            self.invalid_points+=1;self.consecutive_invalid+=1
            self.emit('quality',dict(total=self.invalid_points,consecutive=self.consecutive_invalid))
            if self.consecutive_invalid>=10 and self.consecutive_invalid%10==0:
                self.event('invalid_streak',dict(total=self.invalid_points,consecutive=self.consecutive_invalid,path=self.path))
                self.log(f'告警：连续 {self.consecutive_invalid} 次采集无效，继续遍历；请检查接线和触发')
            reason=f'{type(exc).__name__}: {exc}'
            self.event('invalid_raw',dict(path=self.path,route=self.b.route_name,
                columns=self.b.route_columns,logical_point=self.logical_point,
                route_index=self.route_index,reason=reason,raw_repr=repr(data)))
            self.log(f'电极组合 {self.b.route_name} 读数无效/采集超时，写入 NULL 并继续：{exc}')
            result=dict(current_a=bias,voltage_v=None,resistance_ohm=None,
                measurement_status='invalid',measurement_error=reason,raw=repr(data),
                daq_metadata=getattr(getattr(self.b,'daq',None),'last_capture_metadata',{}))
            if self.continuous_pxi:
                result['channels']={str(ch):dict(resistance_ohm=None,harmonic_order=self.continuous_pxi.get('harmonic_order',2),
                    **{key:None for _,key in pxi_voltage_columns(self.continuous_pxi.get('harmonic_order',2))})
                    for ch in self.continuous_pxi['daq_channels']}
            result['acquisition_start_utc']=started
            result['acquisition_end_utc']=datetime.now(timezone.utc).isoformat()
            return result

    def record(self,n,data):
        try:finite_tree(data)
        except Exception:
            self.event('invalid_raw',{'path':self.path,'raw_repr':repr(data)})
            raise
        # Commit the measurement BEFORE any further controller traffic.
        self.event('acquisition',dict(point=self.points+1,acquisition_index=self.points+1,logical_point=self.logical_point,
            route_index=self.route_index,route=self.b.route_name,columns=self.b.route_columns,
            node_id=n['id'],path=self.path,data=data))
        env_status='ok';env_error='';environment_read_utc=''
        try:
            if self.stop.is_set():raise Stopped('停止请求；保存已完成采集，跳过环境回读')
            self.b.service();env=self.clean_environment(self.b.snapshot())
            if env['invalid_channels']:
                env_status='partial';env_error='无效环境通道: '+', '.join(env['invalid_channels'])
            environment_read_utc=datetime.now(timezone.utc).isoformat()
            self.last_environment=env;self.last_poll=time.monotonic()
            self.emit('environment',env)
        except Stopped:
            env_status='stopped';env_error='用户停止，跳过环境回读；已完成的电压仍保留'
            env={key:None for key in ('temperature','vti','field','pressure','angle')}
            self.emit('stale',None);self.log(env_error)
        except Exception as exc:
            env_status='unavailable';env_error=f'{type(exc).__name__}: {exc}'
            env={key:None for key in ('temperature','vti','field','pressure','angle')}
            self.emit('stale',None)
            self.log('电压已保存；本点环境信息不可用：'+env_error)
        self.points+=1
        row=dict(point=self.points,time_utc=datetime.now(timezone.utc).isoformat(),mode='SIMULATED' if self.b.simulated else 'HARDWARE',node_id=n['id'],kind=n['kind'],path=self.path,route=self.b.route_name,columns=json.dumps(self.b.route_columns),temperature_k=env['temperature'],vti_k=env['vti'],field_t=env['field'],pressure_mbar=env['pressure'],angle_deg=env['angle'])
        row['temperature_setpoint_k']=getattr(self.b,'temp_target',None)
        row.update(data)
        row['measurement_mode']=n['params'].get('mode',n['kind'])
        row['environment_timing']='after_point'
        row['environment_read_utc']=environment_read_utc
        if self.environment_overrides and env_status in ('ok','partial'):
            env_status='accepted_by_operator' if env_status=='ok' else 'partial_accepted_by_operator'
        row['environment_overrides']=json.dumps(self.environment_overrides,ensure_ascii=False)
        row['environment_status']=env_status;row['environment_error']=env_error
        row.setdefault('measurement_status','valid')
        row['angle_read_time_unix']=env.get('angle_read_time')
        row['logical_point']=self.logical_point;row['route_index']=self.route_index;row['acquisition_index']=self.points
        # Raw instrument replies, algorithm settings, and all PXI channels are retained.
        self.event('point',row)
        self.record_table(row)
        self.emit('point',row)

    def record_table(self,row):
        """One acquisition point per row, one voltage column per logical channel."""
        new_row=self.table_point!=row['logical_point']
        if new_row:
            self.table_point=row['logical_point']
            self.table_position=self.file.tell()
            self.table_row={k:row.get(k,'') for k in self.base_fields}
            self.table_states={}
            if self.c['enable_matrix'] and self.c['matrix_each_point']:
                self.table_row['route']='逐点电极表（各列对应电极）'
                self.table_row['columns']='';self.table_row['node_id']=row['node_id']
                self.table_row['environment_timing']='first_route_after_point'
                self.table_row['route_index']='';self.table_row['acquisition_index']=''
            self.table_row['point']=row['logical_point']
        if self.continuous_pxi:
            channels=row['channels']
            for ch in self.continuous_pxi['daq_channels']:
                result=channels[ch] if ch in channels else channels[str(ch)]
                for suffix,key in pxi_voltage_columns(self.continuous_pxi.get('harmonic_order',2)):
                    value=result[key]
                    self.table_row[f'ai{ch}_{suffix}']='NULL' if value is None else value
        elif self.c['enable_matrix'] and self.c['matrix_each_point']:
            self.table_row[self.voltage_fields[row['route_index']-1]]=('NULL' if row['voltage_v'] is None else row['voltage_v'])
            prefix=f"route{row['route_index']}_"
            for key in self.route_detail_fields:self.table_row[prefix+key]=row.get(key,'')
            self.table_states[row['route_index']]=(row['measurement_status'],row['environment_status'],row.get('environment_error',''))
        else:
            self.table_row['voltage_v']=('NULL' if row['voltage_v'] is None else row['voltage_v'])
        if self.c['enable_matrix'] and self.c['matrix_each_point']:
            states=list(self.table_states.values())
            complete=len(states)==len(self.c['point_routes'])
            invalid=sum(m=='invalid' for m,_,_ in states)
            self.table_row['measurement_status']=('incomplete' if not complete else
                'invalid' if invalid==len(states) else 'partial_invalid' if invalid else 'valid')
            env_states={e for _,e,_ in states}
            self.table_row['environment_status']=('incomplete' if not complete else next(iter(env_states)) if len(env_states)==1 else 'partial_unavailable')
            self.table_row['environment_error']='; '.join(f'route{i}: {v[2]}' for i,v in self.table_states.items() if v[2])
            self.table_row['measurement_error']='详见逐电极状态与错误列' if invalid else ''
        else:
            for key in ('environment_status','environment_error','measurement_status','measurement_error'):
                self.table_row[key]=row.get(key,'')
        self.table_row['acquisition_end_utc']=row.get('acquisition_end_utc','')
        # Persist each finished channel immediately; missing channels remain blank.
        self.file.seek(self.table_position)
        self.writer.writerow(self.table_row)
        self.file.truncate();self.file.flush();os.fsync(self.file.fileno())
        if new_row:self.csv_rows+=1

    def execute(self,nodes,prefix=''):
        for ix,n in enumerate(nodes,1):
            if not n['enabled']:continue
            self.path=f'{prefix}/{ix}:{n["kind"]}';self.gate()
            self.emit('node',n['id']);self.emit('progress',self.path)
            self.event('node_start',{'id':n['id'],'path':self.path,'params':n['params']})
            k=n['kind'];p=n['params'];path=self.path
            if k in ('temperature','field','pressure','angle'):self.environment(k,p['target'],p)
            elif k=='wait':self.wait(p['seconds'])
            elif k=='route':self.route(p)
            elif k=='repeat':
                for i in range(p['count']):
                    self.gate();self.execute(n['children'],path+f'[{i+1}]')
            elif k=='sweep':
                for target in p['values']:
                    self.gate();self.environment(p['axis'],target,p)
                    self.execute(n['children'],path+f'[{p["axis"]}={target}]')
            elif k=='channels':
                if not self.c['enable_matrix']:
                    self.b.route_name='直接接线';self.b.route_columns=[]
                    self.log('矩阵关闭：电极循环子序列仅按直接接线执行一次')
                    self.execute(n['children'],path+'[direct]')
                else:
                    for r in p['routes']:
                        self.gate();self.route(r);self.execute(n['children'],path+f'[{r["name"]}:{r["columns"]}]')
            else:
                check_currents(k,p,self.c)
                self.measurement(n)
            self.event('node_done',{'id':n['id'],'path':path})
    PXI_APPLY_KEYS=('current_a','frequency_hz','marker_phase_deg','harmonic_order',
                    'daq_device','daq_fs_hz','daq_duration_s','daq_range_v','daq_channels','daq_save_waveforms')

    def _pxi_changed(self,p):
        """Only parameters consumed by backend.begin() can force a reconfigure.

        Validation keeps everything except current_a identical across a PXI run,
        so in practice only the current triggers this path (D-024).
        """
        if self.pxi_applied is None:return True
        return any(self.pxi_applied.get(k)!=p.get(k) for k in self.PXI_APPLY_KEYS)

    def _pxi_per_step_current(self,nodes):
        currents=[]
        def walk(items):
            for n in items:
                if not n['enabled']:continue
                if n['kind']=='resistance' and n['params'].get('mode')=='pxi':currents.append(n['params']['current_a'])
                walk(n['children'])
        walk(nodes)
        return len(set(currents))>1

    def measurement(self,n):
        k=n['kind'];p=n['params']
        if p['mode']=='pxi':
            if self._pxi_changed(p):
                self.retry_io('准备测量',lambda:self.b.begin(k,p))
                self.pxi_applied=copy.deepcopy(p)
                self.log(f'6221 交流激励切换为 {p["current_a"]:g} A（波形关闭后重配）')
                self.event('pxi_reconfigure',dict(current_a=p['current_a'],path=self.path))
            for j in range(p['samples']):
                self.gate();self.logical_point+=1;self.route_index=0
                data=self.acquire_delta(n,p['current_a'])
                self.record(n,data);self.check()
                if j<p['samples']-1:self.wait(p['interval_s'])
            return
        per_point=self.c['enable_matrix'] and self.c['matrix_each_point']
        try:
            if not per_point:
                self.retry_io('准备测量',lambda:self.b.begin(k,p))
                self.measurement_restart=lambda:self.b.begin(k,p)
            for j in range(p['samples']):
                bias=p['current_a']
                self.logical_point+=1
                if per_point:
                    for ri,r in enumerate(self.c['point_routes'],1):
                        self.route_index=ri;self.gate();self.route(r);self.check()
                        try:
                            self.retry_io('准备测量',lambda:self.b.begin(k,p))
                            data=self.acquire_delta(n,bias)
                            self.record(n,data)
                        finally:
                            if not self.stop.is_set():self.retry_io('关闭电流源',self.b.off)
                        self.check()
                else:
                    self.route_index=0;self.gate(restart=lambda:self.b.begin(k,p))
                    data=self.acquire_delta(n,bias);self.record(n,data);self.check()
                if j<p['samples']-1:self.wait(p['interval_s'])
        finally:
            self.measurement_restart=None
            if not self.stop.is_set():self.retry_io('关闭电流源',self.b.off)

    def run(self,nodes,output):
        nodes=copy.deepcopy(nodes)
        normalize_pxi_channels(nodes)
        validate(nodes,self.c,hardware=not self.b.simulated)
        validate_device_selection(nodes,self.c)
        if self.c['measurement_device']!=self.b.measurement_device:
            raise ValueError('运行配置与已连接测量设备不一致；请断开后重新连接')
        self.continuous_pxi=pxi_run_params(nodes);self.pxi_applied=None
        self.invalid_points=0;self.points=0;self.csv_rows=0;self.logical_point=0;self.route_index=0
        self.last_environment=None;self.last_poll=0
        self.required_environment=set();self.environment_overrides={};self.consecutive_invalid=0
        self.emit('quality',dict(total=0,consecutive=0))
        self.run_dir=Path(output).expanduser().resolve()/(datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8])
        self.run_dir.mkdir(parents=True,exist_ok=False)
        manifest={'schema_version':VERSION,'software':'Oxford Sequence Studio 1.7.9','planned_workload':workload(nodes,self.c),'simulation':self.b.simulated,'config':self.c,'nodes':nodes,'status':'running'}
        manifest_path=self.run_dir/'run.json'
        manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        self.base_fields=['point','time_utc','temperature_setpoint_k','temperature_k','field_t','angle_deg','current_a',
            'route','columns','path','node_id','measurement_mode','measurement_status',
            'environment_status','environment_error','acquisition_start_utc','acquisition_end_utc',
            'vti_k','pressure_mbar','environment_read_utc','environment_timing','measurement_error','logical_point','route_index','acquisition_index','angle_read_time_unix','environment_overrides']
        if self.continuous_pxi:
            self.voltage_fields=[f'ai{ch}_{suffix}' for ch in self.continuous_pxi['daq_channels'] for suffix,_ in pxi_voltage_columns(self.continuous_pxi.get('harmonic_order',2))]
        elif self.c['enable_matrix'] and self.c['matrix_each_point']:
            self.voltage_fields=[]
            for r in self.c['point_routes']:
                base='V_'+'-'.join(format_pins(x, '+') for x in r['columns'])
                name=base;occurrence=1
                while name in self.voltage_fields:
                    occurrence+=1;name=f'{base}_{occurrence}'
                self.voltage_fields.append(name)
        else:
            self.voltage_fields=['voltage_v']
        self.route_detail_fields=('acquisition_index','angle_read_time_unix','time_utc','acquisition_start_utc','acquisition_end_utc','environment_read_utc',
            'temperature_k','vti_k','field_t','pressure_mbar','angle_deg','current_a',
            'measurement_status','measurement_error','environment_status','environment_error','environment_overrides')
        details=[f'route{i}_{key}' for i in range(1,len(self.c['point_routes'])+1) for key in self.route_detail_fields] if self.c['enable_matrix'] and self.c['matrix_each_point'] else []
        self.fields=self.base_fields+self.voltage_fields+details
        if self.continuous_pxi:
            h=self.continuous_pxi.get('harmonic_order',2)
            manifest['voltage_columns']={}
            for ch in self.continuous_pxi['daq_channels']:
                for order in (1,h):
                    for component,suffix in (('amplitude','V'),('X','X_V'),('Y','Y_V')):
                        key=f'ai{ch}_'+(f'{order}f_' if order!=1 else '')+suffix
                        manifest['voltage_columns'][key]=dict(channel=ch,harmonic=order,component=component,unit='V',amplitude='peak')
        elif self.c['enable_matrix'] and self.c['matrix_each_point']:
            manifest['voltage_columns']={key:dict(route,detail_prefix=f'route{i}_')
                for i,(key,route) in enumerate(zip(self.voltage_fields,self.c['point_routes']),1)}
        else:
            manifest['voltage_columns']={'voltage_v':dict(kind='row_route',identity_columns=['route','columns','path','node_id'])}
        manifest['counter_semantics']={'points':'legacy alias of acquisition_count','acquisition_index':'completed acquisition number, in events and routeN_ columns','logical_point':'one sample across its electrode combinations','csv_row_count':'logical-point rows whose initial CSV write/flush/fsync completed; may include incomplete electrode rows','data.csv point':'legacy alias of logical_point'}
        manifest['environment_columns_note']='逐点电极表的公共环境列属于首组电极；各组实际环境与状态以 routeN_ 列为准。'
        if self.continuous_pxi:
            manifest['pxi_contract']=copy.deepcopy(self.continuous_pxi)
            manifest['pxi_per_step_current']=self._pxi_per_step_current(nodes)
            self.log(f"4462 固定同步采集 ai0–ai3；每通道 1f/{self.continuous_pxi.get('harmonic_order',2)}f 的幅值、X、Y 均写入 data.csv")
            if manifest['pxi_per_step_current']:self.log('本次序列包含不同的激励电流：将在各步骤边界重配 6221（波形短暂关闭后重开）')
            if self.continuous_pxi.get('daq_coupling')=='ac' and self.continuous_pxi['frequency_hz']<50:
                self.log(f"提示：AC 耦合是 3.4 Hz 高通，当前基频 {self.continuous_pxi['frequency_hz']:g} Hz 下 1f/2f/3f 衰减与相移各不相同（13 Hz 时 1f 约 −3.3%/+14.7°）；如要精确谐波幅相，请用 DC 或把频率提到 100 Hz 以上，并用标准电阻重新标定")
        self.b.raw_directory=self.run_dir/'waveforms'
        if hasattr(self.b,'invalidate_measurement_cache'):self.b.invalidate_measurement_cache()
        self.active=True;self.state='running';self.emit('state','运行中');self.emit('run_dir',str(self.run_dir))
        status='completed';error='';failure_trace='';cleanup=[];cleanup_traces=[]
        self.table_row=None;self.table_position=None;self.table_point=None
        try:
            with (self.run_dir/'events.jsonl').open('w',encoding='utf-8') as journal, (self.run_dir/'data.csv').open('w+',encoding='utf-8-sig',newline='') as f:
                self.file=f;self.journal=journal;self.writer=csv.DictWriter(f,fieldnames=self.fields);self.writer.writeheader();f.flush()
                try:
                    self.check()
                    self.log('本次运行测量设备：'+('2182A Delta（通过6221控制）' if self.c['measurement_device']=='2182a' else 'PXI-4462（6221交流激励）'))
                    if self.continuous_pxi:
                        self.pxi_applied=copy.deepcopy(self.continuous_pxi)
                        self.retry_io('准备连续采集',lambda:self.b.begin('resistance',self.pxi_applied))
                    self.execute(nodes)
                except Stopped as e:status='stopped';error=str(e)
                except Exception as e:
                    status='failed';error=f'{type(e).__name__}: {e}';failure_trace=traceback.format_exc()
                    self.event('failure',dict(error=error,traceback=failure_trace,path=self.path));self.log(error)
                finally:
                    try:self.b.off()
                    except Exception as e:cleanup.append(str(e));cleanup_traces.append(traceback.format_exc())
                    if status!='completed':
                        try:self.b.hold()
                        except Exception as e:cleanup.append('磁场HOLD失败: '+str(e));cleanup_traces.append(traceback.format_exc())
                    if cleanup:status='cleanup_failed';self.log('; '.join(cleanup))
                    self.event('run_end',dict(status=status,error=error,traceback=failure_trace,cleanup_errors=cleanup,cleanup_tracebacks=cleanup_traces,points=self.points,acquisition_count=self.points,csv_row_count=self.csv_rows,path=self.path))
        except Exception as e:
            status='failed';error=f'{type(e).__name__}: {e}';failure_trace=traceback.format_exc()
            try:self.b.off()
            except Exception as ce:cleanup.append(str(ce));cleanup_traces.append(traceback.format_exc())
            try:self.b.hold()
            except Exception as ce:cleanup.append(str(ce));cleanup_traces.append(traceback.format_exc())
            if cleanup:status='cleanup_failed'
            self.emit('log',error)
        finally:
            self.file=self.journal=None;self.active=False;self.state=status
            manifest.update(status=status,error=error,cleanup_errors=cleanup,traceback=failure_trace,cleanup_tracebacks=cleanup_traces,points=self.points,acquisition_count=self.points,csv_row_count=self.csv_rows,invalid_points=self.invalid_points)
            tmp=manifest_path.with_suffix('.tmp');tmp.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(manifest_path)
            self.emit('state',{'completed':(f'已完成（{self.invalid_points} 个无效读数已写 NULL）' if self.invalid_points else '已完成'),'stopped':'已停止','failed':'运行失败','cleanup_failed':'清理失败，请检查仪器'}.get(status,status))
        return manifest
