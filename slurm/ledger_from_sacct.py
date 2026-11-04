#!/usr/bin/env python3
"""Turn Slurm accounting into the fleet's allocation ledger.

  ledger_from_sacct.py --since 2026-01-01 [--sacct-file dump.txt] [--caps caps.json] [--ledger ALLOCATION_LEDGER.md] [--gpus-per-node 4]

Reads `sacct -a -X -P -n --format=JobID,JobName,Account,Partition,AllocNodes,AllocTRES,ElapsedRaw,State,Start,End` (or a saved dump
of it), charges each job gpus * elapsed (gpus from AllocTRES gres/gpu, else nodes * gpus-per-node), assigns the lane from the job
name prefix before the first '-', appends one row per new JobID to the ledger table (idempotent), and prints per-lane totals against
the caps. Charged whole-node hours are what the allocation loses; that is what is recorded.
--account ACCT reads that account only (sacct -A). Accounting is read in UTC windows of --window-days (7), each job once (the last
reading wins), so no single reply grows large.
"""
import argparse, datetime, json, os, re, subprocess, sys
FIELDS='JobID,JobName,Account,Partition,AllocNodes,AllocTRES,ElapsedRaw,State,Start,End,AllocCPUS'
def rows(args):
    if args.sacct_file: texts=[open(args.sacct_file).read()]
    else:
        base=['sacct','-a','-X','-P','-n']+(['-A',args.account] if args.account else [])
        def run(extra): return subprocess.run(base+extra+['--format',FIELDS],capture_output=True,text=True,check=True,env={**os.environ,'TZ':'UTC'}).stdout   # accounting times in UTC, whatever the login node's clock
        start=None
        for f in ('%Y-%m-%dT%H:%M:%S','%Y-%m-%d'):
            try: start=datetime.datetime.strptime(args.since,f); break
            except ValueError: pass
        if start is None: texts=[run(['--starttime',args.since])]
        else:
            now=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None,microsecond=0); texts=[]; ws=start
            while ws<now:
                we=min(ws+datetime.timedelta(days=args.window_days),now); texts.append(run(['--starttime',ws.isoformat(),'--endtime',we.isoformat()])); ws=we
    out=[]
    for line in '\n'.join(texts).strip().splitlines():
        if not line.strip(): continue
        f=line.split('|')
        if len(f)<11: continue
        jid,name,acct,part,nodes,tres,el,state,start,end,cpus=f[:11]
        m=re.search(r'gres/gpu=(\d+)',tres); gpus=int(m.group(1)) if m else (int(nodes or 0)*args.gpus_per_node if args.charge_node_gpus else 0)
        try: cpus=int(cpus)
        except ValueError: cpus=0
        try: el=int(el)
        except ValueError: el=0
        out.append(dict(job=jid,name=name,account=acct,partition=part,nodes=int(nodes or 0),gpus=gpus,cpus=cpus,elapsed_s=el,state=state,start=start,end=end,
                        gpu_hours=round(gpus*el/3600,4),core_hours=round(cpus*el/3600,4),lane=name.split('-')[0] if name else '?'))
    byjob={}
    for r in out: byjob[r['job']]=r   # a job read by two windows is kept once: the last reading
    return list(byjob.values())
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--since',default='2026-01-01'); ap.add_argument('--sacct-file'); ap.add_argument('--caps',default='caps.json')
    ap.add_argument('--ledger',default='ALLOCATION_LEDGER.md'); ap.add_argument('--gpus-per-node',type=int,default=4)
    ap.add_argument('--charge-node-gpus',action='store_true',help='whole-node sites (JUPITER): a job with no gres/gpu is still charged nodes x gpus-per-node')
    ap.add_argument('--account',default='',help='read this account only (sacct -A)'); ap.add_argument('--window-days',type=float,default=7.0,help='read accounting in UTC windows of this many days (default 7)'); a=ap.parse_args()
    # caps through the budget check's own validation; a report, so an unusable file or entry is named, never fatal (reading any JSON as
    # caps, with a cap formatted as an integer, once crashed the summary on 1.5 after the ledger was written)
    sys.path.insert(0,os.path.dirname(os.path.abspath(__file__))); from budget import load_caps
    caps,badcaps,capnote=load_caps(a.caps)
    jobs=rows(a)
    # Rows are idempotent per JobID, except that a row written while the job was still queued or running (a non-terminal
    # state) is replaced when the job is next seen, so the charged hours are final ones. Terminal rows are never rewritten.
    NONTERMINAL=('RUNNING','PENDING','CONFIGURING','COMPLETING','SUSPENDED','REQUEUED','RESIZING')
    header='# Allocation ledger\n\nOne row per Slurm job (whole-node hours charged). Totals per lane are recomputed from the rows. A row first written while the job was running is replaced with the final accounting once the job ends. Start and end are UTC.\n\n| job | lane | name | partition | nodes | gpus | cores | elapsed s | GPU-hours | core-hours | state | start (UTC) | end (UTC) |\n|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---|---|\n'
    lines=open(a.ledger).read().splitlines(keepends=True) if os.path.exists(a.ledger) else header.splitlines(keepends=True)
    def row(j): return f"| {j['job']} | {j['lane']} | {j['name']} | {j['partition']} | {j['nodes']} | {j['gpus']} | {j['cpus']} | {j['elapsed_s']} | {j['gpu_hours']} | {j['core_hours']} | {j['state']} | {j['start']} | {j['end']} |\n"
    byid={j['job']:j for j in jobs}; seen=set(); refreshed=0; out=[]
    for l in lines:
        if l.startswith('| ') and not l.startswith('| job'):
            c=[x.strip() for x in l.split('|')[1:-1]]; jid=c[0]; seen.add(jid)
            if len(c)>=11 and c[10].split()[0] in NONTERMINAL and jid in byid and byid[jid]['state'].split()[0] not in NONTERMINAL:
                out.append(row(byid[jid])); refreshed+=1; continue
        out.append(l)
    new=[j for j in jobs if j['job'] not in seen]
    out.extend(row(j) for j in new)
    open(a.ledger,'w').write(''.join(out))
    if refreshed: print(f'{refreshed} row(s) refreshed with final accounting')
    tot={}; ch={}
    for l in open(a.ledger):
        if l.startswith('| ') and not l.startswith('| job'):
            c=[x.strip() for x in l.split('|')[1:-1]]
            if len(c)<10: continue
            tot[c[1]]=tot.get(c[1],0)+float(c[8]); ch[c[1]]=ch.get(c[1],0)+float(c[9])
    print(f'{len(new)} new job rows appended; totals (GPU-hours):')
    if capnote: print(f'  (caps: {capnote}; the totals are shown without caps)')
    for lane in sorted(set(tot)|set(caps)|set(badcaps)):
        cap=caps.get(lane); used=round(tot.get(lane,0),2)
        capcol=((f"  cap {cap:9.2f}  left {cap-used:9.2f}" + ("  OVER CAP" if used>cap else "")) if cap is not None else
                (f"  (not a usable cap: {badcaps[lane]!r})" if lane in badcaps else "  (no cap)"))
        print(f"  {lane:8s} used {used:9.2f}" + capcol + f"  core-hours {round(ch.get(lane,0),2):9.2f}")
    print(f"  {'total':8s} used {round(sum(tot.values()),2):9.2f}  cap {sum(caps.values()):9.2f}")
if __name__=='__main__': main()
