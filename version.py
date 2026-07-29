"""Single source of truth for auto-claude's version.

Recorded verbatim in `auto_claude.harness.version` (db/harness.py, Task 6) so
an incident review can tell which build produced a given row. Bump this by
hand on every release that changes on-disk or on-wire behavior worth
distinguishing.
"""

__version__ = "0.2.0"
