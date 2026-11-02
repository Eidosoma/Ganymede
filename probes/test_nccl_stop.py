#!/usr/bin/env python3
"""probes/test_nccl_stop.py: the NCCL probe's stopping rule and parameter check, on CPU with gloo and two processes (no GPU, no Slurm).
Ranks whose deadlines differ by seconds leave the training loop together, at the same step, so their DDP collectives stay matched; the
trained parameters are identical bit for bit; and a difference that a checksum of sums cannot see (two weights swapped on one rank) is
caught by the tensor-by-tensor check.
  python3 probes/test_nccl_stop.py        (needs torch; about ten seconds; exit 0 when every check passes)"""
import os, socket, sys, time


def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p


def worker(rank, world, port, q):
    import torch, torch.distributed as dist
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import nccl_test as nt
    torch.set_num_threads(1)
    dist.init_process_group('gloo', init_method=f'tcp://127.0.0.1:{port}', world_size=world, rank=rank)
    torch.manual_seed(0)
    model = torch.nn.Linear(16, 16); ddp = torch.nn.parallel.DistributedDataParallel(model)
    opt = torch.optim.SGD(ddp.parameters(), lr=0.01); g = torch.Generator().manual_seed(10 + rank)

    def step():
        loss = ddp(torch.randn(8, 16, generator=g)).pow(2).mean(); opt.zero_grad(); loss.backward(); opt.step(); time.sleep(0.01)
    t0 = time.time(); deadline = t0 + (0.5 if rank == 1 else 4.0)      # rank 1 reaches its deadline seconds before rank 0
    steps, why = nt.train_until_stop(step, dict(flag=False), deadline, dist, torch.zeros(2), every=5)
    loop_s = time.time() - t0
    identical = nt.tensors_differing(list(model.parameters()), dist)
    with torch.no_grad():
        for p in model.parameters():            # on every rank alike: values on a 2^-10 grid, so any sum of them is exact in double
            p.copy_((p * 1024).round() / 1024)
        model.weight[0, 0] = 0.5; model.weight[0, 1] = 0.25
        if rank == 1:                           # rank 1 only: two weights swapped; the sum of all values is exactly the same
            model.weight[0, 0] = 0.25; model.weight[0, 1] = 0.5
    chk = torch.stack([p.detach().double().sum() for p in model.parameters()]).sum().reshape(1)
    lo, hi = chk.clone(), chk.clone(); dist.all_reduce(lo, op=dist.ReduceOp.MIN); dist.all_reduce(hi, op=dist.ReduceOp.MAX)
    differing = nt.tensors_differing(list(model.parameters()), dist)
    q.put(dict(rank=rank, steps=steps, why=list(why), loop_s=round(loop_s, 2), identical_after_training=identical,
               checksum_spread=float((hi - lo).abs().item()), differing_after_swap=differing))
    dist.destroy_process_group()


def main():
    import torch.multiprocessing as mp
    ctx = mp.get_context('spawn'); q = ctx.Queue(); port = free_port(); world = 2
    procs = [ctx.Process(target=worker, args=(r, world, port, q)) for r in range(world)]
    for p in procs:
        p.start()
    res = sorted((q.get(timeout=180) for _ in range(world)), key=lambda r: r['rank'])
    for p in procs:
        p.join(timeout=60)
    print(res)
    checks = [
        ('both ranks left the training loop at the same step', res[0]['steps'] == res[1]['steps']),
        ('both agree it was a deadline (rank 1\'s), not the signal', all(r['why'] == [False, True] for r in res)),
        ('rank 0 left at rank 1\'s deadline, well before its own four seconds', res[0]['loop_s'] < 3.0),
        ('after training the parameters are identical bit for bit (0 tensors differ)', all(r['identical_after_training'] == 0 for r in res)),
        ('two weights swapped on rank 1: the checksum of sums sees no difference', res[0]['checksum_spread'] == 0.0),
        ('... and the tensor-by-tensor check counts one differing tensor', all(r['differing_after_swap'] == 1 for r in res)),
    ]
    fails = 0
    for name, good in checks:
        print(('  pass: ' if good else '  FAIL: ') + name); fails += not good
    print('NCCL STOP RESULT: %d passed, %d failed' % (len(checks) - fails, fails))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
