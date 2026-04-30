# Coding guidelines

- Do not add obvious comments or docstrings. Only comment where the logic is non-obvious.
- Never use try/except around imports. If a dependency is required, import it at the top level and let it fail loudly if missing.
- Never add "Ultraworked with" or "Co-authored-by" lines to commit messages.
