# Memory fixtures

`demo_001/` is the clean, version-controlled baseline for tests and local
demo setup. It includes profile, facts, preferences, and one persistent
session with two turns. The live application writes user state under
`data/memory/`, which is intentionally ignored by Git.

Copy the fixture into a temporary memory root for tests. Do not point tests
at the live `data/memory/` directory.
