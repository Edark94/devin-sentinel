Ran the latest ruff (0.16.x) against the repo with our own config and it flags this in the StarRocks dialect:

```
superset/sql/dialects/starrocks.py:132:60: B023 Function definition does not bind loop variable `keyword`
```

```python
keyword: (lambda keyword: lambda self: exp.var(keyword))(keyword)
for keyword in _STARROCKS_AGGREGATE_COLUMN_CONSTRAINTS
```

It works at runtime because the outer lambda is called immediately, but it does so by shadowing the comprehension variable, which is exactly what B023 is warning about and it makes everyone stop and think. Would be nicer with a small named helper or `functools.partial` instead of the double lambda, no `noqa`.

Behaviour should stay the same (each keyword still maps to a parser returning `exp.var(keyword)`). ruff should be clean on the file afterwards and the dialect tests should pass, add a small test if nothing covers this.
