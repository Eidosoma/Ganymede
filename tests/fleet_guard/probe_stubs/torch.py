"""Stand-in torch for the probe tests: CUDA device 0 is the GPU that CUDA_VISIBLE_DEVICES names, counted in CUDA's own order of the
NVML devices (STUB_CUDA_ORDER, NVML indices; the same order by default); its uuid as torch gives it (without NVML's 'GPU-'). No GPU
needed: a matmul is a no-op. STUB_NO_UUID=1: an old torch without the uuid property."""
import json, os
bfloat16 = 'bfloat16'
_G = json.loads(os.environ.get('STUB_GPUS') or '[{"util": 90}, {"util": 50}, {"util": 25}, {"util": 0}]')
_ORDER = [int(x) for x in (os.environ.get('STUB_CUDA_ORDER') or ','.join(str(i) for i in range(len(_G)))).split(',')]
def _nvml_index():
    first = os.environ.get('CUDA_VISIBLE_DEVICES', '0').split(',')[0].strip()
    return _ORDER[int(first)] if first.isdigit() else 0
class device:
    def __init__(self, *a, **k): pass
class _Tensor:
    def __matmul__(self, other): return self
def randn(*a, **k): return _Tensor()
class _Props:
    def __init__(self, i):
        self.name = 'stub GPU %d' % i
        if os.environ.get('STUB_NO_UUID') != '1': self.uuid = _G[i].get('uuid', 'GPU-%08d-1111-2222-3333-444444444444' % i)[4:]
class cuda:
    @staticmethod
    def synchronize(): pass
    @staticmethod
    def get_device_name(d=0): return 'stub GPU %d' % _nvml_index()
    @staticmethod
    def get_device_properties(d=0): return _Props(_nvml_index())
