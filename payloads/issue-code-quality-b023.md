## Finding
Running the current ruff release (0.16.x) with the repository's own `[tool.ruff]` configuration reports:

```
superset/sql/dialects/starrocks.py:132:60: B023 Function definition does not bind loop variable `keyword`
```

The offending code:

```python
**{
    keyword: (lambda keyword: lambda self: exp.var(keyword))(keyword)
    for keyword in _STARROCKS_AGGREGATE_COLUMN_CONSTRAINTS
},
```

The immediately-invoked outer lambda does bind the value correctly at runtime, but it does so by shadowing the comprehension variable, which is exactly the pattern B023 exists to flag and which every reader has to stop and reason about.

## Remediation
- [ ] Rewrite the parser-map construction so the binding is explicit and lint-clean, e.g. a small named helper (`def _var_parser(keyword: str) -> Callable[..., exp.Var]: return lambda self: exp.var(keyword)`) or `functools.partial`.
- [ ] Behaviour must be identical: each keyword in `_STARROCKS_AGGREGATE_COLUMN_CONSTRAINTS` still maps to a parser returning `exp.var(<that keyword>)`.
- [ ] Do not add a `# noqa`; fix the construct.

## Verification
- `ruff check superset/sql/dialects/starrocks.py` reports no B023.
- `pytest tests/unit_tests/sql/dialects/ -q` passes (add a focused test for the aggregate-constraint parsing if none covers it).

## Scope
Single file. No other lint fixes, no reformatting of unrelated code.
