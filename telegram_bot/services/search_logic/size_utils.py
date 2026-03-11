# telegram_bot/services/search_logic/size_utils.py

import re


def _parse_size_to_gb(size_str: str) -> float:
    """Converts size strings like '1.5 GB' or '500 MB' to a float in GB."""
    size_str = size_str.lower().replace(",", "")
    try:
        size_match = re.search(r"([\d.]+)", size_str)
        if not size_match:
            return 0.0

        size_val = float(size_match.group(1))
        if "gb" in size_str:
            return size_val
        if "mb" in size_str:
            return size_val / 1024
        if "kb" in size_str:
            return size_val / (1024 * 1024)

    except (ValueError, TypeError):
        return 0.0
    return 0.0
