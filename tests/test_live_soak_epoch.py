"""Soak epoch — the operator's --fresh-start/--resume choice, persisted.

The scenario being defended: `adopt_position` runs on every launch no matter
what, so the position was never the ambiguous part. What was ambiguous is
whether *this* launch continues the soak that was running before, or starts a
new one — and until this existed, nothing recorded which the operator meant.
"""
import json
from datetime import datetime, timedelta

from forecast_eval.live import soak_epoch

T0 = datetime(2026, 8, 31, 20, 39, 23)


def test_resume_with_nothing_on_disk_bootstraps_the_first_epoch(tmp_path):
    path = tmp_path / "soak_epoch.json"
    epoch = soak_epoch.resolve(path, fresh_start=False, now=T0)
    assert epoch.soak_id and epoch.started_at == T0.isoformat()
    assert path.exists()


def test_fresh_start_with_nothing_on_disk_also_just_mints_one(tmp_path):
    path = tmp_path / "soak_epoch.json"
    epoch = soak_epoch.resolve(path, fresh_start=True, now=T0)
    assert epoch.soak_id and epoch.started_at == T0.isoformat()
    assert json.loads(path.read_text())["retired"] == []


def test_resume_carries_the_same_epoch_forward(tmp_path):
    path = tmp_path / "soak_epoch.json"
    first = soak_epoch.resolve(path, fresh_start=False, now=T0)
    later = soak_epoch.resolve(path, fresh_start=False,
                               now=T0 + timedelta(days=6))
    assert later.soak_id == first.soak_id
    assert later.started_at == first.started_at


def test_fresh_start_mints_a_new_epoch_and_retires_the_old_one(tmp_path):
    path = tmp_path / "soak_epoch.json"
    first = soak_epoch.resolve(path, fresh_start=False, now=T0)
    t1 = T0 + timedelta(days=6)
    second = soak_epoch.resolve(path, fresh_start=True, now=t1)

    assert second.soak_id != first.soak_id
    assert second.started_at == t1.isoformat()

    on_disk = json.loads(path.read_text())
    assert on_disk["soak_id"] == second.soak_id
    retired = on_disk["retired"]
    assert len(retired) == 1
    assert retired[0]["soak_id"] == first.soak_id
    assert retired[0]["started_at"] == first.started_at
    assert retired[0]["retired_at"] == t1.isoformat()


def test_a_second_fresh_start_accumulates_retired_history_not_overwrites_it(tmp_path):
    path = tmp_path / "soak_epoch.json"
    e1 = soak_epoch.resolve(path, fresh_start=False, now=T0)
    e2 = soak_epoch.resolve(path, fresh_start=True, now=T0 + timedelta(days=1))
    soak_epoch.resolve(path, fresh_start=True, now=T0 + timedelta(days=2))

    retired_ids = [r["soak_id"] for r in json.loads(path.read_text())["retired"]]
    assert retired_ids == [e1.soak_id, e2.soak_id]


def test_fresh_start_within_the_same_second_still_mints_a_distinct_id(tmp_path):
    """REGRESSION, found manually 2026-08-31: second-resolution ids collided
    when minted less than a second apart, so a fresh-start could silently
    retire an epoch into one indistinguishable from itself."""
    path = tmp_path / "soak_epoch.json"
    t0 = datetime(2026, 8, 31, 20, 59, 44, 100000)
    t1 = datetime(2026, 8, 31, 20, 59, 44, 900000)   # same second, later microsecond
    first = soak_epoch.resolve(path, fresh_start=False, now=t0)
    second = soak_epoch.resolve(path, fresh_start=True, now=t1)
    assert second.soak_id != first.soak_id


def test_a_corrupt_file_is_treated_as_no_epoch_not_a_crash(tmp_path):
    path = tmp_path / "soak_epoch.json"
    path.write_text("not json{{{", encoding="utf-8")
    assert soak_epoch.load(path) is None
    epoch = soak_epoch.resolve(path, fresh_start=False, now=T0)
    assert epoch.soak_id
