#!/usr/bin/env python3
"""tools/login_check.py: look before you run on a shared login node.

Run it before anything heavier than an editor on a login node: a test suite, a build, a CPU stage, a probe on the login GPU.
It reads, and never changes, the node's load, how busy each core was over the last second, the free memory, your own memory use
against your per-user limit, and the login GPU's memory and other users' processes on it. Then it says GO, REDUCE or WAIT, how many
workers to use, which cores to pin them to, and the prefix to run them with.

  python3 tools/login_check.py [--want-cpus N] [--mem-gib G] [--gpu-gib G] [--json | --print-cores]

Exit codes: 0 GO; 1 REDUCE (run with the suggested, smaller settings); 2 WAIT (do not start now); 3 the node could not be read.

The defaults are the rules the original fleet kept on JUPITER's login nodes, one core per worker: 12 workers when the 5-minute load
is under 30, else 6, and at least 6 usable cores to start at all; never cores 0 and 1 (the OS and other users kept them near 100 %
busy); a core counts as usable when it was under 50 % busy; your own memory under 20 GiB on the node; at most 16 GiB of your own on
the login GPU while anyone else uses it, with at least 2 GiB left free. Change them with the options to suit your site.

Read-only and cheap: one second of /proc/stat sampling, a few files under /proc and /sys, and nvidia-smi only when --gpu-gib is set.
No network, no Slurm, no writes.
"""
import argparse, json, os, subprocess, sys, time

GIB = 2**30


def compress(cores):
    out, run = [], []
    for c in sorted(cores):
        if run and c == run[-1] + 1:
            run.append(c)
        else:
            if run:
                out.append(str(run[0]) if len(run) == 1 else str(run[0]) + '-' + str(run[-1]))
            run = [c]
    if run:
        out.append(str(run[0]) if len(run) == 1 else str(run[0]) + '-' + str(run[-1]))
    return ','.join(out)


def read_stat():
    d = {}
    with open('/proc/stat') as f:
        for line in f:
            if line.startswith('cpu') and line[3:4].isdigit():
                p = line.split()
                v = [int(x) for x in p[1:]]
                d[int(p[0][3:])] = (sum(v), v[3] + (v[4] if len(v) > 4 else 0))
    return d


def core_busy(sample_s):
    a = read_stat()
    time.sleep(sample_s)
    b = read_stat()
    busy = {}
    for c in b:
        if c in a:
            tot = b[c][0] - a[c][0]
            idle = b[c][1] - a[c][1]
            busy[c] = round(1 - idle / tot, 3) if tot > 0 else 0.0
    return busy


def meminfo():
    m = {}
    with open('/proc/meminfo') as f:
        for line in f:
            k, v = line.split(':', 1)
            m[k] = int(v.split()[0]) * 1024
    return m


def user_slice():
    """Memory and tasks of the cgroup v2 slice that holds all of this user's sessions on this node, if it can be found."""
    try:
        with open('/proc/self/cgroup') as f:
            path = [l.strip().split('::', 1)[1] for l in f if l.startswith('0::')][0]
    except Exception:
        return None
    parts = path.strip('/').split('/')
    idx = [i for i, p in enumerate(parts) if p.startswith('user-') and p.endswith('.slice')]
    if not idx:
        return None
    base = '/sys/fs/cgroup/' + '/'.join(parts[:idx[0] + 1])

    def rd(name):
        try:
            v = open(os.path.join(base, name)).read().strip()
            return None if v == 'max' else int(v)
        except Exception:
            return None
    return dict(path=base, mem_current=rd('memory.current'), mem_max=rd('memory.max'), mem_peak=rd('memory.peak'),
                tasks_current=rd('pids.current'), tasks_max=rd('pids.max'))


def gpu_state():
    """The login GPU(s): memory, utilisation and the processes on them, each marked as yours or someone else's."""
    try:
        q = subprocess.run(['nvidia-smi', '--query-gpu=index,memory.used,memory.total,utilization.gpu',
                            '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=15)
        a = subprocess.run(['nvidia-smi', '--query-compute-apps=pid,used_memory', '--format=csv,noheader,nounits'],
                           capture_output=True, text=True, timeout=15)
    except Exception as e:
        return dict(error=type(e).__name__)
    if q.returncode != 0:
        return dict(error='nvidia-smi exit ' + str(q.returncode))
    gpus = []
    for line in q.stdout.strip().splitlines():
        p = [x.strip() for x in line.split(',')]
        if len(p) >= 4 and p[1].isdigit() and p[2].isdigit():
            gpus.append(dict(index=p[0], used_mib=int(p[1]), total_mib=int(p[2]), util=p[3]))
    me = os.getuid()
    apps = []
    for line in a.stdout.strip().splitlines():
        p = [x.strip() for x in line.split(',')]
        if len(p) >= 2 and p[0].isdigit():
            owner = None
            try:
                with open('/proc/' + p[0] + '/status') as f:
                    owner = [int(l.split()[1]) for l in f if l.startswith('Uid:')][0]
            except Exception:
                pass
            apps.append(dict(pid=int(p[0]), used_mib=int(p[1]) if p[1].isdigit() else None, mine=(owner == me)))
    return dict(gpus=gpus, apps=apps)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--want-cpus', type=int, default=12, help='workers wanted when the node is quiet (default 12)')
    ap.add_argument('--low-cpus', type=int, default=6, help='workers when the node is busy, and the fewest worth starting (default 6)')
    ap.add_argument('--busy-load', type=float, default=30.0, help='5-minute load at or above which the node counts as busy (default 30)')
    ap.add_argument('--skip-cores', default='0,1', help='cores never used (default 0,1)')
    ap.add_argument('--idle-threshold', type=float, default=0.5, help='a core is usable when it was less busy than this (default 0.5)')
    ap.add_argument('--sample-s', type=float, default=1.0, help='seconds of /proc/stat sampling (default 1)')
    ap.add_argument('--mem-gib', type=float, default=0.0, help='host memory the run will use, in GiB (default 0: not checked)')
    ap.add_argument('--own-mem-cap-gib', type=float, default=20.0, help='your own memory on the node should stay under this (default 20)')
    ap.add_argument('--mem-margin-gib', type=float, default=8.0, help='free memory to leave the node beyond your run (default 8)')
    ap.add_argument('--gpu-gib', type=float, default=0.0, help='login-GPU memory the run will use, in GiB (default 0: no GPU)')
    ap.add_argument('--gpu-own-max-gib', type=float, default=16.0, help='your own GPU memory while others use the card (default 16)')
    ap.add_argument('--gpu-free-gib', type=float, default=2.0, help='GPU memory to leave free beyond your run (default 2)')
    ap.add_argument('--json', action='store_true', help='print the whole reading as JSON')
    ap.add_argument('--print-cores', action='store_true', help='print only the cores to pin to, as a plain comma list (empty on WAIT), for scripts')
    a = ap.parse_args()

    try:
        load1, load5, load15 = os.getloadavg()
        allowed = sorted(os.sched_getaffinity(0))
        busy = core_busy(a.sample_s)
        mem = meminfo()
    except Exception as e:
        print('login_check: could not read the node: ' + type(e).__name__ + ': ' + str(e), file=sys.stderr)
        return 3
    skip = {int(x) for x in a.skip_cores.split(',') if x.strip().isdigit()}
    usable = sorted((busy.get(c, 1.0), c) for c in allowed if c not in skip and busy.get(c, 1.0) < a.idle_threshold)
    target = a.want_cpus if load5 < a.busy_load else min(a.want_cpus, a.low_cpus)
    levels, reasons = [], []

    def flag(level, why):
        levels.append(level)
        reasons.append(why)

    if load5 >= a.busy_load and a.want_cpus > a.low_cpus:
        flag(1, '5-minute load ' + str(round(load5, 1)) + ' is at or above ' + str(a.busy_load) + ': ' + str(target) + ' workers, not ' + str(a.want_cpus))
    workers = min(target, len(usable))
    if workers < min(target, a.low_cpus):
        flag(2, 'only ' + str(len(usable)) + ' cores under ' + str(int(a.idle_threshold * 100)) + ' % busy (cores ' + a.skip_cores + ' excluded); fewer than ' + str(min(target, a.low_cpus)))
        workers = 0
    elif workers < target:
        flag(1, 'only ' + str(len(usable)) + ' usable cores: ' + str(workers) + ' workers')
    cores = sorted(c for _, c in usable[:workers])

    sl = user_slice()
    avail = mem.get('MemAvailable', 0)
    if a.mem_gib > 0:
        if avail < (a.mem_gib + a.mem_margin_gib) * GIB:
            flag(2, 'the node has ' + str(round(avail / GIB, 1)) + ' GiB available; the run needs ' + str(a.mem_gib) + ' plus a ' + str(a.mem_margin_gib) + ' GiB margin')
        if sl and sl['mem_max'] and sl['mem_current'] is not None and sl['mem_max'] - sl['mem_current'] < a.mem_gib * GIB:
            flag(2, 'your per-user limit leaves ' + str(round((sl['mem_max'] - sl['mem_current']) / GIB, 1)) + ' GiB; the run needs ' + str(a.mem_gib))
        if sl and sl['mem_current'] is not None and sl['mem_current'] + a.mem_gib * GIB > a.own_mem_cap_gib * GIB:
            flag(1, 'your sessions already hold ' + str(round(sl['mem_current'] / GIB, 1)) + ' GiB; with this run you would exceed ' + str(a.own_mem_cap_gib) + ' GiB on a shared node')

    g = None
    if a.gpu_gib > 0:
        g = gpu_state()
        if 'error' in g or not g.get('gpus'):
            flag(2, 'no login GPU could be read (' + str(g.get('error', 'none listed')) + ')')
        else:
            card = g['gpus'][0]
            free = (card['total_mib'] - card['used_mib']) / 1024
            others = [x for x in g['apps'] if not x['mine']]
            if a.gpu_gib + a.gpu_free_gib > free:
                flag(2, 'the login GPU has ' + str(round(free, 1)) + ' GiB free; the run needs ' + str(a.gpu_gib) + ' plus ' + str(a.gpu_free_gib) + ' GiB left free')
            if others and a.gpu_gib > a.gpu_own_max_gib:
                flag(1, str(len(others)) + ' other process(es) on the login GPU: keep your run at or under ' + str(a.gpu_own_max_gib) + ' GiB, or run it as a job')
            if a.gpu_gib > a.gpu_own_max_gib and not others:
                flag(1, 'more than ' + str(a.gpu_own_max_gib) + ' GiB on a shared card: others can arrive mid-run; watch it, or run it as a job')

    level = max(levels) if levels else 0
    verdict = ['GO', 'REDUCE', 'WAIT'][level]
    if level == 2:
        workers, cores = 0, []
    prefix = ('taskset -c ' + compress(cores) + ' nice -n 10' + ('' if a.gpu_gib > 0 else ' env CUDA_VISIBLE_DEVICES=')) if cores else ''
    out = dict(verdict=verdict, workers=workers, cores=cores, cores_list=compress(cores), prefix=prefix,
               load=[round(load1, 1), round(load5, 1), round(load15, 1)], cores_allowed=len(allowed),
               cores_usable=len(usable), mem_available_gib=round(avail / GIB, 1), user_slice=sl, gpu=g, reasons=reasons)
    if a.print_cores:
        print(','.join(str(c) for c in cores))
    elif a.json:
        print(json.dumps(out, indent=1))
    else:
        print('load ' + ' '.join(str(x) for x in out['load']) + ' on ' + str(len(allowed)) + ' cores; ' + str(len(usable)) + ' under '
              + str(int(a.idle_threshold * 100)) + ' % busy; ' + str(out['mem_available_gib']) + ' GiB available')
        if sl and sl['mem_current'] is not None:
            print('your sessions: ' + str(round(sl['mem_current'] / GIB, 1)) + ' GiB'
                  + (' of ' + str(round(sl['mem_max'] / GIB, 1)) + ' GiB allowed' if sl['mem_max'] else '')
                  + ((', peak ' + str(round(sl['mem_peak'] / GIB, 1)) + ' GiB') if sl.get('mem_peak') else '')
                  + ((', ' + str(sl['tasks_current']) + ' tasks') if sl.get('tasks_current') is not None else ''))
        if g and g.get('gpus'):
            c = g['gpus'][0]
            print('login GPU ' + c['index'] + ': ' + str(round(c['used_mib'] / 1024, 1)) + ' of ' + str(round(c['total_mib'] / 1024, 1))
                  + ' GiB used, ' + c['util'] + ' % busy, ' + str(sum(1 for x in g['apps'] if not x['mine'])) + ' other process(es)')
        for r in reasons:
            print('  ' + r)
        if level == 2:
            print('WAIT: do not start now')
        else:
            print(verdict + ': ' + str(workers) + ' worker(s) on cores ' + compress(cores))
            print('run with: ' + prefix + ' <command>')
    return level


if __name__ == '__main__':
    sys.exit(main())
