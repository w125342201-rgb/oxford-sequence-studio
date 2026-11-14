"""Recover long-form points from durable events; never overwrite data.csv.
Usage: python audit/recover_points.py /path/to/run/events.jsonl /path/to/recovered.csv
Incomplete trailing JSON lines are ignored. Environment values are never invented.
"""
import csv
import json
from pathlib import Path
import sys

def recover(source, destination):
    source,destination=Path(source),Path(destination)
    rows={}
    for line in source.read_text(encoding='utf-8').splitlines():
        try:event=json.loads(line)
        except json.JSONDecodeError:continue
        kind=event.get('event');data=event.get('data',{})
        if kind=='acquisition':
            row={k:v for k,v in data.items() if k!='data'}
            row.update(data['data']);row['environment_status']='unavailable'
            rows[row['point']]=row
        elif kind=='point':rows[data['point']]=data
    flat=[]
    for _,row in sorted(rows.items()):
        item={k:row.get(k,'') for k in ('point','acquisition_index','angle_read_time_unix','logical_point','route_index','route','columns','path','node_id',
            'time_utc','acquisition_start_utc','acquisition_end_utc','temperature_k','vti_k','pressure_mbar','field_t','angle_deg',
            'environment_read_utc','environment_timing','measurement_mode','measurement_error',
            'environment_overrides','current_a','voltage_v','measurement_status','environment_status','environment_error')}
        for key,value in item.items():
            if isinstance(value,(list,dict)):item[key]=json.dumps(value,ensure_ascii=False)
        for ch,result in row.get('channels',{}).items():
            for key in ('v_peak_v','x_v','y_v','v2_peak_v','x2_v','y2_v','v3_peak_v','x3_v','y3_v'):
                suffix={'v_peak_v':'V','x_v':'X_V','y_v':'Y_V','v2_peak_v':'2f_V','x2_v':'2f_X_V','y2_v':'2f_Y_V','v3_peak_v':'3f_V','x3_v':'3f_X_V','y3_v':'3f_Y_V'}[key]
                if key in result:item[f'ai{ch}_{suffix}']=result[key]
        flat.append(item)
    fields=list(dict.fromkeys(k for row in flat for k in row)) or ['point']
    with destination.open('x',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(flat)
    return len(flat)

if __name__=='__main__':
    if len(sys.argv)!=3:raise SystemExit(__doc__)
    print(f'Recovered {recover(*sys.argv[1:])} acquisition points')
