# The unit tests do not need flux. The t/ tests need flux-core and the hold
# branch of vsoch/flux-sched, which has the coschedule queue policy.
PY ?= python3
export PYTHONPATH := $(CURDIR)
export FLUX_CLI_PLUGINPATH := $(CURDIR)/cli-plugins

.PHONY: test unit check-flux
test: unit check-flux
	bash t/t0001-shape.sh
	bash t/t0002-shell.sh
	bash t/t0003-concurrent.sh
	bash t/t0004-multinode.sh
	bash t/t0005-jobscope.sh
	bash t/t0006-external-queue.sh
	bash t/t0007-add.sh

unit:
	$(PY) -m unittest discover -s test -p 'test_*.py' -v

check-flux:
	@command -v flux >/dev/null || { echo "flux not found"; exit 1; }
	@test -f "$$(flux config builtin libdir 2>/dev/null || echo /usr/lib)/flux/modules/sched-fluxion-qmanager.so" \
	  || ls /usr/lib/flux/modules/sched-fluxion-qmanager.so >/dev/null 2>&1 \
	  || { echo "sched-fluxion-qmanager.so not installed"; exit 1; }
