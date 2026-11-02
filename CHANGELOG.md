# Changelog

## 0.1.0 (unreleased)

The first public release, extracted from the tooling of a fleet of AI agents supporting research and development on JUPITER Booster (internal version 1.3.11, 2 November 2026).

Included: the node launcher (`pack_node.py`), the submit wrapper and its guards, the fleet queue and dispatcher, the budget check, the waiter, the status screens, the allocation ledger, the copy-back helper, the git tools (`lanegit.sh`, `ffscoped.py`) and size hooks, the probes, example specs, agent instruction templates, the test suite with its fake Slurm, and the documentation.

Changed for the public release:

- Clarify the company's research and development work, team collaboration and designated responsibility for job approvals; attribute copyright to Robert Bjarnason & Eidosoma.ai.
- Documentation: clarify how a year of running agent fleets shaped the first version, with further lessons from the JUPITER deployment; distinguish potential risks from observed incidents.
- Images: a README logo featuring Ganymede serving Jupiter a green drink beside a supercomputer and symbolic job queue, plus a PNG under 510 KB for social sharing.
- Site files: a generic template (`site.example.env`), JUPITER Booster's settings without an account, and a workstation stand-in. Whole-node charging is a site key (`CHARGE_WHOLE_NODES`) instead of following the site's name.
- The batch script's directory variable is `FLEET_DIR`.
- `lanegit.sh`: the checkout defaults to the one you are in (`LANEGIT_ROOT` overrides); a token file is optional (`FLEET_GIT_TOKEN_FILE`), otherwise your own git credentials apply; lane folders for `check` are configurable (`LANEGIT_LANES_PREFIX`); the new-file size limit is configurable (`FLEET_MAX_NEW_FILE_KB`).
- `ffscoped.py`: the same defaults for the checkout and the token.
- The NVML bindings are a dependency (`nvidia-ml-py`) instead of a vendored copy.
- `probes/` (formerly the calibration lane's folder); a generic smoke test.
- The tests use their own site file and neutral lane tags.

New in the public release: `tools/login_check.py` and `docs/login-nodes.md`, the fleet's login-node practice as a tool and a page: look before you run (load, least-busy cores, memory against the per-user limit, other users on the login GPU), then run with what the check allows. Tested by `tests/fleet_guard/test_login_check.sh` (run_tests.sh section 21).

## Internal history

The versions below were the internal releases during the first deployment, September to November 2026.

- **1.3.11**: the budget check reads the account only, in weekly UTC windows, each job once, with finished windows cached (after the first run a check reads only the current week, instead of the whole period on every submit); the ledger reads the same way; the waiter polls every ten minutes by default, never faster than once a minute.
- **1.3.10**: `git/ffscoped.py`, the scoped fast-forward of a shared checkout; `git merge` retired there.
- **1.3.9**: the GPU-idle watchdog in every job, with a timestamped line in the job log and the idle times in the receipt.
- **1.3.8**: the site's MPS opened to every lane through `--comment "CUDA_MPS=1"`.
- **1.3.7**: GPU feeders take the highest cores of their processor.
- **1.3.6**: the lowest cores are kept away from GPU feeders (slot 0 had run 15 to 30 % slower).
- **1.3.5**: spec paths made absolute before `sbatch`.
- **1.3.4**: one fleet queue, lock and release log for every copy of the tooling.
- **1.3.3**: accept and hold instead of refusing when the fleet is full; the dispatcher; the duplicate, rate and array guards; `--no-requeue`; fail-closed budget check and waiter; the fake-Slurm test suite.
- **1.3.2**: a multi-node job's GPUs no longer counted once more per node.
- **1.3.1**: the batch shell traps and forwards Slurm's checkpoint warning.
- **1.3**: site job overrides through `--comment`; the concurrency guard; the multi-node qualification.
- **1.2.1**: accounting in UTC.
- **1.2**: GPM GPU telemetry and a per-slot activity summary in every receipt.
- **1.1**: a clean `SIGUSR1` interruption is a checkpoint segment, not a failure.
- **1.0**: the first site file, caps, the launcher, the git size hooks.
- **October 2026**: `lanegit.sh`, git for many agents in one checkout, with private-index commits and merge-tree pushes.
