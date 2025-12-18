import pytest
import math
from telegram_bot.utils import calculate_torrent_health


def test_health_zero_seeds():
    assert calculate_torrent_health(0, 100) == 0.0
    assert calculate_torrent_health(-1, 0) == 0.0


def test_health_high_seeds_low_leeches():
    # 100 seeds should be close to 10
    health = calculate_torrent_health(100, 0)
    assert 9.0 < health <= 10.0


def test_health_low_seeds_low_leeches():
    # 10 seeds: 10 * (1 - exp(-10/25)) = 3.29
    health = calculate_torrent_health(10, 0)
    assert pytest.approx(health, rel=0.01) == 3.296


def test_health_contention_penalty():
    # 10 seeds, 100 leeches -> availability 3.29, penalty 10/100 = 0.1 -> 0.329
    health = calculate_torrent_health(10, 100)
    assert pytest.approx(health, rel=0.01) == 0.3296


def test_health_no_penalty_when_seeds_equal_leeches():
    health = calculate_torrent_health(50, 50)
    expected = 10 * (1 - math.exp(-50 / 25))
    assert pytest.approx(health) == expected


def test_health_no_penalty_when_seeds_greater_than_leeches():
    health = calculate_torrent_health(50, 10)
    expected = 10 * (1 - math.exp(-50 / 25))
    assert pytest.approx(health) == expected


@pytest.mark.parametrize(
    "seeds,leeches,expected_min,expected_max",
    [
        (1, 0, 0.3, 0.5),
        (25, 0, 6.0, 6.5),
        (50, 0, 8.5, 8.8),
        (20, 200, 0.5, 0.6),  # Example A from discussion
        (50, 100, 4.0, 4.5),  # Example B from discussion
        (40, 5, 7.5, 8.5),  # Example C from discussion
    ],
)
def test_health_scenarios(seeds, leeches, expected_min, expected_max):
    health = calculate_torrent_health(seeds, leeches)
    assert expected_min <= health <= expected_max
