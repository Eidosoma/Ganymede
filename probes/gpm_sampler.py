#!/usr/bin/env python3
"""GPU activity sampler for the fleet launcher. Reads, per visible GPU, NVML's Hopper GPM counters without any
daemon: SM_UTIL (share of SMs busy, the quantity LLview's "Active SM" is built from), SM_OCCUPANCY, ANY_TENSOR_UTIL, DRAM_BW_UTIL
and GRAPHICS_UTIL, plus the coarse nvidia-smi utilization (utilization.gpu, the share of time any kernel ran). One JSON line per
sample; summary at the end. Usage: gpm_sampler.py --seconds S --period P [--gpus 0,1,2,3] [--out FILE]"""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pynvml as nv
METRICS = {1: "graphics_util", 2: "sm_active", 3: "sm_occupancy", 5: "tensor_active", 10: "dram_bw"}
def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--seconds", type=float, default=60); ap.add_argument("--period", type=float, default=1.0)
    ap.add_argument("--gpus", default=""); ap.add_argument("--out", default=""); a = ap.parse_args()
    nv.nvmlInit(); n = nv.nvmlDeviceGetCount()
    idx = [int(x) for x in a.gpus.split(",") if x.strip()] or list(range(n))
    hs = {i: nv.nvmlDeviceGetHandleByIndex(i) for i in idx}
    def ident(h):   # who each GPU is, so a reading can be matched to a task's GPU by UUID, not by its place
        out = {}
        for key, get in (('uuid', lambda: nv.nvmlDeviceGetUUID(h)), ('pci_bus_id', lambda: nv.nvmlDeviceGetPciInfo(h).busId), ('name', lambda: nv.nvmlDeviceGetName(h))):
            try:
                v = get(); out[key] = v.decode() if isinstance(v, bytes) else str(v)
            except Exception:
                out[key] = None
        return out
    devices = {str(i): ident(hs[i]) for i in idx}
    s1 = {i: nv.nvmlGpmSampleAlloc() for i in idx}; s2 = {i: nv.nvmlGpmSampleAlloc() for i in idx}
    for i in idx: nv.nvmlGpmSampleGet(hs[i], s1[i])
    out = open(a.out, "w") if a.out else sys.stdout; acc = {i: {k: [] for k in list(METRICS.values()) + ["smi_util"]} for i in idx}
    t0 = time.time()
    while time.time() - t0 < a.seconds:
        time.sleep(a.period); row = {"t": round(time.time() - t0, 1)}
        for i in idx:
            nv.nvmlGpmSampleGet(hs[i], s2[i])
            m = nv.c_nvmlGpmMetricsGet_t(); m.version = nv.NVML_GPM_METRICS_GET_VERSION; m.numMetrics = len(METRICS); m.sample1 = s1[i]; m.sample2 = s2[i]
            for k, mid in enumerate(METRICS): m.metrics[k].metricId = mid
            nv.nvmlGpmMetricsGet(m); vals = {}
            for k, (mid, name) in enumerate(METRICS.items()):
                r = m.metrics[k]; vals[name] = round(r.value, 2) if r.nvmlReturn == 0 else None
            vals["smi_util"] = nv.nvmlDeviceGetUtilizationRates(hs[i]).gpu
            row[str(i)] = vals; s1[i], s2[i] = s2[i], s1[i]
            for k, v in vals.items():
                if v is not None: acc[i][k].append(v)
        out.write(json.dumps(row) + "\n"); out.flush()
    summary = {str(i): {k: (round(sum(v) / len(v), 2) if v else None) for k, v in acc[i].items()} for i in idx}
    summary_line = {"summary": summary, "seconds": round(time.time() - t0, 1), "samples": {str(i): len(acc[i]["smi_util"]) for i in idx}, "devices": devices}
    out.write(json.dumps(summary_line) + "\n"); out.flush()
    if a.out: print(json.dumps(summary_line))
if __name__ == "__main__": main()
