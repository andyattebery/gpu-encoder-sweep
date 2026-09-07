# The gate. Named modules, never discovered; no `-` prefixes, no `|| true`.
PY ?= python3
.PHONY: check test model docs
check: test model docs
test:
	$(PY) -m unittest tests.test_model_check
model:
	$(PY) sweep/model_check.py --mutate
docs:
	$(PY) sweep/model_check.py --check
