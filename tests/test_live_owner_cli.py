"""Tests for live/owner_cli.py — the owner entry point (§18.2).

The venue-touching path needs credentials, so what is tested here is everything
that decides whether the owner should start at all, plus the serve loop driven
with a fake owner.
"""
import csv

import pytest

from forecast_eval.live.ledger import LEDGER_COLUMNS
from forecast_eval.live.owner_cli import build_parser, main, seed_nets, _serve


def _ledger(directory, slug, rows, soak="epoch-1"):
    path = directory / f"fills_{slug}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(LEDGER_COLUMNS)
        for side, qty in rows:
            writer.writerow(["2026-09-07 10:00:00", side, 46000.0, qty, 1.2, soak])
    return path


# ---- seeding ---------------------------------------------------------------

def test_seed_nets_reads_each_deployments_own_ledger(tmp_path):
    _ledger(tmp_path, "day", [("BUY", 1), ("BUY", 1)])
    _ledger(tmp_path, "night", [("SELL", 1)])
    assert seed_nets(tmp_path, ["day", "night"]) == {"day": 2, "night": -1}


def test_a_slug_with_no_ledger_seeds_flat(tmp_path):
    """A deployment that has never traded is normal, not unknown."""
    _ledger(tmp_path, "day", [("BUY", 1)])
    assert seed_nets(tmp_path, ["day", "night", "day_night"]) == {
        "day": 1, "night": 0, "day_night": 0}


def test_seed_nets_ignores_ledgers_for_slugs_it_does_not_serve(tmp_path):
    _ledger(tmp_path, "day", [("BUY", 1)])
    _ledger(tmp_path, "other", [("BUY", 9)])
    assert seed_nets(tmp_path, ["day"]) == {"day": 1}


# ---- refusing to start -----------------------------------------------------

def test_the_acknowledgement_is_required(tmp_path, capsys):
    assert main(["--out", str(tmp_path)]) == 5
    assert "places orders" in capsys.readouterr().err


def test_a_missing_out_directory_is_refused(tmp_path, capsys):
    code = main(["--out", str(tmp_path / "absent"),
                 "--i-understand-this-places-orders"])
    assert code == 2
    assert "not a directory" in capsys.readouterr().err


def test_the_parser_defaults_to_the_three_deployments():
    args = build_parser().parse_args(["--out", "."])
    assert args.slugs == ["day", "night", "day_night"]


# ---- the serve loop --------------------------------------------------------

class _FakeOwner:
    """Records what the loop asked it to do."""

    deployment_timeout = 180.0

    def __init__(self, reconcile=(True,), stale=()):
        self.polls = 0
        self.reconciles = 0
        self._reconcile = list(reconcile)
        self._stale = list(stale)
        self.anomalies = ["mirror 1 != venue 2"]

    def poll(self):
        self.polls += 1

    def reconcile(self):
        self.reconciles += 1
        return self._reconcile[min(self.reconciles - 1,
                                   len(self._reconcile) - 1)]

    def stale_deployments(self):
        return self._stale


class _Args:
    def __init__(self, **kw):
        self.poll_seconds = 0.0
        self.reconcile_seconds = 0.0
        self.once = True
        self.__dict__.update(kw)


def test_serve_polls_before_it_reconciles():
    owner = _FakeOwner()
    assert _serve(owner, _Args()) == 0
    assert owner.polls == 1
    assert owner.reconciles == 1


def test_a_failed_reconciliation_stops_the_owner(capsys):
    """The account net sizes every subsequent leg; continuing multiplies a desync."""
    owner = _FakeOwner(reconcile=(False,))
    assert _serve(owner, _Args()) == 6
    err = capsys.readouterr().err
    assert "RECONCILIATION FAILED" in err
    assert "mirror 1 != venue 2" in err


def test_a_venue_that_will_not_answer_does_not_stop_the_owner():
    """None is 'cannot compare', which is not a disagreement."""
    owner = _FakeOwner(reconcile=(None,))
    assert _serve(owner, _Args()) == 0


def test_a_stale_deployment_is_reported_and_not_acted_on(capsys):
    """§7.4 — closing an absent deployment's lot is a risk call, not a mechanical one."""
    owner = _FakeOwner(stale=["night"])
    assert _serve(owner, _Args()) == 0
    out = capsys.readouterr().out
    assert "no intents from ['night']" in out
    assert "still open and still counted" in out


def test_reconcile_is_skipped_until_its_cadence_elapses():
    """It costs a venue call; the poll does not."""
    owner = _FakeOwner()
    _serve(owner, _Args(reconcile_seconds=9999.0))
    assert owner.polls == 1
    assert owner.reconciles == 1, "the first pass always reconciles"

    owner2 = _FakeOwner()
    args = _Args(reconcile_seconds=9999.0, once=False)

    # Stop after three passes, so the cadence has something to skip.
    calls = {"n": 0}
    original = owner2.poll

    def _counting_poll():
        original()
        calls["n"] += 1
        if calls["n"] >= 3:
            raise KeyboardInterrupt

    owner2.poll = _counting_poll
    with pytest.raises(KeyboardInterrupt):
        _serve(owner2, args)
    assert owner2.polls == 3
    assert owner2.reconciles == 1, "only the first pass was due"


# ---- traffic headroom (§18.13) ---------------------------------------------

class _FakeUsage:
    def __init__(self, used, limit, remaining):
        self.bytes, self.limit_bytes, self.remaining_bytes = used, limit, remaining


class _FakeAPI:
    def __init__(self, usage=None, raises=False):
        self._usage, self._raises = usage, raises

    def usage(self):
        if self._raises:
            raise RuntimeError("no answer")
        return self._usage


def test_low_headroom_warns_and_keeps_serving(capsys):
    """Stopping would halt every deployment; a thin allowance is not that."""
    api = _FakeAPI(_FakeUsage(500_000_000, 524_288_000, 24_288_000))
    owner = _FakeOwner()
    assert _serve(owner, _Args(min_remaining_mb=50.0), api) == 0
    out = capsys.readouterr().out
    assert "traffic headroom" in out and "23 MB left" in out
    assert "EMPTY history" in out, "the consequence, not just the number"


def test_ample_headroom_says_nothing(capsys):
    api = _FakeAPI(_FakeUsage(1_000_000, 524_288_000, 523_288_000))
    assert _serve(_FakeOwner(), _Args(min_remaining_mb=50.0), api) == 0
    assert "traffic headroom" not in capsys.readouterr().out


def test_a_usage_call_that_raises_does_not_take_the_owner_down():
    """A statistic must never stop the process that holds three deployments' orders."""
    assert _serve(_FakeOwner(), _Args(min_remaining_mb=50.0),
                  _FakeAPI(raises=True)) == 0


def test_no_api_means_no_traffic_check():
    assert _serve(_FakeOwner(), _Args(min_remaining_mb=50.0), None) == 0


# ---- the futures account, and §13's classification trap (§18.14) ------------

def test_futopt_account_is_preferred_when_the_sdk_offers_one():
    from forecast_eval.live.owner_cli import _futures_account

    class _API:
        futopt_account = "THE-ACCOUNT"

    assert _futures_account(_API(), []) == "THE-ACCOUNT"


def test_the_account_list_is_the_second_opinion():
    """§13: the first probe reported '0 futures accounts' for a credential
    trading TXF nightly, because `str(AccountType.Future)` is 'F'. Matching the
    enum MEMBER is what makes the fallback mean anything.

    This test also caught the enum being reached at the wrong path
    (`sj.constant.AccountType`, which does not exist in 1.7.2) — the same class
    of mistake one level up.
    """
    import shioaji as sj
    from forecast_eval.live.owner_cli import _futures_account

    class _API:
        futopt_account = None

    class _Acct:
        def __init__(self, kind):
            self.account_type = kind

    stock = _Acct(sj.AccountType.Stock)
    future = _Acct(sj.AccountType.Future)
    assert _futures_account(_API(), [stock, future]) is future


def test_no_futures_account_returns_none_not_a_guess():
    from forecast_eval.live.owner_cli import _futures_account

    class _API:
        futopt_account = None

    assert _futures_account(_API(), []) is None
    assert _futures_account(_API(), None) is None
