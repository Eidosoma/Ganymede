# Multi-node jobs

`submit.sh --nodes N` runs the same spec on N whole nodes, one launcher per node under `srun`, with receipts in `receipts/<name>-<jobid>/node-<k>/`. The job counts N nodes against the fleet ceiling and is charged N times `GPUS_PER_NODE` GPU-hours an hour. This page is the recipe qualified on JUPITER Booster with `probes/nccl_test.py`; qualify any deviation with a short job first.

## Submitting

```bash
bash slurm/submit.sh --site mysite --nodes 2 --time 00:20:00 examples/specs/nccl-2node.json
```

Give the spec one GPU task per GPU. Each task sees exactly one GPU (`cuda:0`).

## What each task must do

- **Rank and world.** `world = SLURM_NNODES x GPUs per node`; `rank = SLURM_NODEID x GPUs per node + local rank`, with the local rank fixed per task in the spec.
- **Master address.** The first host of `scontrol show hostnames $SLURM_JOB_NODELIST`, resolved to its IPv4 address with `socket.gethostbyname`. On JUPITER the host name itself once resolved to an interconnect alias and the rendezvous hung; an IPv4 literal avoids that.
- **Port.** `29500 + SLURM_JOB_ID % 1000`, so two jobs never collide.
- **Bootstrap interface.** `NCCL_SOCKET_IFNAME` and `GLOO_SOCKET_IFNAME` name the interface for the bootstrap only (`ib0` on JUPITER). NCCL then uses InfiniBand natively, on all adapters, with GPUDirect RDMA.
- **A short rendezvous timeout.** `init_process_group(..., timeout=120 s)`, so a bad rendezvous fails in two minutes instead of holding the nodes to the limit. Write a small failure record before exiting non-zero.
- **Stopping together.** Slurm's warning reaches the batch shell, which forwards it through `srun` to every node's launcher, which forwards `USR1` to every task. In a collective job the ranks must agree when to stop: a flag set by the signal handler, all-reduced with MAX every few steps; when any rank has it, all stop at that step, rank 0 checkpoints, everyone meets at a barrier and exits 0. A rank that exits on its own leaves the others hanging in a collective until the limit.

## What was measured on JUPITER Booster

| | one node | two nodes |
|---|---|---|
| ranks | 4 | 8 |
| rendezvous, first all-reduce | | 3.3 s, 0.73 s |
| 256 MiB all-reduce | 1.23 ms (204 GiB/s algorithm bandwidth, NVLink) | 3.17 ms (79 GiB/s, about 85 % of the line rate of four 200 Gb/s adapters) |
| DDP on a 67M-parameter MLP, batch 256 per rank | 39,337 samples/s per rank | 27,730 samples/s per rank (70.5 %) |
| parameters identical on every rank at the end | yes | yes |
| stopped by the forwarded signal, `checkpoint_segment` | true | true on both nodes |

The test model is communication-bound by design (9 ms of compute per step against a 256 MiB gradient all-reduce), so 70 % per-rank throughput is a floor for a real model, not a forecast. Measure your own scaling with a short job before a long one.

## Not yet qualified

More than two nodes, `torchrun` (the recipe starts one process per GPU task from the spec instead), pipeline or tensor parallelism, and the fleet queue's release at the end of a multi-node job.
