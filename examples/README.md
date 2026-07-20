# Examples

Each directory contains a small dataset and a working YAML suite:

- [taxi](taxi/) — taxi trips in CSV with deliberate errors; uses all 8 check
  types from the contract and shows what failures look like.

To run any of them:

```console
$ uv run quacklint run examples/<name>/quacklint.yaml
```
