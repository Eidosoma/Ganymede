# Roadmap

What we would like to build next, roughly in order. Discussion and help are welcome.

1. **An enforceable sign-off.** Today the one-word-per-job rule is enforced by the agents' instructions and checked afterwards by a reviewer. Next: a single-use approval token, issued by the principal for one job and bound to its name, time limit, worst case and spec hash, which `submit.sh` verifies and spends. Agents propose, a human approves, the machine enforces.
2. **An agent tool interface.** A small server exposing submit, status, budget, wait and receipt as tools (for example over the Model Context Protocol), so any agent framework uses the guardrails natively instead of learning the scripts.
3. **Packaging.** An installable package with command-line entry points, and a published test suite run in continuous integration.
4. **More sites.** Site files for other GPU systems (at JSC: JUWELS Booster, JURECA-DC), and reading the site's own quota tool beside accounting.
5. **Accounting by account.** Count usage with `sacct -A <account>` for sites where all fleet jobs are visible that way, and support several accounts.
6. **Receipts as data.** Export receipts as a dataset (GPU activity, wall times, failures per lane), dashboards, and links to site monitoring such as LLview.
7. **Multi-node beyond two nodes**, `torchrun`, and the fleet queue's release at the end of a multi-node job.
8. **ffscoped for any branch**, not only `main`.
