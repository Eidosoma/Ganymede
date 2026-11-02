#!/usr/bin/env python3
"""probes/smoke_task.py [--import mod1,mod2] [--matmul N]: a five-minute GPU smoke test for one task of a packed node.

Run it as one GPU task of a spec (examples/specs/smoke-node.json runs four, one per GPU). It checks that exactly one device is
visible (the launcher sets CUDA_VISIBLE_DEVICES per task), that the process runs on the cores the launcher pinned (PACK_CORES), that
the named modules import (your lane's package, its heavy dependencies), and times an FP32 and a BF16 matmul on the card. Prints one
JSON line; exit 0 only if every check passed. Writes nothing. Needs PyTorch.

Every lane should be able to prove its environment on a new machine with a test like this before its first real job."""
import argparse, importlib, json, os, sys, time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--import', dest='mods', default='', help='comma-separated modules that must import, e.g. mypkg,transformers')
    ap.add_argument('--matmul', type=int, default=8192, help='matrix size for the timed matmuls')
    a = ap.parse_args()
    out = dict(host=os.uname().nodename, slot=os.environ.get('PACK_SLOT'), cuda_visible=os.environ.get('CUDA_VISIBLE_DEVICES'),
               cores=sorted(os.sched_getaffinity(0)), threads=os.environ.get('OMP_NUM_THREADS'), checks={})
    ok = True

    def check(name, cond, detail=None):
        nonlocal ok
        out['checks'][name] = bool(cond) if detail is None else [bool(cond), detail]
        ok = ok and bool(cond)

    mods = [m.strip() for m in a.mods.split(',') if m.strip()]
    try:
        for m in mods:
            importlib.import_module(m)
        check('imports', True, mods)
    except Exception as e:
        check('imports', False, (type(e).__name__ + ': ' + str(e))[:200])
    import torch
    out.update(torch=torch.__version__, python=sys.version.split()[0], executable=sys.executable)
    check('one_visible_device', torch.cuda.is_available() and torch.cuda.device_count() == 1,
          torch.cuda.device_count() if torch.cuda.is_available() else 'no cuda')
    if os.environ.get('PACK_CORES'):
        check('affinity_matches_launcher', os.environ['PACK_CORES'] == ','.join(map(str, out['cores'])), os.environ['PACK_CORES'])
    if torch.cuda.is_available():
        p = torch.cuda.get_device_properties(0)
        out.update(device=p.name, sm=str(p.major) + '.' + str(p.minor), memory_gb=round(p.total_memory / 2**30, 1))
        n = a.matmul
        c = None
        for dtype, name in ((torch.float32, 'fp32'), (torch.bfloat16, 'bf16')):
            x = torch.randn(n, n, device='cuda', dtype=dtype)
            y = torch.randn(n, n, device='cuda', dtype=dtype)
            torch.cuda.synchronize(); (x @ y).sum().item()
            torch.cuda.synchronize(); t = time.perf_counter(); iters = 10
            for _ in range(iters):
                c = x @ y
            torch.cuda.synchronize(); dt = (time.perf_counter() - t) / iters
            out[name + '_matmul_tflops'] = round(2 * n**3 / dt / 1e12, 1)
        check('gpu_matmul_finite', torch.isfinite(c).all().item())
        v = torch.randn(1000, device='cuda')
        check('gpu_cpu_roundtrip', torch.allclose(v.cpu().cuda(), v))
    out['ok'] = ok
    print(json.dumps(out))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
