"""tools/roll_at_measure.py must be import-safe and degrade when data is absent.

The suite's static name check imports every module; this one ran its two
surveys at module level until 2026-10-11, so importing it on a machine without
the raw TAIFEX files crashed that check (empty buckets -> None -> TypeError).
"""
import importlib

import forecast_eval.tools.roll_at_measure as ram


def test_importing_the_module_runs_no_survey(capsys):
    # Tech: reload re-executes the module body deterministically.
    # Why: the first import happened elsewhere (the static name check); a body
    #      that prints or crashes here is the bug class this pins.
    importlib.reload(ram)
    assert capsys.readouterr().out == ""


def test_measure_with_no_data_reports_and_returns_empty(monkeypatch, capsys):
    # Tech: read_day is the only data source; an empty read leaves `agg` empty
    #       and best_thin_b None -- exactly the import-crash shape.
    # Why:  a survey with no readable files must print a legible "no rows"
    #       line and return an empty table, not raise.
    monkeypatch.setattr(ram, "read_day", lambda d, extra_days=(0,): [])
    agg = ram.measure((15, 0), 60, "no-data probe")
    assert agg == {}
    assert "no rows matched" in capsys.readouterr().out
