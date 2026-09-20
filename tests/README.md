# Tests

The test suite uses `pytest` to exercise the CLI, workflow state, evidence,
runtime adapters, and release tooling without reading or writing your normal
Agent Orchestra configuration and job storage.

## Offline suite

Run the ordinary offline suite in parallel:

```shell
mise run tests
```

This uses one worker per available CPU and distributes tests with a work-stealing
scheduler. Use `mise run tests-serial` when you need ordered, readable failure
output; it runs the same suite in one process.

Most offline test modules live directly under `tests/`. Shared CLI helpers and
fixtures are in `cli_helpers.py` and `conftest.py`; files under `data/` are
fixture input rather than collected tests.

## Live runtime checks

The tests under `live/` invoke authenticated vendor CLIs and are skipped by the
ordinary suite. Install the corresponding bundled role skills, then run them
explicitly with `mise run test-live-claude` or `mise run test-live-codex`; see the
[CLI guide](../docs/cli.md#opt-in-live-runtime-verification) for prerequisites,
isolation guarantees, and usage-cost expectations.
