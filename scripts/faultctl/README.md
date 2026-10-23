# faultctl

Inject and reset practice incidents on the local demo (F18). This is a
**human/test tool**: the agent under `oncall/` must never import, call or read
it (CI `rules` job enforces this).

```bash
python scripts/faultctl/faultctl.py list
python scripts/faultctl/faultctl.py inject scenario-06
python scripts/faultctl/faultctl.py status
python scripts/faultctl/faultctl.py reset            # always run when done
```

- Scenarios live in `scenarios.yaml` with neutral ids. Types: `stop`,
  `cpu_limit`, `mem_limit`. Flag scenarios are not supported yet (the flagd
  write mechanism is unverified).
- Only containers of the compose project `opentelemetry-demo` are touched.
- `inject` records the original state and limits in `.state.json`
  (gitignored) before changing anything.
- `reset` restores every recorded change, starts any exited container of the
  project, then waits up to 120 s (`--wait`) until all demo containers are
  running and not `starting`/`unhealthy`. Exit code 1 if they are not. Safe to
  run repeatedly.
- `cpu_limit` uses `--cpu-period/--cpu-quota`, not `--cpus`: a `--cpus` limit
  cannot be removed with `docker update` (verified on Docker 29.1.5), while
  `--cpu-quota -1` restores "unlimited". If a container already has a `--cpus`
  limit, faultctl uses `--cpus` and restores the original value.
- `mem_limit` sets `--memory` and `--memory-swap` to the same value; reset
  restores both originals.

Tests: `pytest -q scripts/faultctl/` (docker is mocked).
