# GPTA agent notes

## Required for every release

**Bump the version with every release.** A release is any change committed
and pushed to `main`. Before committing, raise `version` in
`pyproject.toml`, the single source of the version number (`gpta/__init__.py`
and `flake.nix` read it from there):

- **Patch** (`0.2.0` → `0.2.1`): fixes, docs, and internal changes with no
  new behaviour.
- **Minor** (`0.2.1` → `0.3.0`): new features, new or removed modules, and
  changes to the module API (`gpta/module_api.py`), the HTTP API, or the
  config and data formats in `data/`.

Bump once per release, not once per commit within it, and mention the new
version in the commit message. After restarting, check that `/api/state`
reports it under `server.version`.

## Checks before a release

```bash
.nix-python/bin/python3 -m unittest discover -s tests
```

```bash
nix develop -c sh -c 'ruff check . && ruff format --check .'
```

Restart with `./scripts/restart.sh` (it restarts the `gpta` systemd unit and
waits for the health check). New files must be `git add`ed before a Nix build
sees them.
