# Contributing

- Bugs and app reports: open an issue with the template. For app problems, attach the app log (webOS ATL → Android
  apps → the app → App log, or `/media/developer/apps/usr/palm/applications/org.webosatl.app.<name>/app.log`) and
  the compat report.
- Pull requests: keep them focused. Python on the TV must use only the standard library, and shell scripts must run
  under busybox `sh`.
- Changes to ATL: add or edit a `runtime/tvpatches/*.py` script, regenerate `runtime/patches/` (see
  [docs/BUILDING.md](docs/BUILDING.md)), and run the smoke test (`runtime/smoke/smoke.py --tv`) before and after.
- Run `python3 tv/test_atld_memory.py` after touching `tv/atld.py`.
- Please don't open issues or PRs about piracy apps or sites. They will be closed.
