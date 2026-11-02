#!/usr/bin/env python3
"""probes/nccl_test.py --local-rank K [--minutes M]: multi-node qualification of the fleet launcher's --nodes path.

One copy runs per GPU task on every node (the launcher starts one per GPU slot under srun, one launcher per node). It proves, with
receipts, the five things a lane's multi-node job depends on: (1) rendezvous from the Slurm variables the launcher passes through
(SLURM_NNODES, SLURM_NODEID, SLURM_JOB_NODELIST) and one visible GPU per task; (2) NCCL collectives across nodes (all-reduce value
check, all-gather of host / slot / GPU identity); (3) inter-node bandwidth (256 MiB float32 all-reduce, bus bandwidth); (4) a real
DistributedDataParallel training loop with parameters identical on every rank at the end; (5) the launcher's forwarded SIGUSR1:
checkpoint on rank 0, barrier, exit 0 on every rank, so the receipts read as a checkpoint segment on every node. Leaving the training
loop is decided together (the signal on any rank, or any rank's deadline), so every rank stops at the same step; the parameters are
compared tensor by tensor, bit for bit. probes/test_nccl_stop.py checks both on CPU.
Scientific weight zero. Writes nccl-rank<R>.json (every rank) and nccl-summary.json (rank 0) into PACK_RECEIPT_DIR."""
import argparse, datetime, json, os, signal, socket, subprocess, sys, time

def hostnames():
    nl = os.environ.get('SLURM_JOB_NODELIST', '')
    try:
        out = subprocess.run(['scontrol', 'show', 'hostnames', nl], capture_output=True, text=True, check=True).stdout.split()
        return out or [socket.gethostname()]
    except Exception:
        return [socket.gethostname()]

def resolve_master(first):
    # JSC convention on several systems: "<host>i" is the InfiniBand name. Use it if it resolves, else the plain name.
    tried = {}
    for cand in (first + 'i', first):
        try:
            tried[cand] = socket.gethostbyname(cand)
        except Exception as ex:
            tried[cand] = 'unresolved: ' + type(ex).__name__
    for cand in (first + 'i', first):
        if not str(tried[cand]).startswith('unresolved'):
            return cand, tried
    return first, tried

def train_until_stop(step, stop, deadline, dist, flag, every=20):
    """Run step() until some rank has seen the stop signal or passed its own deadline. The ranks decide together: every `every` steps
    they all-reduce [signal seen, deadline passed] with MAX, so all of them leave at the same step and their collectives stay matched.
    (Each rank used to leave on its own clock: one past its deadline left the loop while another was still in DDP's all-reduce.)
    Returns (steps, (stopped by the signal, stopped by a deadline))."""
    steps = 0
    while True:
        step(); steps += 1
        if steps % every == 0:
            flag[0] = 1.0 if stop['flag'] else 0.0
            flag[1] = 1.0 if time.time() >= deadline else 0.0
            dist.all_reduce(flag, op=dist.ReduceOp.MAX)
            if float(flag.max()) > 0:
                return steps, (bool(flag[0] > 0), bool(flag[1] > 0))

def tensors_differing(params, dist):
    """How many parameter tensors differ from rank 0's, bit for bit, counted on every rank and summed: 0 means identical. A checksum
    of sums can agree while tensors differ (two values swapped, or errors that cancel)."""
    import torch
    n = torch.zeros(1, device=params[0].device)
    for p in params:
        ref = p.detach().clone(); dist.broadcast(ref, src=0)
        if not torch.equal(ref, p.detach()): n += 1
    dist.all_reduce(n)
    return int(n.item())

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--local-rank', type=int, required=True)
    ap.add_argument('--minutes', type=float, default=12.0); a = ap.parse_args()
    import torch, torch.distributed as dist
    from torch.nn.parallel import DistributedDataParallel as DDP
    nnodes = int(os.environ.get('SLURM_NNODES', '1')); node = int(os.environ.get('SLURM_NODEID', '0'))
    per_node = int(os.environ.get('FLEET_GPUS_PER_NODE', '4')); world = nnodes * per_node; rank = node * per_node + a.local_rank
    hosts = hostnames(); master, tried = resolve_master(hosts[0])
    port = 29500 + int(os.environ.get('SLURM_JOB_ID', '0')) % 1000
    rdir = os.environ.get('PACK_RECEIPT_DIR', '.'); os.makedirs(rdir, exist_ok=True)
    rec = dict(rank=rank, world=world, node=node, local_rank=a.local_rank, host=socket.gethostname(), hosts=hosts, master=master,
               master_resolution=tried, port=port, pack_slot=os.environ.get('PACK_SLOT'), cuda_visible=os.environ.get('CUDA_VISIBLE_DEVICES'),
               torch=torch.__version__, cores=os.environ.get('PACK_CORES'), start_utc=datetime.datetime.utcnow().isoformat() + 'Z')
    # Identity first, before anything that can hang: printed and written, so a failed rendezvous still leaves evidence.
    try:
        master_ip = socket.gethostbyname(hosts[0])          # IPv4 literal: no IPv6 attempt, and whatever route THIS node has to node 0
    except Exception as ex:
        master_ip = master; rec['master_ip_error'] = repr(ex)
    rec['master_ip'] = master_ip
    if a.local_rank == 0:
        try:
            rec['ipv4_interfaces'] = [l.split()[1] + ' ' + l.split()[3] for l in subprocess.run(['ip', '-o', '-4', 'addr'], capture_output=True, text=True).stdout.splitlines()]
        except Exception as ex:
            rec['ipv4_interfaces'] = repr(ex)
    rec['env'] = {k: os.environ.get(k) for k in ('SLURM_NNODES', 'SLURM_NODEID', 'SLURM_JOB_NODELIST', 'SLURM_PROCID', 'NCCL_SOCKET_IFNAME', 'NCCL_DEBUG', 'NCCL_IB_HCA')}
    print('START ' + json.dumps(rec), flush=True)
    json.dump(rec, open(os.path.join(rdir, f'nccl-rank{rank}-start.json'), 'w'), indent=1)
    assert torch.cuda.is_available() and torch.cuda.device_count() == 1, f'expected one visible GPU, saw {torch.cuda.device_count()}'
    dev = torch.device('cuda:0'); torch.cuda.set_device(dev)
    rec['gpu'] = dict(name=torch.cuda.get_device_name(0), uuid=str(torch.cuda.get_device_properties(0).uuid) if hasattr(torch.cuda.get_device_properties(0), 'uuid') else None)
    stop = dict(flag=False)
    signal.signal(signal.SIGUSR1, lambda *_: stop.__setitem__('flag', True))
    t0 = time.time()
    def fail(stage, ex):
        rec['failed_at'] = stage; rec['error'] = repr(ex)[:2000]; rec['seconds_in_stage'] = round(time.time() - t0, 1)
        json.dump(rec, open(os.path.join(rdir, f'nccl-rank{rank}-FAILED.json'), 'w'), indent=1); print('FAILED ' + json.dumps(rec), flush=True); sys.exit(3)
    try:
        dist.init_process_group('nccl', init_method=f'tcp://{master_ip}:{port}', world_size=world, rank=rank, timeout=datetime.timedelta(seconds=120), device_id=dev)
    except Exception as ex:
        fail('rendezvous (TCP store)', ex)
    rec['rendezvous_s'] = round(time.time() - t0, 3); rec['nccl'] = '.'.join(map(str, torch.cuda.nccl.version()))
    print(f'rank {rank}: rendezvous done in {rec["rendezvous_s"]} s', flush=True)
    # (2) collectives: value check and identity map (the first collective builds the NCCL communicator: the other place a hang can hide)
    t0 = time.time()
    try:
        x = torch.tensor([float(rank)], device=dev); dist.all_reduce(x); torch.cuda.synchronize()
    except Exception as ex:
        fail('first all-reduce (NCCL communicator)', ex)
    rec['first_allreduce_s'] = round(time.time() - t0, 3); print(f'rank {rank}: first all-reduce done in {rec["first_allreduce_s"]} s', flush=True)
    expect = world * (world - 1) / 2
    assert abs(x.item() - expect) < 1e-6, f'all-reduce sum {x.item()} != {expect}'
    ident = [None] * world; dist.all_gather_object(ident, dict(rank=rank, host=rec['host'], slot=rec['pack_slot'], uuid=rec['gpu']['uuid']))
    rec['allreduce_sum_ok'] = True
    # (3) bandwidth: 256 MiB float32 all-reduce, 3 warm-ups, 10 timed
    n = 256 * 1024 * 1024 // 4; buf = torch.ones(n, device=dev)
    for _ in range(3): dist.all_reduce(buf)
    torch.cuda.synchronize(); t = time.time()
    for _ in range(10): dist.all_reduce(buf)
    torch.cuda.synchronize(); dt = (time.time() - t) / 10
    alg = (n * 4 / 2**30) / dt; rec['allreduce_256MiB'] = dict(seconds=round(dt, 5), algbw_GiB_s=round(alg, 3), busbw_GiB_s=round(alg * 2 * (world - 1) / world, 3))
    del buf
    # (4) DDP training loop: a plain residual MLP of about 67M parameters, AdamW, random data with a rank-dependent seed
    torch.manual_seed(1234); width, depth = 2048, 8
    class Block(torch.nn.Module):
        def __init__(s): super().__init__(); s.a = torch.nn.Linear(width, width); s.b = torch.nn.Linear(width, width)
        def forward(s, h): return h + s.b(torch.nn.functional.gelu(s.a(h)))
    model = torch.nn.Sequential(*[Block() for _ in range(depth)]).to(dev); ddp = DDP(model, device_ids=[0])
    opt = torch.optim.AdamW(ddp.parameters(), lr=1e-4); g = torch.Generator(device=dev); g.manual_seed(100 + rank)
    batch, tstart, deadline, last = 256, time.time(), time.time() + a.minutes * 60, {}
    def step():
        h = torch.randn(batch, width, device=dev, generator=g); loss = ddp(h).pow(2).mean()
        opt.zero_grad(set_to_none=True); loss.backward(); opt.step(); last['loss'] = loss
    steps, (by_signal, by_deadline) = train_until_stop(step, stop, deadline, dist, torch.zeros(2, device=dev))
    torch.cuda.synchronize(); el = time.time() - tstart
    rec['ddp'] = dict(steps=steps, seconds=round(el, 2), samples_per_s_per_rank=round(steps * batch / el, 1), parameters=sum(p.numel() for p in model.parameters()),
                      stopped_by_signal=by_signal, stopped_by_deadline=by_deadline, final_loss=float(last['loss'].item()))
    chk = torch.stack([p.detach().double().sum() for p in model.parameters()]).sum().reshape(1)
    lo, hi = chk.clone(), chk.clone(); dist.all_reduce(lo, op=dist.ReduceOp.MIN); dist.all_reduce(hi, op=dist.ReduceOp.MAX)
    rec['parameter_checksum_spread'] = float((hi - lo).abs().item())   # for the record only: a checksum can agree while tensors differ
    rec['parameter_tensors_differing'] = tensors_differing(list(model.parameters()), dist)
    rec['parameters_identical_across_ranks'] = rec['parameter_tensors_differing'] == 0
    assert rec['parameters_identical_across_ranks'], 'DDP parameters differ across ranks'
    # (5) checkpoint on rank 0 (whether stopped by the signal or by the clock), barrier, exit 0 everywhere
    if rank == 0:
        out = os.path.join(os.environ.get('FLEET_RUNS', '/tmp'), 'probes', 'nccl-' + os.environ.get('SLURM_JOB_ID', 'local')); os.makedirs(out, exist_ok=True)
        torch.save(dict(model=model.state_dict(), opt=opt.state_dict(), steps=steps), os.path.join(out, 'ckpt.pt')); rec['checkpoint'] = os.path.join(out, 'ckpt.pt')
    dist.barrier(); rec['end_utc'] = datetime.datetime.utcnow().isoformat() + 'Z'; rec['peak_mem_MiB'] = int(torch.cuda.max_memory_allocated() / 2**20)
    json.dump(rec, open(os.path.join(rdir, f'nccl-rank{rank}.json'), 'w'), indent=1)
    if rank == 0:
        hostsset = sorted({i['host'] for i in ident}); uu = {i['uuid'] for i in ident}
        summ = dict(world=world, nodes=nnodes, hosts=hostsset, distinct_hosts=len(hostsset), distinct_gpus=len(uu), identities=ident,
                    master=master, nccl=rec['nccl'], allreduce_256MiB=rec['allreduce_256MiB'], ddp=rec['ddp'],
                    parameters_identical_across_ranks=rec['parameters_identical_across_ranks'],
                    passed=bool(len(hostsset) == nnodes and (None in uu or len(uu) == world)))
        json.dump(summ, open(os.path.join(rdir, 'nccl-summary.json'), 'w'), indent=1); print(json.dumps(summ)[:1500], flush=True)
    dist.destroy_process_group(); return 0

if __name__ == '__main__':
    sys.exit(main())
