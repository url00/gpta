PYTHON ?= python3
# Extra server flags, e.g. make dev ARGS="--port 9000". Host and port
# otherwise come from data/config.json.
ARGS ?=

.PHONY: dev test start stop restart install-systemd

dev:
	$(PYTHON) -m gpta $(ARGS)

test:
	$(PYTHON) -m unittest discover -s tests -v

start:
	./scripts/start.sh

stop:
	./scripts/stop.sh

restart:
	./scripts/restart.sh

install-systemd:
	./scripts/install-systemd.sh
