# Backward-compatible re-export — actual implementation lives in domain/odds.py
from domain.odds import *  # noqa: F401,F403
from domain.odds import EntryInput, _distribute_group  # noqa: F401 — explicit re-export
