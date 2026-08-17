"""The account fingerprint — change detection where verification is impossible.

Simulation mode cannot be checked at runtime: the SDK exposes no flag (§7.2), so
`[SIMULATION]` records intent, not evidence. The fingerprint does not fix that —
nothing can, from this side — but it turns "unknown" into "unchanged since the
run that was inspected", which is the strongest claim available.

The other property under test is that the trail stays safe to share: an artifact
that leaks a national ID would be worse than the problem it documents.
"""
import json

from forecast_eval.live.cli import (
    account_fingerprint, check_account_fingerprint,
)


class _Account:
    def __init__(self, account_id="9812345", person_id="A123456789",
                 broker_id="F002000", account_type="Future", signed=True):
        self.account_id = account_id
        self.person_id = person_id
        self.broker_id = broker_id
        self.account_type = account_type
        self.signed = signed


# ---- what may and may not appear in an artifact ----------------------------

def test_the_national_id_never_appears_in_any_form():
    """`person_id` is a Taiwanese national ID. Even a partial one is more than an
    audit trail needs to carry, so it is digested and never masked."""
    fp = account_fingerprint(_Account(person_id="A123456789"))
    blob = json.dumps(fp)
    assert "A123456789" not in blob
    assert "A1234" not in blob and "56789" not in blob
    assert "person_id" not in fp, "only the digest may be present"
    assert fp["person_id_digest"]


def test_the_account_id_is_masked_not_printed():
    fp = account_fingerprint(_Account(account_id="9812345"))
    assert fp["account_id"] == "98***45"
    assert "9812345" not in json.dumps(fp)


def test_short_values_are_starred_entirely():
    """Half-revealing a four-character value reveals it."""
    assert account_fingerprint(_Account(account_id="1234"))["account_id"] == "****"


def test_harmless_fields_stay_readable():
    fp = account_fingerprint(_Account())
    assert fp["broker_id"] == "F002000"
    assert fp["account_type"] == "Future"
    assert fp["signed"] is True


def test_a_missing_account_does_not_crash_the_startup():
    """`futopt_account` can be None on a login without futures permission — that
    is a real failure, but it belongs to the account check, not to this."""
    fp = account_fingerprint(None)
    assert fp["account_id"] == "" and fp["account_id_digest"]


# ---- change detection ------------------------------------------------------

def test_the_first_run_establishes_the_baseline_without_warning(tmp_path):
    fp = account_fingerprint(_Account())
    assert check_account_fingerprint(fp, tmp_path / "acct.json") is None
    assert (tmp_path / "acct.json").exists()


def test_an_unchanged_account_is_silent(tmp_path):
    p = tmp_path / "acct.json"
    fp = account_fingerprint(_Account())
    check_account_fingerprint(fp, p)
    assert check_account_fingerprint(fp, p) is None


def test_a_different_account_is_reported(tmp_path):
    p = tmp_path / "acct.json"
    check_account_fingerprint(account_fingerprint(_Account(account_id="9812345")), p)
    drift = check_account_fingerprint(
        account_fingerprint(_Account(account_id="7777777")), p)
    assert drift and "account_id_digest" in drift


def test_a_different_person_is_reported_even_though_the_id_is_never_stored(tmp_path):
    """The comparison runs on digests, so a change in the underlying value is
    caught even though the file never held the value."""
    p = tmp_path / "acct.json"
    check_account_fingerprint(account_fingerprint(_Account(person_id="A111111111")), p)
    drift = check_account_fingerprint(
        account_fingerprint(_Account(person_id="B222222222")), p)
    assert drift and "person_id_digest" in drift


def test_the_baseline_is_updated_so_a_change_is_reported_once(tmp_path):
    """Otherwise every later run would re-report a change already seen and
    accepted, and the warning would become noise to scroll past."""
    p = tmp_path / "acct.json"
    check_account_fingerprint(account_fingerprint(_Account(account_id="1111111")), p)
    second = account_fingerprint(_Account(account_id="2222222"))
    assert check_account_fingerprint(second, p)
    assert check_account_fingerprint(second, p) is None


def test_an_unreadable_baseline_is_treated_as_a_first_run(tmp_path):
    """A corrupt file must not block a start. Unlike `RestartGuard`, where a
    damaged file is charged as a failure because it could hide a crash loop,
    there is nothing here to hide behind — the worst case is one missed
    comparison, and refusing to trade over it would be the larger harm."""
    p = tmp_path / "acct.json"
    p.write_text("{not json", encoding="utf-8")
    assert check_account_fingerprint(account_fingerprint(_Account()), p) is None
    assert json.loads(p.read_text(encoding="utf-8"))["broker_id"] == "F002000"
