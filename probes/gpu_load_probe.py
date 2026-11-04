#!/usr/bin/env python3
"""Known-truth load for calibrating the GPU activity metrics: on this task's GPU, alternate a matmul burst and idle sleep at a
registered duty cycle for --seconds, then print what nvidia-smi and GPM saw on that GPU. Usage: gpu_load_probe.py --duty 0.5 --seconds 120
The sampler reads every GPU; this task's own is found by its UUID (torch's, or CUDA_VISIBLE_DEVICES when that names one), never by its
place in a list: CUDA's device numbers and NVML's need not agree (taking the first GPU's readings for a task on
another is the mistake this avoids). Without a UUID an index counts only under CUDA_DEVICE_ORDER=PCI_BUS_ID, NVML's own order; otherwise no reading is
claimed for the task, and every GPU's is printed."""
import argparse, json, os, subprocess, sys, time


def norm(u):
    """A GPU UUID as NVML ('GPU-...') and torch (without the prefix) give it, in one form."""
    u = (u or '').strip().lower()
    for p in ('gpu-', 'mig-'):
        if u.startswith(p):
            u = u[len(p):]
    return u


def select(summary_line, uuid=None, visible=None, order=None):
    """(NVML index, how it was found) of this task's GPU in the sampler's summary line, or (None, why it could not be)."""
    devices = summary_line.get('devices') or {}
    if uuid:
        hits = [i for i, d in devices.items() if norm(d.get('uuid')) == norm(uuid)]
        if len(hits) == 1:
            return hits[0], 'uuid'
    first = (visible or '').split(',')[0].strip()
    if first.lower().startswith(('gpu-', 'mig-')):
        hits = [i for i, d in devices.items() if norm(d.get('uuid')) == norm(first)]
        if len(hits) == 1:
            return hits[0], 'uuid in CUDA_VISIBLE_DEVICES'
    if first.isdigit() and order == 'PCI_BUS_ID' and first in (summary_line.get('summary') or {}):
        return first, 'index, CUDA_DEVICE_ORDER=PCI_BUS_ID'
    return None, ('not identified: no GPU UUID matched' + (' torch\'s ' + repr(uuid) if uuid else ' (torch gave none)')
                  + ', and CUDA_VISIBLE_DEVICES=' + repr(visible) + ' names no UUID'
                  + ('' if order == 'PCI_BUS_ID' else ' with CUDA_DEVICE_ORDER not PCI_BUS_ID'))


def main():
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
    uuid = str(getattr(torch.cuda.get_device_properties(0), 'uuid', '') or '') or None
    idx, how = select(summ, uuid, os.environ.get("CUDA_VISIBLE_DEVICES"), os.environ.get("CUDA_DEVICE_ORDER"))
    ident = (summ.get("devices") or {}).get(idx) or {}
    res = dict(task=os.environ.get("PACK_TASK"), cuda_visible=os.environ.get("CUDA_VISIBLE_DEVICES"), registered_duty=a.duty, bursts=bursts,
               measured=summ["summary"][idx] if idx is not None else None, nvml_index=idx, device_found_by=how,
               device_uuid=ident.get("uuid"), device_pci_bus_id=ident.get("pci_bus_id"), samples=summ["samples"], device=torch.cuda.get_device_name(0))
    if idx is None:
        res["all_devices"] = summ["summary"]
    print(json.dumps(res))


if __name__ == "__main__":
    main()
