# Security

## What the tools can do

Everything runs with the permissions of the user who calls it. The tools submit, hold and release that user's Slurm jobs, read accounting, write files in the tooling folder and the receipt folders, and run git in the shared checkout. They never need root, install nothing system-wide, and run no daemon.

## Secrets

- **Git tokens.** `lanegit.sh` and `ffscoped.py` use your own git credential setup by default. If you set `FLEET_GIT_TOKEN_FILE`, the file is read only by git's credential helper at the moment of a fetch or push; its content never appears on a command line, and the tools filter their output for token-like strings. Keep the file readable by its owner only.
- **Specs and site files** are plain text and end up in receipts and logs. Never put passwords, tokens or keys in a task's command or environment.
- **Receipts** record each task's command and working directory, the host, and the Slurm job's id, name, nodes, GPUs, partition and account. Review them before publishing.

## Agents

The tools limit what an agent can spend and how fast it can submit, but they do not sandbox the commands in a spec: a job runs whatever its spec says, as the user. Treat an agent's spec like any code it writes, and keep a human's approval in the loop ([docs/operating-model.md](docs/operating-model.md)).

## Reporting a vulnerability

Please report vulnerabilities privately to the maintainers at [robert@eidosoma.ai](mailto:robert@eidosoma.ai) rather than in a public issue. We will acknowledge the report, work on a fix, and credit you if you wish.
