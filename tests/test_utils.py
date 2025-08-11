from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parent.parent))

import pytest
from telegram_bot.utils import format_bytes, extract_first_int

# Use pytest's "parametrize" to test many cases with one function
@pytest.mark.parametrize("size_bytes, expected_str", [
    (0, "0B"),
    (1023, "1023.0 B"),
    (1024, "1.0 KB"),
    (1536, "1.5 KB"),
    (1048576, "1.0 MB"),
    (1610612736, "1.5 GB"),
])
def test_format_bytes(size_bytes, expected_str):
    """Verify that format_bytes converts byte sizes to correct human-readable strings."""
    assert format_bytes(size_bytes) == expected_str

@pytest.mark.parametrize("text, expected_int", [
    ("S01E05", 1),
    ("Season 12", 12),
    ("No numbers here", None),
    ("", None),
    ("Episode 5 is the best", 5),
])
def test_extract_first_int(text, expected_int):
    """Verify that extract_first_int correctly pulls the first integer."""
    assert extract_first_int(text) == expected_int
