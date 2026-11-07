Read SPEC.md before every task and follow it.
- Python project managed with uv: use pyproject.toml, `uv add`, `uv run`. No requirements.txt, no pip.
- Write small functions that do one thing, with clear names and short docstrings. Add type hints.
- Keep files focused: routes in main.py, logic in separate modules as in SPEC.md.
- Never hardcode secrets. Read FIREWORKS_API_KEY from the environment.
- Handle errors explicitly and return clear messages. No silent excepts.
- Do not add features that are not in SPEC.md.