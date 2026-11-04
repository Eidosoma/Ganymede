#!/usr/bin/env python3
"""budget.py [--site NAME] [--lane TAG] [--est-hours H] [--json] [--no-cache]
Per lane: GPU-hours used (accounting since BUDGET_SINCE), committed (running jobs' remaining time and pending jobs' full
time limit, times their GPUs), cap (caps.json), headroom. With --lane and --est-hours: exit 3 if used+committed+est > cap.
A queued job with no time limit (UNLIMITED, or a limit that cannot be read) has no worst case: its lane admits nothing more until
it gets one (exit 3; it must never count as nothing).
A submission (--lane) needs a valid caps.json with a finite, non-negative cap for its lane: otherwise exit 3, before accounting
is read (a missing or empty file must never mean no limit). The table works without one.
Accounting is read for the site's ACCOUNT only, in UTC windows of BUDGET_WINDOW_DAYS (7), each job once; finished windows are
cached (receipts/.budget_cache.<site>.json), so a check reads only the last week or two. --no-cache reads every window."""
import argparse, datetime as dt, json, math, os, re, subprocess, sys
HERE=os.path.dirname(os.path.abspath(__file__)); JDIR=os.path.dirname(HERE)
def site_env(site):
    d={}
    for l in open(os.path.join(HERE,f'site.{site}.env')):
        l=l.split('#')[0].strip()
        if '=' in l: k,v=l.split('=',1); d[k]=v.strip().strip('"').strip("'")
    return d
sys.path.insert(0,HERE); import timelimit   # one reading of Slurm's time forms, shared with the submit wrapper
def dur(s):
    """A Slurm duration as squeue prints it (M:SS, H:MM:SS, D-HH:MM:SS; any of Slurm's six forms) -> seconds; None for UNLIMITED,
    NOT_SET, N/A, INVALID and anything else that is not a duration (counted as 0, a job with no limit would commit
    nothing)."""
    return timelimit.seconds((s or '').strip())
def gpus_of(tres, nodes, per_node, charge_node, per_node_fmt=False):
    """GPUs of a job. sacct's AllocTRES is a job total and carries a typed and an untyped entry ('...,gres/gpu:gh200=8,gres/gpu=8,node=2');
    squeue's %b is per node ('gres/gpu:4'). The caller names the format: guessing it from the string let the typed entry in AllocTRES
    count a multi-node job's GPUs once more per node (a two-node job read 1.58 GPU-hours for 0.79)."""
    t=tres or ''
    if per_node_fmt:
        m=re.search(r'gpu(?::[A-Za-z][\w.-]*)?[:=](\d+)',t)
        if m: return int(m.group(1))*nodes
    else:
        m=re.search(r'gres/gpu=(\d+)',t)
        if m: return int(m.group(1))
    return nodes*per_node if charge_node else 0
TERMINAL=('COMPLETED','FAILED','CANCELLED','TIMEOUT','NODE_FAIL','OUT_OF_MEMORY','BOOT_FAIL','DEADLINE','PREEMPTED')
def parse_when(s):
    for f in ('%Y-%m-%dT%H:%M:%S','%Y-%m-%d'):
        try: return dt.datetime.strptime(s,f)
        except (TypeError,ValueError): pass
    return None
def windows(start, now, days):
    out=[]; ws=start
    while ws<now:
        we=min(ws+dt.timedelta(days=days),now); out.append((ws,we)); ws=we
    return out
def count_elements(task):
    """How many array elements the task part of an accounting JobID names: '' or '3' one; '[5-9]' five; '[1,3,7-9%2]' five."""
    if not task.startswith('['): return 1
    n=0
    for part in task.strip('[]').split('%')[0].split(','):
        a,_,b=part.partition('-')
        try: n+=(int(b.split(':')[0])-int(a)+1) if b else 1
        except ValueError: n+=1
    return max(n,1)
def used_by_lane(env, site, per_node, charge, since, read, no_cache=False):
    """GPU-hours per lane from accounting, for the site's ACCOUNT only (sacct -a -A: every user of the account, nobody else's jobs).
    The period is read in UTC windows of BUDGET_WINDOW_DAYS (7), so no single reply grows large (the accounting database cut off
    replies of about 1.8 MB), and each job is counted once however many windows return it. A window that ended a day ago or more and
    whose jobs have all finished never changes again: it is kept in the cache (BUDGET_CACHE_FILE, default
    receipts/.budget_cache.<site>.json) and not read again, so a check reads only the last week or two. A window whose read failed
    is never cached. read(cmd) returns the command's output, or None when it failed (the caller has already refused if checking)."""
    acct=env.get('ACCOUNT',''); fmt='--format=JobID,JobName,AllocNodes,AllocTRES,ElapsedRaw,State'
    base=['sacct','-a','-X','-n','-P']+(['-A',acct] if acct else [])
    jobs={}
    def take(text, into):
        for l in (text or '').strip().splitlines():
            f=l.split('|')
            if len(f)<6: continue
            into[f[0]]=(f[1].split('-')[0], gpus_of(f[3],int(f[2] or 0),per_node,charge)*int(f[4] or 0)/3600, f[5].split(' ')[0])
    start=parse_when(since)
    if start is None:   # a relative start such as now-30days: one read, no windows, no cache
        take(read(base+['--starttime',since,fmt]), jobs)
    else:
        now=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None,microsecond=0)
        days=float(env.get('BUDGET_WINDOW_DAYS','7') or 7)
        cpath=env.get('BUDGET_CACHE_FILE') or os.path.join(JDIR,'receipts','.budget_cache.'+site+'.json')
        key=dict(version=1,account=acct,since=since,window_days=days,charge=charge,per_node=per_node)
        cache={}
        if not no_cache:
            try:
                c=json.load(open(cpath))
                if c.get('key')==key: cache=c.get('windows',{})
            except Exception: cache={}
        changed=False
        for ws,we in windows(start,now,days):
            k=ws.isoformat()+'/'+we.isoformat()
            if k in cache:
                for jid,v in cache[k].items(): jobs.setdefault(jid,tuple(v))
                continue
            text=read(base+['--starttime',ws.isoformat(),'--endtime',we.isoformat(),fmt])
            w={}; take(text,w); jobs.update(w)
            if text is not None and we<=now-dt.timedelta(days=1) and all(v[2].startswith(TERMINAL) for v in w.values()):
                cache[k]={jid:list(v) for jid,v in w.items()}; changed=True
        if changed:
            try:
                os.makedirs(os.path.dirname(cpath),exist_ok=True); tmp=cpath+'.tmp.'+str(os.getpid())
                with open(tmp,'w') as f: json.dump(dict(key=key,windows=cache),f)
                os.replace(tmp,cpath)
            except Exception: pass   # the cache only saves reads: a failed write means the next check reads more
    used={}
    for lane,gh,state in jobs.values(): used[lane]=used.get(lane,0)+gh
    return used, jobs
def resolve(v):
    """A site-file value such as ${NAME:-/default/path}: the environment's NAME if set, else the default."""
    m=re.match(r'^\$\{(\w+):-(.*)\}$', v or '')
    return (os.environ.get(m.group(1)) or m.group(2)) if m else v
def usable_cap(v):
    return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and v>=0
def load_caps(path):
    """(caps, bad, problem): the usable caps by lane tag, the entries that are not a finite, non-negative number, and why the file as
    a whole cannot be used (None when it can). A missing or empty file was once read as {}, and every lane ran uncapped."""
    if not os.path.exists(path): return {},{},f'no caps file ({path})'
    try: raw=json.load(open(path))
    except Exception as e: return {},{},f'the caps file {path} cannot be read ({type(e).__name__}: {str(e)[:100]})'
    if not isinstance(raw,dict): return {},{},f'the caps file {path} is not a JSON object of lane: GPU-hours'
    if not raw: return {},{},f'the caps file {path} names no lane'
    return {k:v for k,v in raw.items() if usable_cap(v)},{k:v for k,v in raw.items() if not usable_cap(v)},None
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--site',default='local'); ap.add_argument('--lane'); ap.add_argument('--est-hours',type=float,default=0)
    ap.add_argument('--json',action='store_true'); ap.add_argument('--no-cache',action='store_true',help='read every accounting window again'); a=ap.parse_args()
    env=site_env(a.site); per_node=int(env.get('GPUS_PER_NODE','4')); charge=env.get('CHARGE_WHOLE_NODES','1')=='1'; since=env.get('BUDGET_SINCE','now-30days')
    caps,bad_caps,caps_problem=load_caps(os.path.join(JDIR,'caps.json'))
    if a.lane is not None:   # a submission (an empty tag included): a valid caps file and a usable cap for its lane, checked first
        if a.lane=='': why="an empty lane tag: a job name must start with its lane's tag and a hyphen"
        elif caps_problem: why=caps_problem
        elif a.lane in bad_caps: why=f"lane '{a.lane}' has no usable cap in caps.json ({bad_caps[a.lane]!r}): a cap must be a finite, non-negative number of GPU-hours"
        elif a.lane not in caps: why=f"lane '{a.lane}' has no cap in caps.json (known lanes: {', '.join(sorted(caps)) or 'none'}); a job name must start with its lane tag (for example alpha-...; the part before the first '-')"
        else: why=None
        if why: print(f"BUDGET: {why}; refusing rather than running uncapped (--over-cap-ok skips the budget check, deliberately)",file=sys.stderr); sys.exit(3)
    elif caps_problem or bad_caps: print(f"BUDGET: note: {caps_problem or 'unusable caps for '+', '.join(sorted(bad_caps))}; shown without a cap",file=sys.stderr)
    used={}
    def read(cmd, none_on_failure=False):
        """Run an accounting or queue command. Fail closed: if it cannot be run or exits non-zero, a budget CHECK refuses
        (exit 3) instead of reading the silence as 'nothing used, nothing queued'; the plain table only warns."""
        try: r=subprocess.run(cmd,capture_output=True,text=True,timeout=120,env={**os.environ,'TZ':'UTC'})   # Slurm reads and prints times in UTC
        except Exception as e: r=None; err=repr(e)
        if r is None or r.returncode!=0:
            msg=f"BUDGET: `{cmd[0]}` failed ({err if r is None else (r.stderr.strip() or 'exit '+str(r.returncode))[:200]}); the budget cannot be verified"
            print(msg,file=sys.stderr)
            if a.lane is not None: sys.exit(3)
            return None if none_on_failure else ''
        return r.stdout
    used,acct_jobs=used_by_lane(env,a.site,per_node,charge,since,lambda cmd: read(cmd,none_on_failure=True),a.no_cache)
    com={}; counts={}; qids=set(); qcom={}; unbounded={}
    acct=env.get('ACCOUNT','')   # the whole account's queue, one line per array task (-r): a pending array is counted task by task
    out=read(['squeue','-h','-r']+(['-A',acct] if acct else ['-u',os.environ.get('USER','')])+['-o','%i|%j|%t|%l|%M|%D|%b'])
    for l in out.strip().splitlines():
        jid,name,st,lim,el,nodes,tres=l.split('|')[:7]; lane=name.split('-')[0]; nodes=int(nodes or 1); qids.add(jid.split('_')[0])
        g=gpus_of(tres,nodes,per_node,charge,per_node_fmt=True); L=dur(lim)
        if L is None: unbounded.setdefault(lane,[]).append(jid); L=0   # no limit, or one that cannot be read: no bound (refused below)
        rem=max(0,L-((dur(el) or 0) if st=='R' else 0))
        qcom[jid.split('_')[0]]=qcom.get(jid.split('_')[0],0)+g*rem/3600; com[lane]=com.get(lane,0)+g*rem/3600; c=counts.setdefault(lane,{'R':0,'PD':0,'other':0}); c[st if st in ('R','PD') else 'other']+=1
    # Reservations: an accepted submission's worst case stays on file until accounting has reconciled the job, every one of its tasks
    # finished there. Until then the job counts as the larger of that worst case and what the queue and accounting already show for it:
    # never twice and never nothing, whether it is only accepted, queued, running, or ended and gone from the queue before its record
    # reaches accounting. (A reservation used to be dropped as soon as the queue showed its job, so a job that ended before accounting
    # had it counted nothing, and one finished element retired a whole array: the second review of the public release.) A reservation
    # whose job neither the queue nor accounting shows, a day after its time limit and a day after the queue last showed it, expires,
    # loudly. Lines: UTC|job|lane|GPU-h|limit|name|nodes at once|held or chained|tasks|when the queue last showed the job.
    rpath=os.environ.get('FLEET_RESERVATIONS_FILE') or resolve(env.get('FLEET_RESERVATIONS_FILE','')) or os.path.join(JDIR,'receipts','FLEET_RESERVATIONS.log')
    acct_base={}   # base id -> [GPU-hours accounting shows, elements it shows finished]
    for jid,v in acct_jobs.items():
        b,_,t=jid.partition('_'); e=acct_base.setdefault(b,[0.0,0]); e[0]+=v[1]
        if v[2].startswith(TERMINAL): e[1]+=count_elements(t)
    try: rlines=open(rpath).read().splitlines()
    except FileNotFoundError: rlines=[]
    keep=[]; res={}; nowts=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None,microsecond=0)
    def when(s):
        try: return dt.datetime.strptime(s,'%Y-%m-%dT%H:%M:%SZ')
        except ValueError: return None
    for l in rlines:
        f=l.split('|')
        if len(f)<5: continue
        f=f+['']*(10-len(f)); base=f[1].split(';')[0].split('_')[0]
        tasks=int(f[8]) if f[8].isdigit() and int(f[8])>0 else 1
        used_j,done_j=acct_base.get(base,(0.0,0)); inq=base in qids
        if not inq and done_j>=tasks: continue                         # reconciled: every task finished in accounting, where it counts now
        if inq: f[9]=nowts.strftime('%Y-%m-%dT%H:%M:%SZ')              # the queue shows it now
        elif base not in acct_base:
            t0=when(f[0]) or nowts; last=when(f[9])
            if nowts>max(t0+dt.timedelta(seconds=dur(f[4]) or 0),last or t0)+dt.timedelta(days=1):
                print(f"BUDGET: reservation {f[1]} ({f[2]}, {f[3]} GPU-h) expired: neither the queue nor accounting shows the job, a day after its time limit"
                      +(" and a day after the queue last showed it" if last else ''),file=sys.stderr); continue
        keep.append('|'.join(f))
        res[f[2]]=res.get(f[2],0)+max(0.0,float(f[3] or 0)-used_j-qcom.get(base,0.0))   # what the queue and accounting do not show yet
    for lane,h in res.items(): com[lane]=com.get(lane,0)+h
    if os.environ.get('FLEET_LOCK_HELD')=='1' and keep!=rlines:   # only a holder of the fleet lock rewrites the file
        try:
            tmp=rpath+'.tmp.'+str(os.getpid())
            with open(tmp,'w') as fh: fh.write(''.join(l+'\n' for l in keep))
            os.replace(tmp,rpath)
        except Exception as e: print(f"BUDGET: could not prune the reservations ({e!r}); they stay",file=sys.stderr)
    lanes=sorted(set(used)|set(com)|set(caps)); rows=[]
    for lane in lanes:
        cap=caps.get(lane); u=round(used.get(lane,0),3); c=round(com.get(lane,0),3)
        rows.append(dict(lane=lane,used=u,committed=c,reserved=round(res.get(lane,0),3),cap=cap,headroom=(round(cap-u-c,3) if cap is not None else None),queue=counts.get(lane,{'R':0,'PD':0,'other':0}),unbounded_jobs=unbounded.get(lane,[])))
    if a.json: print(json.dumps(rows,indent=1))
    else:
        print(f"{'lane':8s} {'used':>9s} {'committed':>10s} {'cap':>7s} {'headroom':>9s}  R/PD")
        for r in rows: print(f"{r['lane']:8s} {r['used']:9.2f} {r['committed']:10.2f} {('-' if r['cap'] is None else str(r['cap'])):>7s} {str(r['headroom'] if r['headroom'] is not None else '-'):>9s}  {r['queue']['R']}/{r['queue']['PD']}")
    if a.lane is None:
        for lane,j in sorted(unbounded.items()): print(f"BUDGET: lane {lane}: job(s) {' '.join(j[:5])} in the queue with no time limit (UNLIMITED, or one that cannot be read): no worst case, so the lane admits nothing more until they get one",file=sys.stderr)
    if a.lane is not None:
        cap=caps[a.lane]   # checked at the start: present, finite, non-negative
        if a.lane in unbounded:
            print(f"BUDGET: lane {a.lane} has job(s) {' '.join(unbounded[a.lane][:5])} in the queue with no time limit (UNLIMITED, or a limit that cannot be read): their worst case has no bound, so the lane admits nothing more; give them a limit (scontrol update JobId=ID TimeLimit=HH:MM:SS) or cancel them",file=sys.stderr); sys.exit(3)
        if used.get(a.lane,0)+com.get(a.lane,0)+a.est_hours>cap:
            print(f"BUDGET: lane {a.lane} would exceed its cap {cap}: used {used.get(a.lane,0):.2f} + committed {com.get(a.lane,0):.2f} + this job {a.est_hours:.2f}",file=sys.stderr); sys.exit(3)
if __name__=='__main__': main()
