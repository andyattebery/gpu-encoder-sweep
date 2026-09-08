# The gate. Named modules, never discovered; no `-` prefixes, no `|| true`.
# Run as `uv run make check` after `uv sync --locked --extra hub --extra cli`, so $(PY) is the locked environment's.
PY ?= python3
TESTS = tests.test_model_check tests.test_recipes tests.test_hub_store tests.test_hub_app tests.test_hub_catalogue tests.test_cli tests.test_hub_sample tests.test_hub_search tests.test_hub_decision tests.test_hub_ingest tests.test_hub_queue tests.test_hub_artifact tests.test_hub_exchange tests.test_hub_build tests.test_hub_planner tests.test_hub_export tests.test_refusals tests.test_hub_replay
.PHONY: check test model docs integration
check: test model docs
test:
	$(PY) -m unittest $(TESTS)
model:
	$(PY) sweep/model_check.py --mutate
docs:
	$(PY) sweep/model_check.py --check
# The integration suite: the queue contract and the agent protocol against a real Redis. Needs SWEEP_TEST_REDIS set to a
# redis:// URL of a database the suite may flush; the guard is its own recipe line, so a missing variable stops make here.
INTEGRATION = tests.integration.test_redis_queue
integration:
	test -n "$$SWEEP_TEST_REDIS"
	$(PY) -m unittest $(INTEGRATION)
