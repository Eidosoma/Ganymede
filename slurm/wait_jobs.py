#!/usr/bin/env python3
"""wait_jobs.py JOBID [JOBID...] [--poll 600] [--receipts DIR] [--submissions FILE | --site NAME] [--expect BASE:SPEC]: block until
every job has left the queue, then print one line per job (one per array element) with its Slurm state and its receipts (all_ok, wall,
GPU-slot-hours). Exit 0 if every job, and every element of an array, completed and left a receipt with all_ok on every node it ran on;
else 1.
A JOBID is a job, a whole array (its base id) or one element (BASE_INDEX). An array is followed element by element, as accounting lists
it (BASE_INDEX, or BASE_[a-b] for elements that never started); an element's receipts are under its own job id, RECEIPTS/NAME-ID-aINDEX,
as slurm/node.sbatch writes them; a job on N nodes needs N receipts, RECEIPTS/NAME-ID/node-0 ... node-(N-1). (The waiter used to look
for an accounting record under the base id, which an array never has, so it waited forever; and for one receipt beside the job folder,
so a multi-node job read "no receipt".)
An array's element set comes from the fleet's submission record (FLEET_SUBMISSIONS_FILE, one line per submission, written by submit.sh
under the fleet lock): the wait ends only when accounting shows every element ended, as its own row or inside a range of
elements that never started. Taking whatever rows accounting returns as the whole array would miss an element that accounting
does not show yet, and an array with a failed element could report success. An array the record does not
have cannot be verified (exit 1, saying so) unless --expect BASE:SPEC gives its --array spec. The record: --submissions FILE, else
FLEET_SUBMISSIONS_FILE in the environment, else the one --site NAME's site file names, else receipts/FLEET_SUBMISSIONS.log beside the
tooling.
Meant to be run in the background by an agent (it is woken when this exits), or in a Monitor loop."""
import argparse, json, os, re, subprocess, sys, time
HERE=os.path.dirname(os.path.abspath(__file__))
DEFAULT_POLL=600.0   # one watcher a job, checking every ten minutes: the scheduler is shared by every user of the machine
MIN_POLL=60.0        # never faster than once a minute, whatever --poll says
TERMINAL=('COMPLETED','FAILED','CANCELLED','TIMEOUT','NODE_FAIL','OUT_OF_MEMORY','BOOT_FAIL','DEADLINE','PREEMPTED')
SPAN=re.compile(r'(\d+)(?:-(\d+)(?::(\d+))?)?')
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
def holds(task, k):
    """Does an accounting task field ('3', '[2-5]', '[1,3,7-9:2]') name element k? Unreadable: it does not. (covers() assumes a field it
    cannot read includes the element, which is right for deciding what to wait for; for deciding that nothing is missing it is not.)"""
    for part in task.strip('[]').split('%')[0].split(','):
        m=SPAN.fullmatch(part.strip())
        if m:
            a=int(m.group(1)); b=int(m.group(2) or a); s=int(m.group(3) or 1)
            if a<=k<=b and (k-a)%max(s,1)==0: return True
    return False
def elements(spec):
    """An --array spec ('0-9:2,11%3') -> its element indices, the throttle dropped; None when it cannot be read."""
    out=set()
    for part in spec.split('%')[0].split(','):
        m=SPAN.fullmatch(part.strip())
        if not m: return None
        a=int(m.group(1)); b=int(m.group(2) or a); s=int(m.group(3) or 1)
        if b<a or s<1: return None
        out.update(range(a,b+1,s))
    return out or None
def site_value(site, key):
    """KEY's value in slurm/site.SITE.env (the default of a ${KEY:-default} form); '' when the file or the key is absent."""
    try: lines=open(os.path.join(HERE,f'site.{site}.env')).read().splitlines()
    except OSError: return ''
    for l in lines:
        l=l.split('#')[0].strip()
        if l.startswith(key+'='):
            v=l.split('=',1)[1].strip().strip('"').strip("'"); m=re.fullmatch(r'\$\{'+re.escape(key)+r':-(.*)\}',v)
            return m.group(1) if m else v
    return ''
def record(path):
    """The fleet's submission record: {job id: its --array spec, '' for a plain job}; {} while there is none. Lines:
    UTC|job|name|lane|array (- for none)|tasks|nodes|time limit|dependency."""
    d={}
    try: lines=open(path).read().splitlines()
    except FileNotFoundError: return d
    for l in lines:
        f=l.split('|')
        if len(f)>=5 and f[1].isdigit(): d[f[1]]='' if f[4] in ('','-') else f[4]
    return d
def wanted(base, task, want):
    """Is a queue or accounting row (base id, task field) part of a requested job, array or element?"""
    return any(base==b and (not t or not task or covers(task,t)) for b,t in want)
def states(bases):
    """Accounting rows of these base ids, one per job or array element: {JobID: row}; None when the read failed. A failed read is
    never a picture of the jobs, however much it printed."""
    try: r=subprocess.run(['sacct','-X','-n','-P','-j',','.join(bases),'--format=JobID,JobName,State,ElapsedRaw,JobIDRaw,NNodes'],capture_output=True,text=True,timeout=300)
    except Exception: return None
    if r.returncode!=0: return None
    d={}
    for l in r.stdout.strip().splitlines():
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
    ap.add_argument('--submissions',default='',help="the fleet's submission record (default: FLEET_SUBMISSIONS_FILE, the --site file's, or receipts/FLEET_SUBMISSIONS.log beside the tooling)")
    ap.add_argument('--site',default='',help="read the record's path from slurm/site.SITE.env")
    ap.add_argument('--expect',action='append',default=[],metavar='BASE:SPEC',help="an array's element set when the record does not have it: its --array spec, e.g. 12345:0-29")
    a=ap.parse_args()
    want=[split_id(j) for j in a.jobs]; bases=sorted({b for b,_ in want})
    a.poll=max(a.poll,MIN_POLL)   # the scheduler is shared: never poll faster than once a minute
    path=a.submissions or os.environ.get('FLEET_SUBMISSIONS_FILE') or (site_value(a.site,'FLEET_SUBMISSIONS_FILE') if a.site else '') or os.path.join(os.path.dirname(HERE),'receipts','FLEET_SUBMISSIONS.log')
    given={}
    for e in a.expect:
        b,_,spec=e.partition(':')
        if not b.isdigit() or elements(spec) is None: ap.error(f'--expect {e}: BASE:SPEC, for example 12345:0-29')
        given[b]=spec
    def sets():
        """The element set of every whole array asked for, from --expect or the record (read again each time: a waiter may start the
        moment its job id is printed); None for an array the record has with a spec that cannot be read."""
        rec=dict(record(path)); rec.update(given)
        return {b:elements(rec[b]) for b,t in want if not t and rec.get(b)}
    def rows(st, b, t): return [v for v in st.values() if v['base']==b and (not t or not v['task'] or covers(v['task'],t))]
    def ended(st, b, t, expect):
        rs=rows(st,b,t)
        if not rs or not all(v['state'].startswith(TERMINAL) for v in rs): return False
        e=None if t else expect.get(b)   # a whole array: every element accounted for; one element: its own rows
        return e is None or all(any(v['task'] and holds(v['task'],k) for v in rs) for k in e)
    while True:
        q=queued(bases, want)
        if q is not None and not q:
            st=states(bases)   # the queue is empty: believe it only when accounting agrees, in a read that succeeded, that every job has ended
            expect=sets()
            if st is not None and all(ended(st,b,t,expect) for b,t in want): break
        time.sleep(a.poll)
    ok=True; shown=set()   # the report comes from that same read: a later one that failed, or came back partial, cannot drop a job from it
    for b,t in want:
        for v in sorted(rows(st,b,t), key=lambda v:(len(v['task']), v['task'])):
            if v['id'] in shown: continue
            shown.add(v['id']); line,good=report(v, a.receipts); ok = ok and good; print(line)
        if not t and any(v['task'] for v in rows(st,b,t)) and expect.get(b) is None:
            ok=False
            print(f"array {b}: its element set cannot be verified (no readable --array spec for it in the submission record {path}); "
                  f"accounting showed {sum(1 for v in rows(st,b,t) if v['task'])} row(s); give it with --expect {b}:SPEC, the --array it was submitted with")
    sys.exit(0 if ok and shown else 1)
if __name__=='__main__': main()
