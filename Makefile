PYTHON ?= python3
PORT ?= 8787

.PHONY: dev test start stop restart install-systemd

dev:
	$(PYTHON) -m gpta --port $(PORT)

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
