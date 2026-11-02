# Contributing

Thank you for helping. Ganymede guards a shared, expensive resource, so changes are judged first on whether they can fail open.

## Before you open a pull request

- Run the suite: `bash tests/fleet_guard/run_tests.sh` must end with `0 failed`.
- Every new guard, and every change to a guard, comes with a check in the suite, including what happens when Slurm answers wrongly or not at all. Extend the fakes in `tests/fleet_guard/bin/` when you need a new Slurm behaviour.
- Update the documentation the change touches, and add a line to `CHANGELOG.md`.

## Principles

- **Fail closed.** If the queue or the accounting cannot be read, do nothing. Never read silence as zero.
- **No loops, no retries** in anything that submits, releases or cancels.
- **User space only.** No daemons, no databases, no root. Shared state lives in plain files on the shared file system, guarded by atomic `mkdir` locks.
- **Small and auditable.** Bash and the Python standard library. A new dependency needs a strong reason.
- **Evidence.** Anything that costs GPU-hours leaves a receipt.

## Style

- Bash: `set -euo pipefail` (or `set -u` where a failing command must be handled), quoted expansions, exit codes documented in the header.
- Python 3.9 compatible, standard library only, one file per tool where possible.
- Messages say what was refused, why, and what to do instead, in plain words. An agent will read them.

## Reporting a problem

Open an issue with the command you ran, its full output, the site file (without your account name) and, for job problems, the receipt. For security problems, see [`SECURITY.md`](SECURITY.md).
