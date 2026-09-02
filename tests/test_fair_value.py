from bot.signal.fair_value import effective_cost, fair_yes, norm_cdf


def test_cdf_symmetry():
    assert abs(norm_cdf(0.0) - 0.5) < 1e-12
    assert abs(norm_cdf(1.0) + norm_cdf(-1.0) - 1.0) < 1e-12


def test_at_the_money_is_half():
    assert abs(fair_yes(100_000, 100_000, 1e-4, 0.0, 120) - 0.5) < 1e-9


def test_above_strike_favours_yes():
    assert fair_yes(100_100, 100_000, 1e-4, 0.0, 120) > 0.6


def test_certainty_grows_as_time_decays():
    early = fair_yes(100_100, 100_000, 1e-4, 0.0, 240)
    late = fair_yes(100_100, 100_000, 1e-4, 0.0, 10)
    assert late > early


def test_positive_drift_lifts_fair():
    flat = fair_yes(100_000, 100_000, 1e-4, 0.0, 120)
    up = fair_yes(100_000, 100_000, 1e-4, 5e-6, 120)
    assert up > flat


def test_expiry_is_binary():
    assert fair_yes(100_001, 100_000, 1e-4, 0.0, 0) == 1.0
    assert fair_yes(99_999, 100_000, 1e-4, 0.0, 0) == 0.0


def test_effective_cost_adds_fee_on_cheap_side():
    assert effective_cost(0.30, 0) == 0.30
    assert effective_cost(0.30, 100) == 0.30 + 0.01 * 0.30
    assert effective_cost(0.70, 100) == 0.70 + 0.01 * 0.30  # fee scales with min(p, 1-p)


def test_tie_resolves_down():
    # Up wins only strictly above the open: a tie is Down/NO
    assert fair_yes(100_000, 100_000, 1e-4, 0.0, 0) == 0.0
