"""Tk desktop GUI: UI thread owns widgets; one worker owns every instrument."""
import copy
from collections import deque
from itertools import islice
import json
import math
import os
import queue
import threading
import time
import weakref
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk,filedialog,messagebox
from pathlib import Path
from .model import CONFIG,SPECS,LABELS,CONTAINERS,VERSION,node,clone,span,validate,workload,validate_config,example,upgrade_document,selected_mode,measurement_node
from .model import parse_pins, format_pins, set_measurement_device, validate_device_selection
from .backend import SimBackend,HardwareBackend
from .engine import Engine
from .instrument_options import OPTIONS,display_value,stored_value,wiring_hint,validate_discrete,visible_fields

CONFIG_LABELS={
 'vti_host':'VTI 控制器 IP','probe_host':'Probe 控制器 IP','ips_host':'iPS 控制器 IP','port':'Oxford TCP 端口','io_timeout_s':'通信超时 / s',
 'source_address':'6221 VISA 地址',
 'matrix_resource':'矩阵 GPIB 地址（例如 GPIB0::20::INSTR）',
 'matrix_each_point':'每个采集点遍历电极表','point_routes':'逐点电极组合列表',
 'matrix_rows':'矩阵接口 [I+, I−, V+, V−]；1–6 对应 A–F，默认 ABCD','rotator_port':'旋转杆串口',
 'measurement_device':'测量设备（二选一）',
 'auto_pressure_enabled':'启用自动气压（总开关）','auto_pressure_low_mbar':'低温区气压 / mbar（低于9.5 K）','auto_pressure_high_mbar':'高温区气压 / mbar（高于10.5 K）',
 'enable_source':'连接6221','enable_matrix':'连接矩阵','enable_rotator':'连接旋转杆',
 'min_temperature_k':'最低温度 / K','max_temperature_k':'最高温度 / K','max_field_t':'最大 |B| / T',
 'max_field_rate':'最大磁场速率 / T/min','max_temperature_rate':'最大温度速率 / K/min','max_pressure_mbar':'最高气压 / mbar',
 'min_angle_deg':'最小角度 / °','max_angle_deg':'最大角度 / °','max_rotation_speed':'最大旋转速度 / °/s',
 'max_current_a':'最大固定测量激励 / A','compliance_v':'电流源 Compliance / V',
 'matrix_off_delay_s':'确认断电后 → 开始切换的等待 / s','matrix_settle_s':'切换完成后 → 允许通电的等待 / s','rotation_margin_s':'旋转预计时间后的静默余量 / s',
 'max_expanded_steps':'序列最大执行操作数（环境设定/等待/路由/采集）',
}

def format_value(v):
    if isinstance(v,list) and all(isinstance(x,(int,float)) for x in v):return ', '.join(str(x) for x in v)
    if isinstance(v,(list,dict)):return json.dumps(v,ensure_ascii=False)
    return str(v)

def format_environment_value(key, value):
    if value is None:return '—'
    if isinstance(value,(float,int)):
        digits={'temperature':4,'vti':4,'field':4,'angle':1,'pressure':2}.get(key,5)
        return f'{value:.{digits}f}'
    return str(value)

def display_readings(row,harmonic=1,component="X"):
    """Expand one simultaneous acquisition into separately labelled traces."""
    channels=row.get('channels')
    if channels:
        for channel, result in channels.items():
            name=str(channel).rsplit('/',1)[-1]
            if not name.startswith('ai'):name='ai'+name
            route=row.get('route','')
            label=name if route in ('','直接接线') else f'{route} / {name}'
            key=({'X':f'x{harmonic}_v','Y':f'y{harmonic}_v','幅值':f'v{harmonic}_peak_v'} if harmonic in (2,3) else {'X':'x_v','Y':'y_v','幅值':'v_peak_v'})[component]
            yield label,result.get(key)
    else:
        yield row.get('route','直接接线'),row.get('voltage_v')

def parse_value(raw,default):
    if isinstance(default,str):return raw.strip()
    if isinstance(default,list):
        raw=raw.strip()
        if not raw:return []
        if not raw.startswith('['):raw='['+raw+']'
        return json.loads(raw)
    if isinstance(default,int):
        v=float(raw)
        if not v.is_integer():raise ValueError('需要整数')
        return int(v)
    return float(raw)

def configure_tick_checkboxes(root,style):
    """Theme-independent tick indicator; retain native ttk keyboard/state handling."""
    size=max(16,tkfont.nametofont('TkDefaultFont').metrics('linespace'))
    images=[]
    for selected,disabled in [(False,False),(True,False),(False,True),(True,True)]:
        icon=tk.PhotoImage(master=root,width=size+6,height=size)
        border='#a5adb5' if disabled else '#697986'
        ink='#a5adb5' if disabled else '#087c52'
        icon.put(border,to=(0,0,size,size))
        icon.put('#eeeeee' if disabled else '#ffffff',to=(1,1,size-1,size-1))
        if selected:
            # Paint a check mark rather than relying on the theme's X glyph.
            points=[(.20,.51),(.41,.73),(.80,.23)]
            radius=max(1,size//15)
            for (ax,ay),(bx,by) in zip(points,points[1:]):
                steps=size*2
                for i in range(steps+1):
                    x=round(size*(ax+(bx-ax)*i/steps))
                    y=round(size*(ay+(by-ay)*i/steps))
                    icon.put(ink,to=(x-radius,y-radius,x+radius+1,y+radius+1))
        images.append(icon)
    root._checkbox_images=images
    off,on,disabled_off,disabled_on=images
    style.element_create('Tick.indicator','image',off,
                         ('disabled','selected',disabled_on),
                         ('disabled',disabled_off),('selected',on),sticky='')
    style.layout('TCheckbutton',[
        ('Checkbutton.padding',{'sticky':'nswe','children':[
            ('Tick.indicator',{'side':'left','sticky':''}),
            ('Checkbutton.focus',{'side':'left','sticky':'w','children':[
                ('Checkbutton.label',{'sticky':'nswe'})]})]})])

def enable_mousewheel(canvas,region):
    """One application-wide handler, with per-panel lifecycle cleanup."""
    root=canvas._root()
    if not hasattr(root,'_scroll_panels'):
        root._scroll_panels=weakref.WeakKeyDictionary()
        def wheel(event):
            widget=event.widget
            for target,region_ref in list(root._scroll_panels.items()):
                region=region_ref()
                current=widget
                while current is not None:
                    if current is target or (region is not None and current is region):
                        delta=getattr(event,'delta',0)
                        num=getattr(event,'num',None)
                        if num not in (4,5) and not delta:return
                        direction=1 if num==5 or (isinstance(delta,(int,float)) and delta<0) else -1
                        try:target.yview_scroll(direction,'units')
                        except tk.TclError:root._scroll_panels.pop(target,None)
                        return 'break'
                    current=getattr(current,'master',None)
        for sequence in ('<MouseWheel>','<Button-4>','<Button-5>'):
            root.bind_all(sequence,wheel,add='+')
    first=canvas not in root._scroll_panels
    root._scroll_panels[canvas]=weakref.ref(region)
    if first:
        def cleanup(event):
            if event.widget is canvas:root._scroll_panels.pop(canvas,None)
        canvas.bind('<Destroy>',cleanup,add='+')



class RouteEditor(tk.Toplevel):
    def __init__(self,parent,routes):
        super().__init__(parent);self.title('电极组合表');self.geometry('720x480');self.minsize(560,400);self.result=None
        self.routes=copy.deepcopy(routes)
        b=ttk.Frame(self);b.pack(side='bottom',fill='x',padx=14,pady=12)
        ttk.Button(b,text='取消',command=self.destroy).pack(side='right',padx=5)
        ttk.Button(b,text='确定并关闭',command=self.finish,style='Primary.TButton').pack(side='right',padx=5)
        for label,fn in [('添加新组合',self.add),('更新选中组合',self.replace),('删除选中组合',self.delete)]:ttk.Button(b,text=label,command=fn).pack(side='left',padx=4)
        f=ttk.Frame(self);f.pack(side='bottom',fill='x',padx=10)
        self.tree=ttk.Treeview(self,columns=('name','ip','im','vp','vm'),show='headings',height=8)
        for key,label in zip(self.tree['columns'],['名称','I+列','I−列','V+列','V−列']):
            self.tree.heading(key,text=label);self.tree.column(key,width=90,minwidth=40)
        sy=ttk.Scrollbar(self,orient='vertical',command=self.tree.yview);sy.pack(side='right',fill='y')
        sx=ttk.Scrollbar(self,orient='horizontal',command=self.tree.xview);sx.pack(side='bottom',fill='x')
        self.tree.configure(yscrollcommand=sy.set,xscrollcommand=sx.set)
        self.tree.pack(fill='both',expand=True,padx=10,pady=10)
        self.vars=[tk.StringVar(value=v) for v in ['通道',12,9,11,10]]
        for i,(v,label) in enumerate(zip(self.vars,['名称','I+','I−','V+','V−'])):
            f.columnconfigure(i,weight=1);ttk.Label(f,text=label).grid(row=0,column=i)
            ttk.Entry(f,textvariable=v,width=5).grid(row=1,column=i,sticky='ew',padx=2)
        ttk.Label(f,text='每口可填多个 pin，例如 12,11；这些 pin 将电气并联。每个 pin 只能属于一个口。').grid(row=2,column=0,columnspan=5,sticky='w',pady=6)
        self.tree.bind('<<TreeviewSelect>>',self.select);self.refresh()
        self.transient(parent);self.grab_set();self.wait_window()
    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for i,r in enumerate(self.routes):self.tree.insert('', 'end',iid=str(i),values=[r['name']]+[format_pins(x) for x in r['columns']])
    def select(self,event=None):
        s=self.tree.selection()
        if s:
            r=self.routes[int(s[0])]
            for v,x in zip(self.vars,[r['name']]+[format_pins(x) for x in r['columns']]):v.set(x)
    def value(self):
        from .model import columns
        r={'name':self.vars[0].get(),'columns':[parse_pins(v.get()) for v in self.vars[1:]]};columns(r['columns']);return r
    def add(self):
        try:self.routes.append(self.value());self.refresh()
        except Exception as e:messagebox.showerror('电极格式',str(e),parent=self)
    def replace(self):
        try:
            s=self.tree.selection()
            if s:self.routes[int(s[0])]=self.value();self.refresh()
        except Exception as e:messagebox.showerror('电极格式',str(e),parent=self)
    def delete(self):
        s=self.tree.selection()
        if s:self.routes.pop(int(s[0]));self.refresh()
    def finish(self):
        if not self.routes:return
        self.result=self.routes;self.destroy()

class Form(tk.Toplevel):
    def __init__(self,parent,title,params,labels=None,validator=None):
        super().__init__(parent);self.title(title);self.geometry('820x650');self.result=None
        self.params=copy.deepcopy(params);self.fields={};self.field_widgets={};self.validator=validator
        labels=dict(labels or LABELS)
        if 'mode' in params:
            labels['timeout_s']='单次Delta采集超时 / s（超时本次记NULL后继续）'
        axis=params.get('axis') or next((k for k in ('temperature','field','pressure','angle') if SPECS[k][0] in title),None)
        self.axis=axis
        if axis and 'axis' not in params:
            unit={'temperature':'K','field':'T','pressure':'mbar','angle':'°'}[axis]
            labels['target']='目标值 / '+unit;labels['tolerance']='稳定容差 / '+unit
            labels['rate']='变化速率 / '+('°/s' if axis=='angle' else unit+'/min')
        if axis=='field':
            labels['tolerance']='磁场实际回读容差 / T（不是FSET校验容差；小步进未启动且未到位将报错）'
        self.geometry(f'{min(820,self.winfo_screenwidth()-60)}x{min(650,self.winfo_screenheight()-100)}')
        buttons=ttk.Frame(self);buttons.pack(side='bottom',fill='x',padx=18,pady=12)
        ttk.Button(buttons,text='取消',command=self.destroy).pack(side='right',padx=5)
        ttk.Button(buttons,text='应用设置',command=self.save,style='Primary.TButton').pack(side='right')
        outer=ttk.Frame(self);outer.pack(fill='both',expand=True,padx=14,pady=10)
        canvas=tk.Canvas(outer,highlightthickness=0);scroll=ttk.Scrollbar(outer,orient='vertical',command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set);scroll.pack(side='right',fill='y');canvas.pack(side='left',fill='both',expand=True)
        inner=ttk.Frame(canvas);wid=canvas.create_window((0,0),window=inner,anchor='nw')
        inner.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda e:canvas.itemconfigure(wid,width=e.width))
        inner.columnconfigure(0,weight=1)
        wrapped=[]
        def resize(e):
            canvas.itemconfigure(wid,width=e.width)
            for label in wrapped:label.configure(wraplength=max(100,e.width-30))
        canvas.bind('<Configure>',resize)
        enable_mousewheel(canvas,inner)
        for row,(k,v) in enumerate(params.items()):
            row*=2
            label=ttk.Label(inner,text=labels.get(k,k),wraplength=600);wrapped.append(label)
            label.grid(row=row,column=0,columnspan=2,sticky='w',padx=5,pady=(9,2))
            if isinstance(v,bool):
                var=tk.BooleanVar(value=v);ttk.Checkbutton(inner,variable=var).grid(row=row+1,column=0,sticky='w')
            else:
                var=tk.StringVar(value=display_value(k,v) if k in OPTIONS else format_value(v))
                choices=[label for label,_ in OPTIONS[k]] if k in OPTIONS else {'axis':['temperature','field','angle']}.get(k)
                ent=ttk.Label(inner,textvariable=var) if k in ('mode','daq_channels') else ttk.Combobox(inner,textvariable=var,values=choices,state='readonly') if choices else ttk.Entry(inner,textvariable=var)
                if k=='daq_channels':var.set('[0, 1, 2, 3]')
                ent.grid(row=row+1,column=0,sticky='ew',padx=5)
                if k=='axis':ent.bind('<<ComboboxSelected>>',self.axis_defaults)
                if k in ('phase2_deg','phase3_deg'):
                    ttk.Button(inner,text='应用热测量默认相位',command=self.thermal_phase_defaults).grid(row=row+1,column=1,padx=5)
                if k in ('values','temperature_values','angle_values','field_values'):
                    ttk.Button(inner,text='生成点列',command=lambda x=var,key=k:self.generate(x,key)).grid(row=row+1,column=1,padx=5)
                if k in ('routes','point_routes'):ttk.Button(inner,text='编辑电极表',command=lambda x=var:self.routes(x)).grid(row=row+1,column=1,padx=5)
            self.fields[k]=var
            self.field_widgets[k]=[w for w in inner.grid_slaves() if int(w.grid_info()['row']) in (row,row+1)]
        def update_visibility(*args):
            current=dict(self.params)
            for key in ('measurement_device','harmonic_order'):
                if key in self.fields:current[key]=stored_value(key,self.fields[key].get())
            if 'mode' in self.fields:current['mode']=self.fields['mode'].get()
            visible=visible_fields(current)
            for key,widgets in self.field_widgets.items():
                for widget in widgets:
                    if key in visible and key!='delta_divisor':widget.grid()
                    else:widget.grid_remove()
        for key in ('measurement_device','mode','harmonic_order'):
            if key in self.fields:self.fields[key].trace_add('write',update_visibility)
        update_visibility()
        if 'mode' in params or 'voltage_range_v' in params or 'measurement_device' in params:
            hint=ttk.Label(inner,foreground='#36556b',wraplength=600);wrapped.append(hint)
            hint.grid(row=len(params)*2,column=0,columnspan=2,sticky='ew',pady=16)
            def update_hint(*args):
                mode=self.fields['mode'].get() if 'mode' in self.fields else 'delta'
                try:
                    if 'measurement_device' in self.fields:
                        device=stored_value('measurement_device',self.fields['measurement_device'].get())
                        mode='delta' if device=='2182a' else 'pxi'
                    hint.configure(text=wiring_hint(mode))
                except ValueError:hint.configure(text='请选择受支持的测量模式。')
            for key in ('mode','measurement_device'):
                if key in self.fields:self.fields[key].trace_add('write',update_hint)
            update_hint()
        self.transient(parent);self.grab_set();self.wait_window()
    def thermal_phase_defaults(self):
        for key,value in (('marker_phase_deg',90),('phase_deg',0),('phase2_deg',-90),('phase3_deg',-180)):
            if key in self.fields:self.fields[key].set(str(value))

    def axis_defaults(self,event=None):
        axis=self.fields['axis'].get()
        defaults=SPECS[axis][1]
        for k in ('rate','tolerance','stable_s','timeout_s'):
            if k in self.fields and k in defaults:self.fields[k].set(format_value(defaults[k]))
        self.fields['values'].set({'temperature':'4, 4.2, 4.4','field':'0, 0.02, 0.04','angle':'0, 45, 90'}[axis])
    def generate(self,var,key='values'):
        axis=key.removesuffix('_values') if key!='values' else self.fields['axis'].get() if 'axis' in self.fields else self.axis
        precision={'temperature':'0.1 K','field':'0.001 T（1 mT）','angle':'0.1°'}[axis]
        d=Form(self,'生成扫描点列 · 精度 '+precision,{'start':0.0,'stop':1.0,'step':0.1,'include_stop':True},{'start':'起点（对应物理单位，须符合点列精度）','stop':'终点（须符合点列精度）','step':'正步长（自动判断方向，须符合点列精度）','include_stop':'包含终点（最后间隔可能较短；取消则严格等间距）'})
        if d.result:
            try:var.set(format_value(span(**d.result,axis=axis)))
            except Exception as e:messagebox.showerror('点列',str(e),parent=self)
    def routes(self,var):
        try:
            d=RouteEditor(self,json.loads(var.get()))
            if d.result:var.set(json.dumps(d.result,ensure_ascii=False))
        except Exception as e:messagebox.showerror('电极表',str(e),parent=self)
    def save(self):
        try:
            current=copy.deepcopy(self.params)
            for key in ('mode','measurement_device','harmonic_order'):
                if key in self.fields:current[key]=stored_value(key,self.fields[key].get()) if key in OPTIONS else self.fields[key].get()
            visible=visible_fields(current)-{'delta_divisor'}
            result=copy.deepcopy(self.params)
            for k,v in self.fields.items():
                if k in visible:result[k]=stored_value(k,v.get()) if k in OPTIONS else v.get() if isinstance(self.params[k],bool) else parse_value(v.get(),self.params[k])
            validate_discrete(result)
            if self.validator:self.validator(result)
            self.result=result;self.destroy()
        except Exception as e:messagebox.showerror('参数检查',str(e),parent=self)

class App(tk.Tk):
    def __init__(self):
        super().__init__();self.title('Oxford Sequence Studio 1.7.9 · 统一输运测量')
        self.geometry(f'{min(1500,self.winfo_screenwidth()-60)}x{min(930,self.winfo_screenheight()-100)}');self.minsize(800,500)
        self.config_data=copy.deepcopy(CONFIG);self.nodes=example();self.config_path=None
        self.events=queue.Queue();self.jobs=queue.Queue();self.backend=None;self.engine=None
        self.busy=False;self.connected=False;self.closing=False;self.dirty=False;self.data=deque(maxlen=20000);self.node_map={}
        self.matrix_enabled=tk.BooleanVar(value=self.config_data['enable_matrix']);self.matrix_each=tk.BooleanVar(value=self.config_data['matrix_each_point'])
        self.mode=tk.StringVar(value='模拟');self.status=tk.StringVar(value='未连接');self.progress=tk.StringVar(value='载入示例：温度 → 磁场 → 电极组合 → 固定激励电阻')
        self.output=tk.StringVar(value=str(Path.cwd()/'data'));self.run_path=tk.StringVar(value='尚未运行')
        self.env_vars={k:tk.StringVar(value='—') for k in ['temperature','vti','pressure','field','angle','magnet_action']}
        self.route_var=tk.StringVar(value='直接接线 / 尚无矩阵命令')
        self.component=tk.StringVar(value='X')
        self.harmonic=tk.StringVar(value='一次谐波 1f')
        self.xaxis=tk.StringVar(value='field_t');self.group=tk.StringVar(value='全部')
        self.quality_text=tk.StringVar(value='无效 0 / 连续 0')
        self.latest_voltages=tk.StringVar(value='最新电压：—')
        self.build();self.sync_device_options();self.refresh_tree();self.protocol('WM_DELETE_WINDOW',self.close_window)
        threading.Thread(target=self.worker,daemon=True).start();self.after(80,self.drain)
    def build(self):
        style=ttk.Style(self);style.theme_use('clam')
        default_font=tkfont.nametofont('TkDefaultFont');default_font.configure(family='Microsoft YaHei UI',size=10)
        text_font=tkfont.nametofont('TkTextFont');text_font.configure(family='Microsoft YaHei UI',size=10)
        configure_tick_checkboxes(self,style)
        row_height=max(26,default_font.metrics('linespace')+6)
        style.configure('Treeview',rowheight=row_height,font=default_font)
        style.configure('TButton',padding=(7,3),font=default_font)
        style.configure('Primary.TButton',padding=(8,3),font=('Microsoft YaHei UI',10,'bold'),foreground='#075f6b')
        style.configure('Stop.TButton',padding=(8,3),font=('Microsoft YaHei UI',10,'bold'),foreground='#a32121')
        style.configure('TLabelframe.Label',font=('Microsoft YaHei UI',10,'bold'))
        style.configure('Title.TLabel',font=('Microsoft YaHei UI',18,'bold'));style.configure('Value.TLabel',font=('Consolas',12,'bold'),foreground='#0b7285')
        menu=tk.Menu(self)
        file_menu=tk.Menu(menu,tearoff=False)
        for label,fn in [('新建序列',self.new_sequence),('打开序列',self.load_sequence),('保存序列',self.save_sequence),('选择数据目录',self.choose_output),('打开本次数据',self.open_output)]:
            file_menu.add_command(label=label,command=fn)
        menu.add_cascade(label='文件与数据',menu=file_menu)
        menu.add_command(label='使用说明',command=self.help)
        self.configure(menu=menu)
        bar=ttk.Frame(self,padding=(8,4));bar.pack(fill='x')
        self.mode_box=ttk.Combobox(bar,textvariable=self.mode,values=['模拟','真实硬件'],state='readonly',width=8)
        self.mode_box.pack(side='left',padx=(0,5))
        for label,fn in [('设备配置',self.configure_devices),('连接',self.connect),('断开',self.disconnect)]:
            ttk.Button(bar,text=label,command=fn,width=7).pack(side='left',padx=2)
        ttk.Separator(bar,orient='vertical').pack(side='left',fill='y',padx=6)
        for label,fn,style_name in [('校验',self.check_sequence,'TButton'),('运行',self.start,'Primary.TButton'),('暂停/继续',self.pause_run,'TButton'),('停止运行',self.stop_run,'Stop.TButton')]:
            ttk.Button(bar,text=label,command=fn,style=style_name,width=8).pack(side='left',padx=2)
        status=ttk.Frame(self,padding=(10,0,10,3));status.pack(fill='x')
        status.columnconfigure(1,weight=1)
        ttk.Label(status,textvariable=self.status,foreground='#075f6b').grid(row=0,column=0,sticky='w',padx=(0,12))
        ttk.Label(status,textvariable=self.progress,width=1,anchor='w').grid(row=0,column=1,sticky='ew')
        self.quality_label=ttk.Label(status,textvariable=self.quality_text)
        self.quality_label.grid(row=0,column=2,sticky='e')
        # Reserve the footer; long paths never increase its height.
        footer=ttk.Frame(self,padding=(8,2));footer.pack(side='bottom',fill='x')
        ttk.Label(footer,text='数据目录').pack(side='left')
        ttk.Entry(footer,textvariable=self.output,width=12).pack(side='left',fill='x',expand=True,padx=5)
        ttk.Button(footer,text='选择',command=self.choose_output).pack(side='left')
        ttk.Button(footer,text='打开本次数据',command=self.open_output).pack(side='left',padx=4)
        body=ttk.Panedwindow(self,orient='horizontal');body.pack(fill='both',expand=True,padx=8)
        sidebar=ttk.Frame(body,width=285);body.add(sidebar,weight=0)
        side_canvas=tk.Canvas(sidebar,width=265,highlightthickness=0)
        side_scroll=ttk.Scrollbar(sidebar,orient='vertical',command=side_canvas.yview)
        side_canvas.configure(yscrollcommand=side_scroll.set)
        side_scroll.pack(side='right',fill='y');side_canvas.pack(side='left',fill='both',expand=True)
        left=ttk.Frame(side_canvas);side_id=side_canvas.create_window((0,0),window=left,anchor='nw')
        left.bind('<Configure>',lambda e:side_canvas.configure(scrollregion=side_canvas.bbox('all')))
        side_canvas.bind('<Configure>',lambda e:side_canvas.itemconfigure(side_id,width=e.width))
        enable_mousewheel(side_canvas,left)
        device_panel=ttk.LabelFrame(left,text='测量设备（二选一）',padding=6);device_panel.pack(fill='x',pady=(0,5))
        self.device=tk.StringVar(value=display_value('measurement_device',self.config_data['measurement_device']))
        self.device_box=ttk.Combobox(device_panel,textvariable=self.device,values=[x[0] for x in OPTIONS['measurement_device']],state='readonly',width=12)
        self.device_box.pack(fill='x');self.device_box.bind('<<ComboboxSelected>>',self.select_device)
        ttk.Label(device_panel,text='全部采集步骤跟随此选择',wraplength=230).pack(anchor='w')
        self.device_session=tk.StringVar(value='未连接')
        ttk.Label(device_panel,textvariable=self.device_session,wraplength=230,foreground='#075f6b').pack(anchor='w')
        self.vti_link=tk.BooleanVar(value=self.config_data['link_vti_temperature'])
        ttk.Checkbutton(left,text='联动 VTI 控温（关闭：仅 Probe）',variable=self.vti_link,command=self.vti_link_options).pack(anchor='w',pady=(0,5))
        self.pressure_summary=tk.StringVar(value='自动气压：'+('开' if self.config_data['auto_pressure_enabled'] else '关'))
        thermal=ttk.Frame(left);thermal.pack(fill='x',pady=(0,5))
        ttk.Label(thermal,textvariable=self.pressure_summary).pack(side='left')
        ttk.Button(thermal,text='自动气压设置…',command=self.pressure_settings).pack(side='right')
        env=ttk.LabelFrame(left,text='Oxford 实时状态',padding=6);env.pack(fill='x')
        for k,label,unit in [('temperature','样品温度','K'),('vti','VTI 温度','K'),('pressure','交换气压力','mbar'),('field','磁场','T'),('angle','旋转杆角度','°')]:
            f=ttk.Frame(env);f.pack(fill='x',pady=3)
            ttk.Label(f,text=label,width=10).pack(side='left');ttk.Label(f,textvariable=self.env_vars[k],style='Value.TLabel').pack(side='left');ttk.Label(f,text=unit).pack(side='right')
        ttk.Label(env,textvariable=self.env_vars['magnet_action']).pack(anchor='w',pady=5)
        ttk.Label(env,text='温压磁场约1秒刷新；角度为最近静止回读（不轮询）',wraplength=230,foreground='#587081').pack(anchor='w')
        controls=ttk.LabelFrame(left,text='手动控制',padding=8);controls.pack(fill='x',pady=5)
        for k in ['temperature','pressure','field','angle']:
            ttk.Button(controls,text=SPECS[k][0]+'…',command=lambda x=k:self.manual(x)).pack(fill='x',pady=2)

        route=ttk.LabelFrame(left,text='当前电极命令',padding=6);route.pack(fill='x')
        ttk.Checkbutton(route,text='启用矩阵开关',variable=self.matrix_enabled,command=self.matrix_options).pack(anchor='w')
        ttk.Checkbutton(route,text='每点测完全部电极组合',variable=self.matrix_each,command=self.matrix_options).pack(anchor='w')
        ttk.Button(route,text='编辑逐点电极表',command=self.point_routes).pack(fill='x',pady=4)
        ttk.Button(route,text='接口与切换延时…',command=self.matrix_settings).pack(fill='x',pady=2)
        ttk.Label(route,textvariable=self.route_var,wraplength=240).pack(anchor='w')
        ttk.Button(route,text='手动切换四端电极…',command=lambda:self.manual('route')).pack(fill='x',pady=8)
        ttk.Label(left,text='运行时锁定手动控制和方案编辑。\n暂停可在环境等待中关闭电流；采集中在点边界暂停。\n停止不等于硬件急停；旋转杆原协议无停止命令。',wraplength=250,foreground='#74634a').pack(fill='x',pady=12)
        right_holder=ttk.Frame(body);body.add(right_holder,weight=1)
        split=ttk.Panedwindow(right_holder,orient='vertical');split.pack(fill='both',expand=True)
        self.after(100,lambda:split.sashpos(0,int(split.winfo_height()*0.55)) if split.winfo_exists() else None)
        right=ttk.Frame(split);split.add(right,weight=3)
        display=ttk.Frame(split);split.add(display,weight=2)
        actions=ttk.Frame(right,padding=(3,2));actions.pack(fill='x')
        actions.columnconfigure(0,weight=1)
        self.kind=tk.StringVar(value='扫描环境参数')
        self.kind_box=ttk.Combobox(actions,textvariable=self.kind,values=[v[0] for k,v in SPECS.items() if k!='channels'],state='readonly',width=12)
        self.kind_box.grid(row=0,column=0,sticky='ew',padx=(0,4))
        for col,(label,fn) in enumerate([('插入同级',lambda:self.insert(False)),('加入循环',lambda:self.insert(True)),('循环模板',self.loop_template)],1):
            ttk.Button(actions,text=label,command=fn).grid(row=0,column=col,padx=2)
        edits=ttk.Frame(right,padding=(3,2));edits.pack(fill='x')
        for label,fn in [('编辑',self.edit),('复制',self.duplicate),('删除',self.delete),('上移',lambda:self.move(-1)),('下移',lambda:self.move(1))]:
            ttk.Button(edits,text=label,command=fn,width=6).pack(side='left',padx=2)
        more=ttk.Menubutton(edits,text='层级 / 启用',width=12)
        more_menu=tk.Menu(more,tearoff=False)
        for label,fn in [('增加缩进 →',self.indent),('减少缩进 ←',self.outdent),('启用 / 禁用',self.toggle)]:
            more_menu.add_command(label=label,command=fn)
        more.configure(menu=more_menu);more.pack(side='left',padx=3)
        self.detail=tk.StringVar()
        ttk.Label(right,textvariable=self.detail,width=1,anchor='w',foreground='#526477').pack(side='bottom',fill='x',padx=4,pady=2)
        treewrap=ttk.Frame(right);treewrap.pack(fill='both',expand=True)
        self.tree=ttk.Treeview(treewrap,columns=('params','state'),selectmode='browse',height=8)
        self.tree.heading('#0',text='测量序列（双击编辑）');self.tree.heading('params',text='参数');self.tree.heading('state',text='启用')
        self.tree.column('#0',width=295);self.tree.column('params',width=510);self.tree.column('state',width=55,stretch=False)
        sx=ttk.Scrollbar(treewrap,orient='horizontal',command=self.tree.xview);sx.pack(side='bottom',fill='x')
        sy=ttk.Scrollbar(treewrap,orient='vertical',command=self.tree.yview);self.tree.configure(yscrollcommand=sy.set,xscrollcommand=sx.set)
        sy.pack(side='right',fill='y');self.tree.pack(side='left',fill='both',expand=True)
        self.tree.tag_configure('disabled',foreground='#999999');self.tree.tag_configure('active',background='#d8f0ed')
        self.tree.bind('<Double-1>',lambda e:self.edit());self.tree.bind('<Delete>',lambda e:self.delete())
        self.tree.bind('<<TreeviewSelect>>',self.summary)
        nb=ttk.Notebook(display);nb.pack(fill='both',expand=True,pady=(3,0))
        plot=ttk.Frame(nb);logs=ttk.Frame(nb);nb.add(plot,text='电压曲线');nb.add(logs,text='运行日志')
        axes=ttk.Frame(plot,padding=(5,2));axes.pack(fill='x')
        ttk.Label(axes,text='横轴').pack(side='left',padx=(0,4))
        combo=ttk.Combobox(axes,textvariable=self.xaxis,values=['logical_point','acquisition_index','temperature_k','field_t','angle_deg'],state='readonly',width=16)
        combo.pack(side='left');combo.bind('<<ComboboxSelected>>',lambda e:self.draw())
        self.harmonic_box=harmonic_box=ttk.Combobox(axes,textvariable=self.harmonic,values=['一次谐波 1f','二次谐波 2f','三次谐波 3f'],state='readonly',width=13)
        harmonic_box.pack(side='left',padx=6);harmonic_box.bind('<<ComboboxSelected>>',lambda e:self.draw())
        self.component_box=component_box=ttk.Combobox(axes,textvariable=self.component,values=['X','Y','幅值'],state='readonly',width=5)
        component_box.pack(side='left');component_box.bind('<<ComboboxSelected>>',lambda e:self.draw())
        ttk.Label(axes,text='电压 V',foreground='#075f6b').pack(side='left',padx=12)
        ttk.Label(axes,text='通道 / 电极').pack(side='left',padx=4)
        self.groups=ttk.Combobox(axes,textvariable=self.group,values=['全部'],state='readonly',width=10)
        self.groups.pack(side='left',fill='x',expand=True);self.groups.bind('<<ComboboxSelected>>',lambda e:self.draw())
        ttk.Label(plot,textvariable=self.latest_voltages,foreground='#075f6b',anchor='w').pack(fill='x',padx=8,pady=2)
        self.canvas=tk.Canvas(plot,background='#fbfcfd',height=180,highlightthickness=0);self.canvas.pack(fill='both',expand=True);self.canvas.bind('<Configure>',lambda e:self.draw())
        self.sync_plot_mode()
        self.log_text=tk.Text(logs,height=6,wrap='word',font=('Consolas',10));self.log_text.pack(fill='both',expand=True);self.log_text.configure(state='disabled')
    def idle_required(self):
        if self.busy:messagebox.showinfo('正在运行','请等待结束或停止当前任务。',parent=self);return False
        return True
    def selected(self):
        s=self.tree.selection();return self.find(s[0]) if s else None
    def find(self,nid,items=None,parent=None):
        items=self.nodes if items is None else items
        for i,n in enumerate(items):
            if n['id']==nid:return items,i,parent,n
            r=self.find(nid,n['children'],n)
            if r:return r
    def refresh_tree(self,select=None):
        self.tree.delete(*self.tree.get_children());self.node_map={}
        def insert(items,parent=''):
            for n in items:
                p=n['params'];visible=visible_fields(p)-{'delta_divisor'};brief='; '.join(f'{k}={format_value(v)}' for k,v in p.items() if k in visible)
                self.tree.insert(parent,'end',iid=n['id'],text=(('PXI-4462' if p.get('mode')=='pxi' else '2182A '+p.get('mode','').upper())+' 电压采集') if n['kind']=='resistance' else SPECS[n['kind']][0],values=(brief,'✓' if n['enabled'] else '—'),open=True,tags=() if n['enabled'] else ('disabled',))
                self.node_map[n['id']]=n;insert(n['children'],n['id'])
        insert(self.nodes)
        if select and select in self.node_map:self.tree.selection_set(select);self.tree.see(select)
    def summary(self,event=None):
        s=self.selected()
        self.detail.set('' if not s else SPECS[s[3]['kind']][0]+' | '+'; '.join(f'{LABELS.get(k,k)}: {format_value(v)}' for k,v in list(s[3]['params'].items())[:4]))
    def insert(self,child):
        if not self.idle_required():return
        kind=next(k for k,v in SPECS.items() if v[0]==self.kind.get());n=measurement_node(self.config_data) if kind=='resistance' else node(kind);kind=n['kind']
        d=Form(self,SPECS[kind][0],n['params'])
        if d.result is None:return
        n['params']=d.result;s=self.selected()
        if child:
            if not s or s[3]['kind'] not in CONTAINERS:messagebox.showinfo('子步骤','先选中扫描、重复或电极循环节点。');return
            s[3]['children'].append(n)
        elif s:s[0].insert(s[1]+1,n)
        else:self.nodes.append(n)
        self.dirty=True;self.refresh_tree(n['id'])
    def edit(self):
        if not self.idle_required():return
        s=self.selected()
        if not s:return
        n=s[3];d=Form(self,SPECS[n['kind']][0],n['params'])
        if d.result is not None:n['params']=d.result;self.dirty=True;self.refresh_tree(n['id'])
    def duplicate(self):
        if not self.idle_required():return
        s=self.selected()
        if s:n=clone(s[3]);s[0].insert(s[1]+1,n);self.dirty=True;self.refresh_tree(n['id'])
    def delete(self):
        if not self.idle_required():return
        s=self.selected()
        if s:s[0].pop(s[1]);self.dirty=True;self.refresh_tree()
    def move(self,delta):
        if not self.idle_required():return
        s=self.selected()
        if s and 0<=s[1]+delta<len(s[0]):
            a,i,_,n=s;a[i],a[i+delta]=a[i+delta],a[i];self.dirty=True;self.refresh_tree(n['id'])
    def indent(self):
        if not self.idle_required():return
        s=self.selected()
        if s and s[1]>0 and s[0][s[1]-1]['kind'] in CONTAINERS:
            n=s[0].pop(s[1]);s[0][s[1]-1]['children'].append(n);self.dirty=True;self.refresh_tree(n['id'])
        else:messagebox.showinfo('缩进','前一个同级步骤必须是循环节点。')
    def outdent(self):
        if not self.idle_required():return
        s=self.selected()
        if s and s[2]:
            parent=self.find(s[2]['id']);n=s[0].pop(s[1]);parent[0].insert(parent[1]+1,n);self.dirty=True;self.refresh_tree(n['id'])
    def toggle(self):
        if not self.idle_required():return
        s=self.selected()
        if s:s[3]['enabled']=not s[3]['enabled'];self.dirty=True;self.refresh_tree(s[3]['id'])
    def sync_device_options(self):
        self.device.set(display_value('measurement_device',self.config_data['measurement_device']))
        self.device_box.configure(state='disabled' if self.connected or self.busy else 'readonly')
        kind='resistance'
        choices=[v[0] for k,v in SPECS.items() if k!='channels']
        self.kind_box.configure(values=choices)
        if self.kind.get() not in choices:self.kind.set(SPECS[kind][0])
    def select_device(self,event=None):
        if self.connected or self.busy:
            self.sync_device_options();messagebox.showinfo('测量设备','请先停止并断开连接后更换测量设备。');return
        changed=set_measurement_device(self.nodes,self.config_data,stored_value('measurement_device',self.device.get()))
        self.sync_device_options();self.sync_plot_mode();self.dirty=True;self.refresh_tree()
        message='已选 '+self.device.get()+f'；全部采集步骤已同步（切换 {changed} 个），原参数保留'
        self.progress.set(message);self.emit('log',message)
    def sync_matrix_options(self):
        self.vti_link.set(self.config_data['link_vti_temperature'])
        self.sync_device_options()
        self.pressure_summary.set('自动气压：'+('开' if self.config_data['auto_pressure_enabled'] else '关'))
        self.sync_plot_mode()
        self.matrix_enabled.set(self.config_data['enable_matrix']);self.matrix_each.set(self.config_data['matrix_each_point'])
    def settings_subset(self,title,keys):
        if not self.idle_required():return
        if self.connected:messagebox.showinfo(title,'请先断开连接后修改。');return
        params={k:copy.deepcopy(self.config_data[k]) for k in keys}
        def check(values):validate_config(dict(self.config_data,**values))
        d=Form(self,title,params,CONFIG_LABELS,check)
        if d.result is not None:
            self.config_data.update(d.result);self.sync_matrix_options();self.dirty=True
    def vti_link_options(self):
        if not self.idle_required():
            self.vti_link.set(self.config_data['link_vti_temperature']);return
        enabled=self.vti_link.get()
        self.config_data['link_vti_temperature']=enabled;self.dirty=True
        if self.connected:self.submit('vti_link',enabled)
        else:self.progress.set('控温模式：'+('Probe + VTI 联动' if enabled else '仅 Probe'))
    def pressure_settings(self):
        self.settings_subset('自动气压：9.5–10.5 K 内保持原状态',('auto_pressure_enabled','auto_pressure_low_mbar','auto_pressure_high_mbar'))
    def matrix_settings(self):
        self.settings_subset('矩阵六接口 A–F：默认 A=I+、B=I−、C=V+、D=V−',('matrix_rows','matrix_off_delay_s','matrix_settle_s'))
    def matrix_options(self):
        if self.busy or self.connected:
            self.sync_matrix_options();messagebox.showinfo('矩阵配置','请先断开连接后更改矩阵开关/逐点模式。');return
        self.config_data['enable_matrix']=self.matrix_enabled.get()
        self.config_data['matrix_each_point']=self.matrix_each.get();self.dirty=True
    def point_routes(self):
        if not self.idle_required():return
        if self.connected:messagebox.showinfo('电极表','请先断开连接后修改电极表。');return
        d=RouteEditor(self,self.config_data['point_routes'])
        if d.result:self.config_data['point_routes']=d.result;self.dirty=True
    def loop_template(self):
        if not self.idle_required():return
        params=dict(order='temperature, angle, field',temperature_values=[4.0],angle_values=[0.0,1.0,2.0],field_values=[0.0,0.05,0.10,0.05,0.0],current_a=SPECS['resistance'][1]['current_a'])
        labels=dict(order='由外到内：temperature, angle, field（可省略）；电极统一在左下角设置',temperature_values='温度 / K',angle_values='角度 / °',field_values='磁场 / T',current_a='固定测量激励 / A（PXI为峰值）',mode='电阻模式')
        d=Form(self,'生成循环 · '+self.device.get()+' / '+selected_mode(self.config_data),params,labels)
        if d.result is None:return
        try:
            p=d.result;order=[v.strip() for v in p['order'].split(',')]
            if not order or len(order)!=len(set(order)) or any(x not in ('temperature','angle','field') for x in order):raise ValueError('循环量只允许温度、角度、磁场且不能重复；电极统一在左下角设置')
            root=measurement_node(self.config_data,current_a=p['current_a']);current=root
            for axis in reversed(order):
                settings={k:v for k,v in SPECS[axis][1].items() if k!='target'}
                outer=node('sweep',axis=axis,values=p[axis+'_values'],**settings)
                outer['children']=[current];current=outer
            validate([current],self.config_data)
            selected=self.selected()
            if selected:selected[0].insert(selected[1]+1,current)
            else:self.nodes.append(current)
            self.dirty=True;self.refresh_tree(current['id'])
            self.progress.set('已插入环境循环；电极遍历统一在左下角设置')
        except Exception as e:messagebox.showerror('循环模板',str(e))
    def configure_devices(self):
        if not self.idle_required():return
        if self.connected:messagebox.showinfo('配置','请先断开连接后修改设备地址及限值。');return
        params={k:v for k,v in self.config_data.items() if k not in ('link_vti_temperature','measurement_device','enable_matrix','matrix_each_point','point_routes','auto_pressure_enabled','auto_pressure_low_mbar','auto_pressure_high_mbar')}
        d=Form(self,'设备连接与实验限值（矩阵启用与遍历在左下角）',params,CONFIG_LABELS,lambda values:validate_config(dict(self.config_data,**values)))
        if d.result:
            self.config_data.update(d.result);self.sync_matrix_options();self.dirty=True
    def new_sequence(self):
        if not self.idle_required():return
        if self.dirty and not messagebox.askyesno('新建','清空当前未保存的序列？'):return
        self.nodes=[];self.dirty=True;self.refresh_tree()
    def load_sequence(self):
        if not self.idle_required():return
        if self.connected:messagebox.showinfo('打开方案','方案包含设备配置，请先断开连接。');return
        if self.dirty and not messagebox.askyesno('尚未保存','打开文件将替换当前编辑内容，继续？'):return
        path=filedialog.askopenfilename(filetypes=[('测量方案','*.json')])
        if not path:return
        try:
            d=upgrade_document(json.loads(Path(path).read_text(encoding='utf-8')))
            if d['schema_version']!=VERSION:raise ValueError('方案版本不兼容')
            validate(d['nodes'],d['config'])
            self.nodes=d['nodes'];self.config_data=d['config'];self.config_path=path;self.dirty=False;self.sync_matrix_options();self.refresh_tree()
            message='已载入方案；测量设备恢复为 '+self.device.get()+'。需要更换时请在左侧重新选择。'
            self.progress.set(message);self.emit('log',message)
            def has_legacy_routes(nodes):
                return any(n['kind']=='channels' or has_legacy_routes(n.get('children',[])) for n in nodes)
            if has_legacy_routes(self.nodes):messagebox.showinfo('旧电极循环保留','此旧方案含显式电极循环，已保留原顺序，不自动改成逐点遍历。新方案请统一使用左下角电极表。旧循环与每点遍历不能同时启用。',parent=self)
            if d.get('migration_notes'):messagebox.showinfo('旧方案导入',d['migration_notes'],parent=self)
        except Exception as e:messagebox.showerror('打开失败',str(e))
    def save_sequence(self):
        path=filedialog.asksaveasfilename(defaultextension='.json',initialfile='measurement_sequence.json',filetypes=[('测量方案','*.json')])
        if not path:return
        try:
            Path(path).write_text(json.dumps(dict(schema_version=VERSION,config=self.config_data,nodes=self.nodes),ensure_ascii=False,indent=2),encoding='utf-8');self.dirty=False;self.config_path=path
        except Exception as e:messagebox.showerror('保存失败',str(e))
    def check_sequence(self):
        try:
            count=validate(self.nodes,self.config_data,hardware=self.mode.get()=='真实硬件')
            validate_device_selection(self.nodes,self.config_data)
            work=workload(self.nodes,self.config_data)
            messagebox.showinfo('校验通过',f'结构、数值范围、电流上限、电极映射通过检查。\n执行操作数：{count}；采集次数：{work["acquisitions"]}；CSV行数：{work["csv_rows"]}。\n操作数包含环境设定、等待、路由及采集，不代表预计机时。\n硬件接线和实际执行仍需现场验证。')
        except Exception as e:messagebox.showerror('校验失败',str(e))
    def submit(self,kind,data=None):
        if not self.idle_required():return
        if kind=='run' and self.engine:
            self.engine.stop.clear();self.engine.pause.clear()
        self.busy=True;self.device_box.configure(state='disabled');self.mode_box.configure(state='disabled')
        self.status.set('处理中…');self.jobs.put((kind,data))
    def connect(self):
        if self.connected or self.busy:return
        try:
            # Read the visible value again; do not rely solely on the Tk event.
            if stored_value('measurement_device',self.device.get())!=self.config_data['measurement_device']:
                self.select_device()
            validate_config(self.config_data)
            validate_device_selection(self.nodes,self.config_data)
        except Exception as e:messagebox.showerror('设备配置',str(e));return
        self.submit('connect',(self.mode.get()=='模拟',copy.deepcopy(self.config_data)))
    def disconnect(self):
        if self.connected:self.submit('disconnect')
    def start(self):
        if not self.connected:messagebox.showinfo('运行','请先连接模拟或真实设备。');return
        if not self.idle_required():return
        try:
            connected_device=self.backend.measurement_device
            if self.config_data['measurement_device']!=connected_device or stored_value('measurement_device',self.device.get())!=connected_device:
                raise ValueError('界面选择与本次连接的测量设备不一致；请断开后重新连接')
            validate(self.nodes,self.config_data,hardware=not self.backend.simulated)
            validate_device_selection(self.nodes,self.config_data)
            output=self.output.get().strip()
            if not output:raise ValueError('请选择数据目录')
            self.data=deque(maxlen=20000);self.group.set('全部');self.groups['values']=['全部'];self.draw();self.submit('run',(copy.deepcopy(self.nodes),output))
        except Exception as e:messagebox.showerror('运行前检查',str(e))
    def manual(self,kind):
        if not self.connected or not self.idle_required():return
        n=node(kind);d=Form(self,'手动 '+SPECS[kind][0],n['params'])
        if d.result is None:return
        n['params']=d.result
        try:
            validate([n],self.config_data,hardware=not self.backend.simulated)
            output=self.output.get().strip()
            if not output:raise ValueError('请选择数据目录')
        except Exception as e:messagebox.showerror('参数检查',str(e));return
        self.data=deque(maxlen=20000);self.submit('run',([n],output))
    def pause_run(self):
        if self.engine and self.engine.active:
            if self.engine.pause.is_set():self.engine.pause.clear()
            else:self.engine.pause.set();self.status.set('请求暂停：等待当前采集/通讯调用返回')
    def stop_run(self):
        if self.engine and self.busy:
            self.engine.stop.set();self.status.set('请求停止：等待驱动返回后关闭输出、HOLD')
    def choose_output(self):
        if not self.idle_required():return
        path=filedialog.askdirectory()
        if path:self.output.set(path)
    def open_output(self):
        path=self.run_path.get()
        if not Path(path).is_dir():return
        if os.name=='nt':os.startfile(path)
        else:messagebox.showinfo('数据目录',path)
    def emit(self,kind,data):self.events.put((kind,data))
    def worker(self):
        last_idle_error=None;last_idle_log=0.
        while True:
            try:kind,data=self.jobs.get(timeout=1)
            except queue.Empty:
                if self.backend:
                    try:
                        self.backend.service();self.emit('environment',self.backend.snapshot())
                        if last_idle_error:self.emit('log','状态刷新已恢复')
                        last_idle_error=None
                    except Exception as e:
                        self.emit('state','读数失败 / 请检查连接');self.emit('stale',None)
                        error=f'{type(e).__name__}: {e}';now=time.monotonic()
                        if error!=last_idle_error or now-last_idle_log>=60:
                            self.emit('log','状态刷新失败: '+error);last_idle_log=now
                        last_idle_error=error
                continue
            try:
                if kind=='connect':
                    sim,c=data;b=(SimBackend if sim else HardwareBackend)(c,lambda m:self.emit('log',m));b.connect()
                    self.backend=b;self.engine=Engine(b,c,self.emit);self.emit('connected',True)
                    self.emit('route',dict(name=b.route_name,columns=b.route_columns))
                    self.emit('environment',b.snapshot());self.emit('state',('模拟已连接：' if sim else '真实连接已建立，待采集校验：')+display_value('measurement_device',b.measurement_device))
                elif kind=='disconnect':
                    errors=self.backend.close();self.backend=None;self.engine=None;self.emit('connected',False)
                    self.emit('state','已断开（关闭状态有错误）' if errors else '已断开')
                    if errors:self.emit('error','断开时清理未完成，请检查仪器：'+'; '.join(errors))
                elif kind=='vti_link':
                    self.backend.c['link_vti_temperature']=data
                    self.engine.c['link_vti_temperature']=data
                    # Switching alone must not apply the previous thermal target.
                    self.backend.temp_target=None
                    self.emit('log','控温模式：'+('Probe + VTI 联动' if data else '仅 Probe')+'；下一次温度命令生效，VTI保留原有设置')
                    self.emit('state','控温模式已切换')
                elif kind=='run':
                    self.engine.run(*data)
                elif kind=='quit':
                    if self.backend:
                        errors=self.backend.close()
                        if errors:self.emit('error','退出时清理未完成，请检查仪器：'+'; '.join(errors))
                    self.emit('quit',None);return
            except Exception as e:self.emit('error',f'{type(e).__name__}: {e}')
            finally:self.emit('idle',None)
    def drain(self):
        redraw=False
        try:
            for _ in range(300):
                kind,data=self.events.get_nowait()
                if kind=='environment':
                    for k,var in self.env_vars.items():
                        var.set(format_environment_value(k,data.get(k)))
                    if data.get('angle_status')=='运动中/尚未确认':self.env_vars['angle'].set('运动中（不查询）')
                    elif data.get('rotation_error'):self.env_vars['angle'].set('角度未确认，请停止后检查旋转杆')
                elif kind=='stale':
                    for var in self.env_vars.values():var.set('读数失效')
                elif kind=='state':self.status.set(data)
                elif kind=='progress':self.progress.set(data)
                elif kind=='quality':
                    self.quality_text.set(f'无效 {data["total"]} / 连续 {data["consecutive"]}')
                    self.quality_label.configure(foreground='#b32121' if data['consecutive']>=10 else '#526477')
                elif kind=='route':self.route_var.set(data['name']+'\n[I+, I−, V+, V−]\n'+str(data['columns']))
                elif kind=='run_dir':self.run_path.set(data)
                elif kind=='connected':
                    self.connected=data;self.mode_box.configure(state='disabled' if data else 'readonly')
                    self.device_box.configure(state='disabled' if data else 'readonly')
                    self.device_session.set(('本次连接选择：'+display_value('measurement_device',self.backend.measurement_device)) if data else '未连接')
                    if not data:
                        self.route_var.set('未连接 / 无有效矩阵状态')
                        for v in self.env_vars.values():v.set('—')
                elif kind=='idle':
                    self.busy=False
                    state='disabled' if self.connected else 'readonly'
                    self.device_box.configure(state=state);self.mode_box.configure(state=state)
                elif kind=='node':
                    for nid,n in self.node_map.items():self.tree.item(nid,tags=('active',) if nid==data else () if n['enabled'] else ('disabled',))
                    if data in self.node_map:self.tree.see(data)
                elif kind=='point':
                    self.data.append(data)
                    redraw=True
                elif kind in ('log','error'):
                    self.log_text.configure(state='normal');self.log_text.insert('end',time.strftime('%H:%M:%S')+' '+data+'\n');self.log_text.see('end')
                    if int(self.log_text.index('end-1c').split('.')[0])>1000:self.log_text.delete('1.0','end-1000l')
                    self.log_text.configure(state='disabled')
                    if kind=='error':self.status.set('操作失败');messagebox.showerror('操作失败',data)
                elif kind=='quit':self.destroy();return
        except queue.Empty:pass
        if redraw:self._plot_dirty=True
        if getattr(self,'_plot_dirty',False) and time.monotonic()-getattr(self,'_last_plot_time',0)>=max(0.25,getattr(self,'_plot_duration',0)*8):
            self._plot_dirty=False;self._last_plot_time=time.monotonic();self.draw()
            self._plot_duration=time.monotonic()-self._last_plot_time
        if self.closing and not self.busy:self.busy=True;self.jobs.put(('quit',None))
        self.after(100,self.drain)
    def sync_plot_mode(self):
        if not hasattr(self,'harmonic_box'):return
        pxi=self.config_data['measurement_device']=='pxi'
        if pxi:
            if self.data and self.data[-1].get('channels'):
                result=next(iter(self.data[-1]['channels'].values()))
                order=result.get('harmonic_order',2)
                choices=['一次谐波 1f',('二次谐波 2f' if order==2 else '三次谐波 3f')]
            else:choices=['一次谐波 1f','二次谐波 2f','三次谐波 3f']
            self.harmonic_box.configure(values=choices)
            if self.harmonic.get() not in choices:self.harmonic.set(choices[0])
            self.harmonic_box.pack(side='left',padx=6,before=self.groups)
            self.component_box.pack(side='left',before=self.groups)
        else:
            self.harmonic_box.pack_forget();self.component_box.pack_forget()
        return pxi
    def draw(self):
        pxi=self.sync_plot_mode()
        harmonic={'二次谐波 2f':2,'三次谐波 3f':3}.get(self.harmonic.get(),1) if hasattr(self,'harmonic') else 1
        component=self.component.get() if hasattr(self,'component') else 'X'
        if pxi is False:harmonic=1;component='X'
        label=f'{harmonic}f {component}' if pxi is not False else 'Delta 电压'
        plot_rows=list(islice(reversed(self.data),3000));plot_rows.reverse()
        if pxi is not None:plot_rows=[r for r in plot_rows if bool(r.get('channels'))==pxi]
        names=sorted({name for r in plot_rows for name,_ in display_readings(r)})
        if hasattr(self,'groups'):self.groups['values']=['全部']+names
        latest=list(display_readings(plot_rows[-1],harmonic,component)) if plot_rows else []
        latest=[(name,value) for name,value in latest if self.group.get()=='全部' or name==self.group.get()]
        self.latest_voltages.set(f'最新 {label} / V：'+('    |    '.join(f'{name}: {value:.6g} V' if isinstance(value,(int,float)) and math.isfinite(value) else f'{name}: NULL' for name,value in latest) if latest else '—'))
        c=self.canvas;c.delete('all');w=c.winfo_width();h=c.winfo_height()
        if w<180 or h<100:return
        xk,yk=self.xaxis.get(),'voltage_v'
        c.create_text(w/2,12,text=f'{label} / V vs {xk}   ·   最近 {len(plot_rows)} 次采集（图限3000次，全部数据写入文件）',fill='#536779')
        groups={}
        for r in plot_rows:
            for name,y in display_readings(r,harmonic,component):
                if self.group.get()!='全部' and name!=self.group.get():continue
                x=r.get(xk)
                if not isinstance(x,(int,float)) or not isinstance(y,(int,float)) or not math.isfinite(x*y):continue
                # Keep each channel and each sweep invocation separate.
                key=(r['path'],name,r['kind']);groups.setdefault(key,[]).append((x,y))
        pts=[p for g in groups.values() for p in g]
        if not pts:c.create_text(w/2,h/2,text='运行序列后显示曲线 · 可选择横轴和通道 / 电极',fill='#789');return
        xmin=min(x for x,y in pts);xmax=max(x for x,y in pts);ymin=min(y for x,y in pts);ymax=max(y for x,y in pts)
        if xmin==xmax:xmin-=max(abs(xmin)*0.05,1e-9);xmax+=max(abs(xmax)*0.05,1e-9)
        if ymin==ymax:ymin-=max(abs(ymin)*0.05,1e-9);ymax+=max(abs(ymax)*0.05,1e-9)
        colors=['#0b7285','#ae5b16','#7255ab','#2e8b57','#c04869']
        route_colors={name:colors[i%len(colors)] for i,name in enumerate(names)}
        visible=sorted({key[1] for key in groups})
        legend_cols=max(1,int((w-100)//180))
        for i,name in enumerate(visible):
            c.create_text(80+(i%legend_cols)*180,30+(i//legend_cols)*18,text=name,anchor='w',fill=route_colors[name])
        x0,y0,x1,y1=80,44+18*((len(visible)-1)//legend_cols),w-25,h-35
        if y1<=y0:return
        for i in range(5):
            f=i/4;x=x0+f*(x1-x0);y=y1-f*(y1-y0)
            c.create_line(x,y0,x,y1,fill='#e4eaf0');c.create_line(x0,y,x1,y,fill='#e4eaf0')
            c.create_text(x,y1+14,text=f'{xmin+f*(xmax-xmin):.3g}',fill='#526477')
            c.create_text(x0-7,y,text=f'{ymin+f*(ymax-ymin):.3g}',anchor='e',fill='#526477')
        for key,ps in groups.items():
            coords=[(x0+(x-xmin)/(xmax-xmin)*(x1-x0),y1-(y-ymin)/(ymax-ymin)*(y1-y0)) for x,y in ps];color=route_colors[key[1]]
            if len(coords)>1:c.create_line(*[z for pair in coords for z in pair],fill=color,width=1.5)
            if len(coords)==1 or len(plot_rows)<=300:
                for x,y in coords:c.create_oval(x-2,y-2,x+2,y+2,fill=color,outline='')
    def help(self):
        messagebox.showinfo('使用说明','1. 默认模拟模式，不访问仪器。连接后可运行示例。\n2. 真实运行前：设备配置中填写地址、启用所需设备并核对实验限值。\n3. 插入步骤加入同级；加入子步骤放入循环。双击修改参数。\n4. 扫描/电极循环/重复可任意嵌套，所有环境扫描均为逐点稳定后测量。\n5. Delta 按 V/I 计算，无额外除2；实时图显示Delta电压。\n6. 暂停可在环境等待中生效，采集中等待本点结束。停止在驱动取消检查后关闭输出并HOLD，不能停止已发出的旋转命令。\n7. 不要同时运行旧GUI争用相同设备。程序退出不会自动升温或退磁。')
    def close_window(self):
        if self.closing:return
        if self.dirty and not messagebox.askyesno('退出','当前方案有未保存修改，仍然退出？'):return
        self.closing=True
        if self.busy:self.stop_run();self.status.set('正在停止并关闭连接，请等待…')
        else:self.busy=True;self.jobs.put(('quit',None))

def main():App().mainloop()
