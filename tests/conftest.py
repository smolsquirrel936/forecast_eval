"""Pytest config — make `forecast_eval` importable regardless of where
pytest is invoked from (myPaper/, forecast_eval/, or the tests dir itself).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
