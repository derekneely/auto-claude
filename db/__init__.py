"""Postgres-backed shared state for auto-claude (docs/plans/
12-shared-state-in-postgres.md). Runtime SQL is raw psycopg3 throughout —
SQLAlchemy exists only because Alembic requires it (migrations/env.py).
"""
