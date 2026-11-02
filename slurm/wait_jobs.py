#!/usr/bin/env python3
"""wait_jobs.py JOBID [JOBID...] [--poll 600] [--receipts DIR]: block until every job has left the queue, then print one line per job
with its Slurm state and its receipt summary (all_ok, wall, GPU-slot-hours). Exit 0 if all completed with all_ok, else 1.
Meant to be run in the background by an agent (it is woken when this exits), or in a Monitor loop."""
import argparse, glob, json, os, subprocess, sys, time
DEFAULT_POLL=600.0   # one watcher a job, checking every ten minutes: the scheduler is shared by every user of the machine
MIN_POLL=60.0        # never faster than once a minute, whatever --poll says
def states(ids):
    out=subprocess.run(['sacct','-X','-n','-P','-j',','.join(ids),'--format=JobID,JobName,State,ElapsedRaw'],capture_output=True,text=True).stdout
    d={}
    for l in out.strip().splitlines():
        f=l.split('|')
        if len(f)>=4: d[f[0]]=dict(name=f[1],state=f[2].split()[0],elapsed=f[3])
    return d
TERMINAL=('COMPLETED','FAILED','CANCELLED','TIMEOUT','NODE_FAIL','OUT_OF_MEMORY','BOOT_FAIL','DEADLINE','PREEMPTED')
def queued(ids):
    """Jobs still in the queue, or None when squeue could not answer. A failed squeue must never read as an empty queue: one
    scheduler hiccup would end the wait while the job was still running, and a caller that takes the end of the wait as the end of
    the job could then start the next segment beside the first."""
    r=subprocess.run(['squeue','-h','-j',','.join(ids),'-o','%i'],capture_output=True,text=True)
    if r.returncode!=0 and 'Invalid job id' not in r.stderr: return None
    return [j for j in r.stdout.split() if j.split('_')[0] in ids]
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('jobs',nargs='+'); ap.add_argument('--poll',type=float,default=DEFAULT_POLL,help='seconds between checks (default 600; never under 60)'); ap.add_argument('--receipts',default='receipts'); a=ap.parse_args()
    ids=[j.split('_')[0] for j in a.jobs]
    a.poll=max(a.poll,MIN_POLL)   # the scheduler is shared: never poll faster than once a minute
    while True:
        q=queued(ids)
        if q is not None and not q:
            st=states(ids)   # the queue is empty: believe it only when accounting agrees that every job has ended
            if all(st.get(j,{}).get('state','').startswith(TERMINAL) for j in ids): break
        time.sleep(a.poll)
    st=states(ids); ok=True
    for j in ids:
        s=st.get(j,dict(name='?',state='UNKNOWN',elapsed='?'))
        recs=glob.glob(os.path.join(a.receipts,f"{s['name']}-{j}*","receipt.json"))
        summ='no receipt'
        for r in recs:
            try:
                d=json.load(open(r)); summ=f"all_ok={d['all_ok']} wall={d['wall_s']}s gpu_slot_h={d['gpu_slot_hours_held']} tasks={len(d['tasks'])} interrupted={d['interrupted'] is not None} receipt={r}"
                ok = ok and d['all_ok']
            except Exception as e: summ=f'unreadable receipt {r}: {e}'; ok=False
        if not recs or s['state']!='COMPLETED': ok=False
        print(f"job {j} {s['name']} {s['state']} {s['elapsed']}s | {summ}")
    sys.exit(0 if ok else 1)
if __name__=='__main__': main()
