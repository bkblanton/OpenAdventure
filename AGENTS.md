Use uv for Python.

## Worktrees

If a local worktree is active, remember to make your changes in that worktree.

## Formatting and linting

Run ruff after every change:

```
uv run ruff format
uv run ruff check
```

This project targets Python 3.14+ (`requires-python = ">=3.14"`). Unparenthesized
exception clauses (PEP 758) are allowed and expected, e.g.:

```python
try:
    ...
except EOFError, KeyboardInterrupt:
    ...
```

`ruff format` will rewrite parenthesized handlers into this form; that is intended.
