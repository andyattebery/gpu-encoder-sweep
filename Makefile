# The gate. Named modules, never discovered; no `-` prefixes, no `|| true`.
# Run as `uv run make check` after `uv sync --locked --extra hub --extra cli`, so $(PY) is the locked environment's.
PY ?= python3
TESTS = tests.test_model_check
.PHONY: check test model docs
check: test model docs
test:
	$(PY) -m unittest $(TESTS)
model:
	$(PY) sweep/model_check.py --mutate
docs:
	$(PY) sweep/model_check.py --check
