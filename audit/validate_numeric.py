"""Independent analytic inputs, calling BOTH archived and production estimators.
Run from project root: OPENBLAS_NUM_THREADS=1 python audit/validate_numeric.py
Not a DAQ/source hardware test. Units: peak V unless explicitly stated.
"""
import importlib.util
import json
import math
import sys
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "legacy"))
from pxi4462.pxi4462_lockin import fit_harmonics

spec = importlib.util.spec_from_file_location("original", Path(__file__).with_name("original_lockin_reference.py"))
old = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = old
spec.loader.exec_module(old)

def case(name, f=13., fs=51200., duration=1., a=1e-3, b=1e-8,
         phi1=31., phi2=-127., dc=.01, df=0., mains=0., noise=0.,
         clip=None):
    t = np.arange(round(fs*duration))/fs
    # Oracle is a continuous-time expression sampled directly, no driver simulator.
    y = dc + a*np.sin(2*np.pi*(f+df)*t+np.deg2rad(phi1))
    y += b*np.sin(4*np.pi*(f+df)*t+np.deg2rad(phi2))
    y += mains*np.sin(2*np.pi*50.13*t+.43)
    y += np.random.default_rng(62214462).normal(0,noise,len(t))
    if clip is not None:
        y = np.clip(y, -clip, clip)
    before = [old.digital_lockin(y,fs,h*f,1.) for h in (1,2)]
    after = fit_harmonics(y,fs,[f,2*f],1.,[0.,0.])
    expected = [a*np.exp(1j*np.deg2rad(phi1)), b*np.exp(1j*np.deg2rad(phi2))]
    result = dict(name=name, f=f, fs=fs, N=len(t), df=df,
                  expected_peak_v=[a,b], noise_std_v=noise, mains_peak_v=mains)
    for label, values in (("original",before),("corrected",after)):
        result[label]=[dict(x_v=r.x_v,y_v=r.y_v,amp_v=r.v_peak_v,
            vector_error_v=abs(complex(r.x_v,r.y_v)-truth),
            relative_vector_error=abs(complex(r.x_v,r.y_v)-truth)/abs(truth))
            for r,truth in zip(values,expected)]
    result["condition"]=after[0].fit_condition
    result["window_enbw_hz"]=after[0].window_enbw_hz
    return result

def run():
    cases = [
        case("13_cycles_weak_2f"),
        case("one_cycle_low_frequency",f=1.,fs=4000.,duration=1.),
        case("noninteger_13p37_cycles",f=13.37,fs=4000.,duration=1.),
        case("fractional_sample_cycle", f=13.37,fs=51200.,duration=round(13*51200/13.37)/51200),
        case("short_1p3_cycles",f=13.,fs=4000.,duration=.1),
        case("frequency_offset_20ppm",df=13*20e-6),
        case("frequency_offset_100ppm",df=13*100e-6),
        case("mains_50p13Hz_1mV",mains=.001),
        case("white_noise_5uV",noise=5e-6),
        case("asymmetric_clipping",dc=.0004,clip=.0008),
    ]
    # Monte Carlo vs analytic covariance of the ACTUAL weighted estimator.
    fs=4000.;f=13.;n=4000;t=np.arange(n)/fs
    A=np.column_stack([np.ones(n),np.sin(2*np.pi*f*t),np.cos(2*np.pi*f*t),
                       np.sin(4*np.pi*f*t),np.cos(4*np.pi*f*t)])
    w=np.hanning(n)
    C=np.linalg.solve(A.T@(w[:,None]*A),A.T*w)
    predicted=5e-6*np.sqrt(np.sum(C*C,axis=1))[1:]
    rng=np.random.default_rng(830)
    samples=[]
    for _ in range(200):
        r=fit_harmonics(rng.normal(0,5e-6,n),fs,[f,2*f],1,[0,0])
        samples.append([r[0].x_v,r[0].y_v,r[1].x_v,r[1].y_v])
    empirical=np.std(samples,axis=0,ddof=1)
    # Strict deterministic assertions: no relaxed simulator agreement.
    for name in ("13_cycles_weak_2f","one_cycle_low_frequency",
                 "noninteger_13p37_cycles","fractional_sample_cycle","short_1p3_cycles"):
        c=next(c for c in cases if c["name"]==name)
        assert max(r["vector_error_v"] for r in c["corrected"]) < 1e-14, c
    assert np.all(abs(empirical/predicted-1)<.2)
    return dict(hardware_test=False,cases=cases,noise_trials=200,
                noise_predicted_std_v=predicted.tolist(),
                noise_empirical_std_v=empirical.tolist(),
                noise_empirical_over_predicted=(empirical/predicted).tolist())

if __name__=="__main__":
    print(json.dumps(run(),indent=2,ensure_ascii=False))
