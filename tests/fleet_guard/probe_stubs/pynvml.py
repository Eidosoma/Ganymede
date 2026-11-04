"""Stand-in NVML for the probe tests: the GPUs of STUB_GPUS (a JSON list: util, uuid, pci per GPU), no driver needed. Only what
gpm_sampler.py calls."""
import json, os
_G = json.loads(os.environ.get('STUB_GPUS') or '[{"util": 90}, {"util": 50}, {"util": 25}, {"util": 0}]')
NVML_GPM_METRICS_GET_VERSION = 1
_S = {}
def _uuid(i): return _G[i].get('uuid', 'GPU-%08d-1111-2222-3333-444444444444' % i)
def nvmlInit(): pass
def nvmlDeviceGetCount(): return len(_G)
def nvmlDeviceGetHandleByIndex(i): return i
def nvmlGpmSampleAlloc(): return object()
def nvmlGpmSampleGet(h, s): _S[id(s)] = h
class _Metric:
    def __init__(self): self.metricId, self.value, self.nvmlReturn = 0, 0.0, 0
class c_nvmlGpmMetricsGet_t:
    def __init__(self): self.version, self.numMetrics, self.sample1, self.sample2 = 0, 0, None, None; self.metrics = [_Metric() for _ in range(16)]
def nvmlGpmMetricsGet(m):
    u = float(_G[_S[id(m.sample2)]].get('util', 0))
    for k in range(m.numMetrics): m.metrics[k].value, m.metrics[k].nvmlReturn = u, 0
class _Util:
    def __init__(self, g): self.gpu = g
def nvmlDeviceGetUtilizationRates(h): return _Util(int(_G[h].get('util', 0)))
def nvmlDeviceGetUUID(h): return _uuid(h)
class _Pci:
    def __init__(self, b): self.busId = b
def nvmlDeviceGetPciInfo(h): return _Pci(_G[h].get('pci', '00000000:%02X:00.0' % (h + 1)))
def nvmlDeviceGetName(h): return 'stub GPU %d' % h
