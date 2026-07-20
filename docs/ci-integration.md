# CI integration

quacklint is designed to run in CI: stable exit codes (`0` all pass, `1` checks
failed, `2` invalid configuration) and `junit` output so the platform shows
each check as a test.

## GitHub Actions

Validate the suite on every PR (available today):

```yaml
name: data-quality

on:
  pull_request:

jobs:
  quacklint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync
      - run: uv run quacklint validate quality/suite.yaml
```

Run the checks and publish the results as a JUnit report:

```yaml
      - run: uv run quacklint run quality/suite.yaml --format junit > results.xml
      - uses: mikepenz/action-junit-report@v5
        if: always()
        with:
          report_paths: results.xml
```

## Tips

- Version the YAML suites alongside the code that produces the data: rule
  changes go through review just like code.
- Use `quacklint validate` as a fast step (it doesn't touch the data) and
  `quacklint run` in the job that already has access to the files.
