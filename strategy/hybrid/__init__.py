"""The HYBRID strategy set — the DAY half in DAY, the NIGHT half in NIGHT.

A fourth sibling package beside ``day``, ``night`` and ``fullday``
(``autoresearch_trading_plan_hybrid.md``). It trades both sessions and may hold a
lot across a session change, like ``fullday``, but each session is traded by its
own half: ``day_half/`` and ``night_half/`` each own a Forecaster, SignalEmitter
and Trader, and ``router.py`` sends every engine call to the half whose session
it is. Nothing here imports a sibling package (``tests/test_strategy_isolation``).
"""
