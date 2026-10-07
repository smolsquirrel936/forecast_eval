"""Tests for live/owner_cli.py — the owner entry point (§18.2).

The venue-touching path needs credentials, so what is tested here is everything
that decides whether the owner should start at all, plus the serve loop driven
with a fake owner.
"""
import csv

import pytest

from forecast_eval.live.ledger import LEDGER_COLUMNS
from forecast_eval.live.owner_cli import build_parser, main, seed_positions, _serve


def _ledger(directory, slug, rows, soak="epoch-1"):
    path = directory / f"fills_{slug}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(LEDGER_COLUMNS)
        for side, qty in rows:
            writer.writerow(["2026-09-07 10:00:00", side, 46000.0, qty, 1.2, soak])
    return path


# ---- seeding ---------------------------------------------------------------

def _signed(seeds):
    return {slug: getattr(held, "signed_qty", held) for slug, held in seeds.items()}


def test_seed_positions_reads_each_deployments_own_ledger(tmp_path):
    _ledger(tmp_path, "day", [("BUY", 1), ("BUY", 1)])
    _ledger(tmp_path, "night", [("SELL", 1)])
    assert _signed(seed_positions(tmp_path, ["day", "night"])) == {
        "day": 2, "night": -1}


def test_a_slug_with_no_ledger_seeds_flat(tmp_path):
    """A deployment that has never traded is normal, not unknown."""
    _ledger(tmp_path, "day", [("BUY", 1)])
    assert _signed(seed_positions(tmp_path, ["day", "night", "day_night"])) == {
        "day": 1, "night": 0, "day_night": 0}


def test_seed_positions_ignores_ledgers_for_slugs_it_does_not_serve(tmp_path):
    _ledger(tmp_path, "day", [("BUY", 1)])
    _ledger(tmp_path, "other", [("BUY", 9)])
    assert _signed(seed_positions(tmp_path, ["day"])) == {"day": 1}


def test_seed_positions_counts_every_soak_epoch(tmp_path):
    """The venue holds what every epoch left open, so the seed must too."""
    path = _ledger(tmp_path, "day", [("BUY", 1)], soak="epoch-1")
    with path.open("a", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerow(["2026-09-08 10:00:00", "BUY", 46100.0, 1, 1.2,
                                 "epoch-2"])
    assert _signed(seed_positions(tmp_path, ["day"])) == {"day": 2}


def test_a_held_seed_carries_its_entry_price(tmp_path):
    """2026-10-07: the seed was a bare count, so the owner published fullday's
    +1 at avg_price 0.0 and `--resume` adopted it "@ 0.00" — closing it would
    have booked ~+50 000 points."""
    _ledger(tmp_path, "day_night", [("BUY", 1)])
    held = seed_positions(tmp_path, ["day_night"])["day_night"]
    assert held.signed_qty == 1
    assert held.avg_price == 46000.0


def test_the_seeded_entry_reaches_the_deployment(tmp_path):
    """End to end: ledger -> owner seed -> position event -> what
    `adopt_position` reads."""
    from forecast_eval.live.broker.client import ClientBroker
    from forecast_eval.live.owner_process import OwnerProcess

    _ledger(tmp_path, "day_night", [("BUY", 1)])
    owner = OwnerProcess(None, None, directory=tmp_path, slugs=["day_night"],
                         account="ACC")
    owner.seed_from(seed_positions(tmp_path, ["day_night"]))
    owner.publish_positions()
    detail = ClientBroker(tmp_path, "day_night").broker_position_detail()
    assert (detail.signed_qty, detail.avg_price) == (1, 46000.0)


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

    def __init__(self, reconcile=(True,), stale=(), note=None):
        self.polls = 0
        self.reconciles = 0
        self._reconcile = list(reconcile)
        self._stale = list(stale)
        self.anomalies = ["mirror 1 != venue 2"]
        self.frozen = None
        self.reconcile_note = note

    def freeze(self, reason):
        if self.frozen is None:
            self.frozen = reason

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


def test_a_failed_reconciliation_freezes_the_owner(capsys):
    """The account net sizes every subsequent leg; placing on it multiplies a desync."""
    owner = _FakeOwner(reconcile=(False,))
    assert _serve(owner, _Args()) == 6, "a frozen owner still exits 6 when stopped"
    assert owner.frozen == "mirror 1 != venue 2"
    err = capsys.readouterr().err
    assert "RECONCILIATION FAILED — OWNER FROZEN" in err
    assert "mirror 1 != venue 2" in err


def _serve_passes(owner, passes, **kw):
    """Run `_serve` for exactly `passes` passes, reconciling on every one."""
    original = owner.poll

    def _counting_poll():
        original()
        if owner.polls > passes:
            raise KeyboardInterrupt

    owner.poll = _counting_poll
    with pytest.raises(KeyboardInterrupt):
        _serve(owner, _Args(once=False, **kw))


def test_a_frozen_owner_keeps_serving():
    """2026-10-06: the owner RETURNED on a failed reconcile, and the logout cut
    the deal stream while the fill it was disputing was still on its way.
    Frozen, it keeps polling — booking deals and publishing positions."""
    owner = _FakeOwner(reconcile=(False, False, False))
    _serve_passes(owner, 3)
    assert owner.polls == 4, "the loop went on past the failure"
    assert owner.reconciles == 3


def test_the_freeze_banner_prints_once_and_keeps_the_first_reason(capsys):
    owner = _FakeOwner(reconcile=(False, False))
    _serve_passes(owner, 2)
    assert capsys.readouterr().err.count("OWNER FROZEN") == 1
    assert owner.frozen == "mirror 1 != venue 2"


def test_agreement_after_a_freeze_is_reported_and_does_not_unfreeze(capsys):
    """Like SafetyRails: a dispute is settled by a person and a restart."""
    owner = _FakeOwner(reconcile=(False, True, True))
    _serve_passes(owner, 3)
    assert owner.frozen is not None
    assert capsys.readouterr().err.count("agree again") == 1


def test_a_pending_reconcile_is_printed(capsys):
    owner = _FakeOwner(note="account mirror 0 != venue 1, explained by orders "
                            "in flight — waiting 3s of 120s")
    assert _serve(owner, _Args()) == 0
    assert ("reconcile pending: account mirror 0 != venue 1"
            in capsys.readouterr().out)


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


# ---- the owner's re-login (§2.24) ------------------------------------------
#
# Until 2026-09-29 the owner logged in once and never again, so past 24 h its
# token died while the deployments -- which do re-login -- kept trading through
# it. These pin the fix: the schedule, the action, and that the loop drives it.

def test_the_owner_refreshes_before_every_deployment_does():
    """Owner window [relogin_at, verify_at] ends before the deployments' starts,
    so its connection spike (~30 s, relogin_probe Q9) has cleared first."""
    from forecast_eval.live.owner_cli import owner_relogin_anchors
    from forecast_eval.live.relogin import session_open_anchors

    owner = owner_relogin_anchors()
    deployments = session_open_anchors()
    assert len(owner) == len(deployments) == 2
    for (o_relogin, o_verify, o_open), (d_relogin, _, d_open) in zip(owner,
                                                                      deployments):
        assert o_open == d_open
        assert o_relogin < o_verify < d_relogin


class _ReloginAPI:
    def __init__(self, subscribed=True, logout_raises=False):
        self.logins, self.subscribed_accounts, self.calls = [], [], []
        self._subscribed = subscribed
        self._logout_raises = logout_raises
        self.futopt_account = "OLD-ACCOUNT"

    def logout(self):
        self.calls.append("logout")
        if self._logout_raises:
            raise RuntimeError("AuthError: Not authenticated")

    def login(self, **kw):
        self.calls.append("login")
        self.logins.append(kw)
        self.futopt_account = "NEW-ACCOUNT"

    def subscribe_trade(self, account):
        self.calls.append("subscribe_trade")
        self.subscribed_accounts.append(account)
        return self._subscribed


class _AttachOwner:
    def __init__(self):
        self.attached = 0

    def attach(self):
        self.attached += 1


class _Cred:
    api_key, secret_key = "k", "s"


def test_the_relogin_logs_out_then_in_and_subscribes_the_new_session():
    """MEASURED 2026-10-08 (relogin_probe --mode reassert): after a
    `force_refresh` login nothing restored the order callback — the 10-07 owner
    re-logged in that way and never heard the 22:16 night fill. Logout + plain
    login + subscribe_trade(new account) was heard, 2 of 2."""
    from forecast_eval.live.owner_cli import make_relogin

    api, owner = _ReloginAPI(), _AttachOwner()
    make_relogin(api, _Cred(), "ACCOUNT", owner)()
    assert api.calls == ["logout", "login", "subscribe_trade"]
    assert api.logins == [dict(api_key="k", secret_key="s")], (
        "force_refresh is the form that leaves the owner deaf")
    assert api.subscribed_accounts == ["NEW-ACCOUNT"]
    assert owner.attached == 1


def test_a_logout_that_raises_does_not_stop_the_login():
    """A token that is already dead cannot log out, and still needs replacing."""
    from forecast_eval.live.owner_cli import make_relogin

    api, owner = _ReloginAPI(logout_raises=True), _AttachOwner()
    make_relogin(api, _Cred(), "ACCOUNT", owner)()
    assert api.calls == ["logout", "login", "subscribe_trade"]
    assert owner.attached == 1


def test_a_refused_deal_stream_fails_the_attempt():
    """Settling on a session that cannot hear fills is the failure to avoid."""
    from forecast_eval.live.owner_cli import make_relogin

    owner = _AttachOwner()
    with pytest.raises(RuntimeError, match="subscribe_trade"):
        make_relogin(_ReloginAPI(subscribed=False), _Cred(), "ACCOUNT", owner)()
    assert owner.attached == 0


class _FakeScheduler:
    def __init__(self, outcome=None):
        self.checks, self._outcome = 0, outcome

    def check(self, now):
        self.checks += 1
        return self._outcome

    def token_age(self, now):
        return "0:01:00"


def test_serve_drives_the_relogin_scheduler_and_reports_its_outcome(capsys):
    from forecast_eval.live.relogin import Outcome

    sched = _FakeScheduler(Outcome.RELOGGED)
    assert _serve(_FakeOwner(), _Args(), None, sched) == 0
    assert sched.checks == 1
    assert "relogin: " + Outcome.RELOGGED.value in capsys.readouterr().out


def test_a_quiet_scheduler_prints_nothing(capsys):
    sched = _FakeScheduler(None)
    assert _serve(_FakeOwner(), _Args(), None, sched) == 0
    assert sched.checks == 1
    assert "relogin" not in capsys.readouterr().out
