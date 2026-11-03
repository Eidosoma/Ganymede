#!/usr/bin/env python3
"""timelimit.py: one reading of a Slurm time limit. The submit wrapper takes from it what sbatch is given, what the
budget is charged, what the review threshold sees and what the reservation records; budget.py reads the queue's limits with it.

  timelimit.py LIMIT                         prints 'MINUTES NORMALIZED', e.g. '00:20:01' -> '21 00:21:00'; exit 2 if refused
  timelimit.py --gpu-hours MINUTES N [N..]   prints MINUTES / 60 x N x .., rounded UP to 0.001 GPU-hours, e.g. 1 4 1000 1 -> 66.667

Slurm's rules (src/common/parse_time.c, time_str2secs and time_str2mins, Slurm 25.05): 'minutes', 'minutes:seconds',
'hours:minutes:seconds', 'days-hours', 'days-hours:minutes', 'days-hours:minutes:seconds', with seconds rounded UP to a whole minute;
'-1', 'INFINITE' and 'UNLIMITED' mean no time limit, and sbatch reads a limit of 0 the same way. A limit that is zero, unlimited or
not one of those forms is refused: a budget needs a bounded worst case. (Read from the text as given, the hours once priced
00:00:01 as one second of a job Slurm runs for a minute, '20:00', twenty minutes to Slurm, as twenty hours, and 0, no limit, as
nothing.) The normalized limit is HH:MM:00, or D-HH:MM:00 from a day up, which Slurm reads back unchanged."""
import re
import sys

D = '[0-9]+'   # ASCII digits only, as Slurm reads them (Python's \\d would also take other scripts' digits)
FORMS = (      # each form and its seconds, in Slurm's order of reading
    (re.compile(f'({D})'), lambda g: int(g[0]) * 60),
    (re.compile(f'({D}):({D})'), lambda g: int(g[0]) * 60 + int(g[1])),
    (re.compile(f'({D}):({D}):({D})'), lambda g: int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2])),
    (re.compile(f'({D})-({D})'), lambda g: int(g[0]) * 86400 + int(g[1]) * 3600),
    (re.compile(f'({D})-({D}):({D})'), lambda g: int(g[0]) * 86400 + int(g[1]) * 3600 + int(g[2]) * 60),
    (re.compile(f'({D})-({D}):({D}):({D})'), lambda g: int(g[0]) * 86400 + int(g[1]) * 3600 + int(g[2]) * 60 + int(g[3])),
)
NO_LIMIT = ('-1', 'infinite', 'unlimited')
LARGEST = 2 ** 31 - 1   # Slurm holds the seconds in a 32-bit int


def seconds(text):
    """A Slurm time or duration in any of the six forms -> seconds, not rounded (squeue prints its limits and elapsed times in three
    of them: M:SS, H:MM:SS, D-HH:MM:SS); None for no limit (-1, INFINITE, UNLIMITED) and for anything that is not one of the forms
    (NOT_SET, N/A, INVALID, a typing error)."""
    t = text or ''
    if t.lower() in NO_LIMIT:
        return None
    for pattern, secs in FORMS:
        m = pattern.fullmatch(t)
        if m:
            s = secs(m.groups())
            return s if s <= LARGEST else None
    return None


def minutes(text):
    """A requested time limit -> whole minutes, rounded up as Slurm rounds it; ValueError naming the reason when it is refused."""
    t = text or ''
    if t.lower() in NO_LIMIT:
        raise ValueError(f"'{t}' asks for no time limit")
    s = seconds(t)
    if s is None:
        raise ValueError(f"'{t}' is not a Slurm time limit (minutes, minutes:seconds, hours:minutes:seconds, days-hours, "
                         "days-hours:minutes or days-hours:minutes:seconds)")
    m = -(-s // 60)
    if m == 0:
        raise ValueError(f"'{t}' is a zero time limit, which Slurm reads as no time limit")
    return m


def normalized(m):
    """Whole minutes -> HH:MM:00, or D-HH:MM:00 from a day up."""
    d, r = divmod(m, 1440)
    h, mm = divmod(r, 60)
    return (f'{d}-' if d else '') + f'{h:02d}:{mm:02d}:00'


def gpu_hours(m, *factors):
    """Whole minutes x whole factors (GPUs per node, tasks, nodes) -> GPU-hours rounded UP to 0.001, in integers until the division,
    so the admission arithmetic never rounds down. Printed as Python prints the float: 4.0, 1.4, 66.667."""
    n = m
    for f in factors:
        n *= f
    return (n * 1000 + 59) // 60 / 1000


def main(argv):
    if argv[:1] == ['--gpu-hours']:
        try:
            ns = [int(x) for x in argv[1:]]
        except ValueError:
            ns = []
        if len(ns) < 2 or min(ns) < 0:
            print('timelimit: --gpu-hours takes the minutes and at least one factor, all whole and not negative', file=sys.stderr)
            return 2
        print(gpu_hours(*ns))
        return 0
    if len(argv) != 1:
        print(__doc__.split('\n\n')[1], file=sys.stderr)
        return 2
    try:
        m = minutes(argv[0])
    except ValueError as e:
        print(f'refused: the time limit {e}; give a bounded limit, for example --time 01:30:00 (nothing was submitted)', file=sys.stderr)
        return 2
    print(m, normalized(m))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
