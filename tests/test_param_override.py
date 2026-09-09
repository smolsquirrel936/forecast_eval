"""`--param KEY=VALUE` coercion (LIVE_STRATEGY_ROUTING_PLAN D7).

The module's only job is to turn a command-line string into the type a package's
`PARAMS` declares. Everything that can go wrong is a silent mistype, so each
branch has a case here — especially the boolean one, where Python's own
truthiness would give the wrong answer.
"""
import pytest

from forecast_eval.param_override import parse_overrides

# One knob per branch of the coercion table.
PARAMS = {
    "checkpoint": "Datadog/Toto-2.0-1B",   # str
    "context_length": 3008,                # int
    "buy_threshold": 0.00075,              # float
    "min_prob": 0.5,                       # float, but None is meaningful
    "forced_close": False,                 # bool
    "max_aggression_ticks": None,          # no declared type
    "ret_k": 3,                            # a knob no CLI has a flag for
}


def test_each_declared_type_is_coerced_from_its_string():
    got = parse_overrides(
        ["checkpoint=Datadog/Toto-2.0-313m", "context_length=1024",
         "buy_threshold=0.0009", "ret_k=4"],
        PARAMS,
    )
    assert got == {
        "checkpoint": "Datadog/Toto-2.0-313m",
        "context_length": 1024,
        "buy_threshold": 0.0009,
        "ret_k": 4,
    }
    assert isinstance(got["context_length"], int)
    assert isinstance(got["buy_threshold"], float)


@pytest.mark.parametrize("raw,expected",
                         [("false", False), ("False", False), ("FALSE", False),
                          ("0", False), ("no", False),
                          ("true", True), ("1", True), ("yes", True)])
def test_booleans_come_from_a_table_not_from_truthiness(raw, expected):
    """`bool("false")` is True. Every non-empty string is.

    This is the one branch where the obvious implementation is silently wrong,
    and `forced_close` is a knob that decides whether positions are closed at the
    session boundary — a strategy difference, not a formatting one.
    """
    assert parse_overrides([f"forced_close={raw}"], PARAMS) == {"forced_close": expected}


def test_a_non_boolean_word_for_a_boolean_knob_is_refused():
    with pytest.raises(ValueError, match="expected a boolean"):
        parse_overrides(["forced_close=maybe"], PARAMS)


def test_none_is_accepted_for_any_knob_not_only_optional_ones():
    """`PARAMS` cannot express optionality, and disabling `min_prob` on a package
    that declares 0.5 is a real thing to want."""
    got = parse_overrides(["min_prob=none", "max_aggression_ticks=NONE"], PARAMS)
    assert got == {"min_prob": None, "max_aggression_ticks": None}


def test_a_knob_with_no_declared_default_takes_a_number():
    assert parse_overrides(["max_aggression_ticks=2"], PARAMS)["max_aggression_ticks"] == 2


def test_a_knob_with_no_declared_default_refuses_a_word():
    with pytest.raises(ValueError, match="type is unknown"):
        parse_overrides(["max_aggression_ticks=lots"], PARAMS)


def test_a_float_string_for_an_int_knob_is_refused_not_truncated():
    """Truncating 3008.5 to 3008 would run a context length nobody asked for."""
    with pytest.raises(ValueError, match="expected an integer"):
        parse_overrides(["context_length=3008.5"], PARAMS)


def test_an_unparseable_number_names_the_knob():
    with pytest.raises(ValueError, match="buy_threshold"):
        parse_overrides(["buy_threshold=wide"], PARAMS)


def test_an_unknown_knob_is_forwarded_uncoerced_for_make_strategy_to_reject():
    """Delegation, not duplication: `make_strategy` already raises an error that
    names the package's real knob set, which is the message a typo deserves."""
    assert parse_overrides(["ret_kk=4"], PARAMS) == {"ret_kk": "4"}


def test_a_value_containing_an_equals_sign_survives():
    assert parse_overrides(["checkpoint=org/model=v2"], PARAMS)["checkpoint"] == "org/model=v2"


def test_a_pair_with_no_equals_is_refused():
    with pytest.raises(ValueError, match="expects KEY=VALUE"):
        parse_overrides(["ret_k"], PARAMS)


def test_a_repeated_knob_is_refused_rather_than_last_wins():
    """A repeatable flag makes this easy to reach, and taking the last silently
    is the difference between a typo that stops and a typo that produces a
    number."""
    with pytest.raises(ValueError, match="more than once"):
        parse_overrides(["ret_k=4", "ret_k=5"], PARAMS)


def test_whitespace_around_the_pair_is_tolerated():
    assert parse_overrides([" ret_k = 4 "], PARAMS) == {"ret_k": 4}
