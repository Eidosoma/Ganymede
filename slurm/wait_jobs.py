#!/usr/bin/env python3
"""wait_jobs.py JOBID [JOBID...] [--poll 600] [--receipts DIR]: block until every job has left the queue, then print one line per job
(one per array element) with its Slurm state and its receipts (all_ok, wall, GPU-slot-hours). Exit 0 if every job, and every element of
an array, completed and left a receipt with all_ok on every node it ran on; else 1.
A JOBID is a job, a whole array (its base id) or one element (BASE_INDEX). An array is followed element by element, as accounting lists
it (BASE_INDEX, or BASE_[a-b] for elements that never started); an element's receipts are under its own job id, RECEIPTS/NAME-ID-aINDEX,
as slurm/node.sbatch writes them; a job on N nodes needs N receipts, RECEIPTS/NAME-ID/node-0 ... node-(N-1). (The waiter used to look
for an accounting record under the base id, which an array never has, so it waited forever; and for one receipt beside the job folder,
so a multi-node job read "no receipt".)
Meant to be run in the background by an agent (it is woken when this exits), or in a Monitor loop."""
import argparse, json, os, subprocess, sys, time
DEFAULT_POLL=600.0   # one watcher a job, checking every ten minutes: the scheduler is shared by every user of the machine
MIN_POLL=60.0        # never faster than once a minute, whatever --poll says
TERMINAL=('COMPLETED','FAILED','CANCELLED','TIMEOUT','NODE_FAIL','OUT_OF_MEMORY','BOOT_FAIL','DEADLINE','PREEMPTED')
def split_id(j):
    base,_,task=j.partition('_'); return base,task
def covers(spec, task):
    """Does a task field as squeue or sacct print it ('3', '[2-5%2]', '[1,3,7-9]') include element `task`? Unreadable: assume it does."""
    spec=spec.strip('[]').split('%')[0]
    for part in spec.split(','):
        a,_,b=part.partition('-')
        try:
            if b:
                if int(a)<=int(task)<=int(b.split(':')[0]): return True
            elif int(a)==int(task): return True
        except ValueError:
            return True
    return False
def wanted(base, task, want):
    """Is a queue or accounting row (base id, task field) part of a requested job, array or element?"""
    return any(base==b and (not t or not task or covers(task,t)) for b,t in want)
def states(bases):
    """Accounting rows of these base ids, one per job or array element: {JobID: row}."""
    out=subprocess.run(['sacct','-X','-n','-P','-j',','.join(bases),'--format=JobID,JobName,State,ElapsedRaw,JobIDRaw,NNodes'],capture_output=True,text=True).stdout
    d={}
    for l in out.strip().splitlines():
        f=l.split('|')
        if len(f)>=4:
            base,task=split_id(f[0])
            d[f[0]]=dict(id=f[0],base=base,task=task,name=f[1],state=(f[2].split() or [''])[0],elapsed=f[3],
                         raw=(f[4] if len(f)>4 and f[4] else base),nodes=(int(f[5]) if len(f)>5 and f[5].isdigit() else 1))
    return d
def queued(bases, want):
    """Jobs still in the queue, or None when squeue could not answer. A failed squeue must never read as an empty queue: one
    scheduler hiccup would end the wait while the job was still running, and a caller that takes the end of the wait as the end of
    the job could then start the next segment beside the first."""
    r=subprocess.run(['squeue','-h','-j',','.join(bases),'-o','%i'],capture_output=True,text=True)
    if r.returncode!=0 and 'Invalid job id' not in r.stderr: return None
    return [j for j in r.stdout.split() if wanted(*split_id(j), want)]
def receipt_paths(s, receipts):
    d=os.path.join(receipts, f"{s['name']}-{s['raw']}" + (f"-a{s['task']}" if s['task'] else ''))
    return [os.path.join(d, f'node-{i}', 'receipt.json') for i in range(s['nodes'])] if s['nodes']>1 else [os.path.join(d, 'receipt.json')]
def report(s, receipts):
    """One line for one job or element, and whether it counts as a success."""
    if '[' in s['task']:
        return f"job {s['id']} {s['name']} {s['state']} | never started", False
    ok=s['state']=='COMPLETED'; seen=[]; missing=[]
    for p in receipt_paths(s, receipts):
        if not os.path.exists(p): missing.append(p); ok=False; continue
        try:
            d=json.load(open(p))
            seen.append(f"all_ok={d['all_ok']} wall={d['wall_s']}s gpu_slot_h={d['gpu_slot_hours_held']} tasks={len(d['tasks'])} interrupted={d['interrupted'] is not None} receipt={p}")
            ok = ok and d['all_ok'] is True
        except Exception as e:
            seen.append(f'unreadable receipt {p}: {e}'); ok=False
    if s['nodes']>1:
        summ=f"node receipts {len(seen)} of {s['nodes']}" + ('; ' + '; '.join(seen) if seen else '') + ('; missing: ' + ', '.join(missing) if missing else '')
    else:
        summ=seen[0] if seen else 'no receipt'
    return f"job {s['id']} {s['name']} {s['state']} {s['elapsed']}s | {summ}", ok
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('jobs',nargs='+'); ap.add_argument('--poll',type=float,default=DEFAULT_POLL,help='seconds between checks (default 600; never under 60)'); ap.add_argument('--receipts',default='receipts')
    a=ap.parse_args()
    want=[split_id(j) for j in a.jobs]; bases=sorted({b for b,_ in want})
    a.poll=max(a.poll,MIN_POLL)   # the scheduler is shared: never poll faster than once a minute
    def rows(st, b, t): return [v for v in st.values() if v['base']==b and (not t or not v['task'] or covers(v['task'],t))]
    while True:
        q=queued(bases, want)
        if q is not None and not q:
            st=states(bases)   # the queue is empty: believe it only when accounting agrees that every job has ended
            if all(rows(st,b,t) and all(v['state'].startswith(TERMINAL) for v in rows(st,b,t)) for b,t in want): break
        time.sleep(a.poll)
    st=states(bases); ok=True; shown=set()
    for b,t in want:
        for v in sorted(rows(st,b,t), key=lambda v:(len(v['task']), v['task'])):
            if v['id'] in shown: continue
            shown.add(v['id']); line,good=report(v, a.receipts); ok = ok and good; print(line)
    sys.exit(0 if ok and shown else 1)
if __name__=='__main__': main()
