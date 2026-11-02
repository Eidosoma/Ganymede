#!/usr/bin/env python3
"""Known-truth load for calibrating the GPU activity metrics: on this task's GPU, alternate a matmul burst and idle sleep at a
registered duty cycle for --seconds, then print what nvidia-smi and GPM saw. Usage: gpu_load_probe.py --duty 0.5 --seconds 120"""
import argparse, json, os, sys, time, threading, subprocess
ap = argparse.ArgumentParser(); ap.add_argument("--duty", type=float, required=True); ap.add_argument("--seconds", type=float, default=120)
ap.add_argument("--period", type=float, default=4.0); ap.add_argument("--n", type=int, default=6144); a = ap.parse_args()
here = os.path.dirname(os.path.abspath(__file__)); out = os.path.join(os.environ.get("PACK_RECEIPT_DIR", "."), f"gpm-{os.environ.get('PACK_TASK','task')}.jsonl")
samp = subprocess.Popen([sys.executable, os.path.join(here, "gpm_sampler.py"), "--seconds", str(a.seconds + 2), "--period", "1", "--out", out])
import torch
dev = torch.device("cuda"); x = torch.randn(a.n, a.n, device=dev, dtype=torch.bfloat16); torch.cuda.synchronize()
t0 = time.time(); bursts = 0
while time.time() - t0 < a.seconds:
    tb = time.time()
    while time.time() - tb < a.period * a.duty:
        y = x @ x; bursts += 1
    torch.cuda.synchronize(); time.sleep(max(0.0, a.period * (1 - a.duty)))
samp.wait()
summ = [json.loads(l) for l in open(out) if l.startswith('{"summary"')][-1]
gpu = summ["summary"][list(summ["summary"])[0]]
print(json.dumps(dict(task=os.environ.get("PACK_TASK"), cuda_visible=os.environ.get("CUDA_VISIBLE_DEVICES"), registered_duty=a.duty, bursts=bursts,
                      measured=gpu, samples=summ["samples"], device=torch.cuda.get_device_name(0))))
