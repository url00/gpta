PYTHON ?= python3
# Extra server flags, e.g. make dev ARGS="--port 9000". Host and port
# otherwise come from data/config.json.
ARGS ?=

.PHONY: dev test lint format start stop restart install-systemd

dev:
	$(PYTHON) -m gpta $(ARGS)

test:
	$(PYTHON) -m unittest discover -s tests -v

# ruff is in the Nix dev shell (nix develop).
lint:
	ruff check .
	ruff format --check .

format:
	ruff format .
	ruff check --fix .

start:
	./scripts/start.sh

stop:
	./scripts/stop.sh

restart:
	./scripts/restart.sh

install-systemd:
	./scripts/install-systemd.sh
