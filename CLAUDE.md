# KIO1 orchestrator

Start with `docs/handoff.md` (state, how to run and test, pitfalls, known
gaps), then the current plan in `docs/plans/`.

Rules from the user:

- Talk to the user in Turkish. Code, comments, and docs in English.
- Never `git push`. Commit each finished step yourself, with a plain
  message and **no** `Co-Authored-By` (or any other Claude) trailer.
- Keep UI text minimal; ask before a large visual change; verify UI with
  `tools/console_screenshots.py` and show the screenshots.
- Run the tests (`python -m pytest -q -p no:cacheprovider`) and try changes
  live against the real model; report weak results honestly.
- Match the surrounding code: docstrings that explain why, the existing
  test style (one behaviour per test, named as a sentence).
