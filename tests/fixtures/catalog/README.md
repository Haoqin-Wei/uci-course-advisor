# Catalog fixtures

This directory is a small synthetic Spring 2025 catalog for offline tests.
It mirrors the production UCI relational CSV layout but is intentionally
limited to three courses and four sections:

- one lecture/discussion pair,
- one full section with an overlapping meeting time,
- one TBA section.

Tests should load these files through `UCIRelationalLoader`; they must not
read or modify the production files under `data/uci/`.
