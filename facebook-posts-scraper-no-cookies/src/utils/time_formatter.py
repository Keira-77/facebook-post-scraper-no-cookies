thonfrom datetime import datetime, timezone
from typing import Optional

from dateutil import parser as date_parser

from utils.logger import get_logger

logger = get_logger(__name__)

def parse_datetime(text: str, default_tz: timezone = timezone.utc) -> Optional[datetime]:
    """
    Parse a human-readable date string into a timezone-aware datetime.

    If parsing fails, returns None instead of raising.
    """
    if not text:
        return None

    try:
        dt = date_parser.parse(text)
    except (ValueError, TypeError) as exc:
        logger.warning("Failed to parse datetime from %r: %s", text, exc)
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)

    return dt

def parse_datetime_to_unix_ms(text: str) -> Optional[int]:
    """
    Convert a human-readable date string into a Unix timestamp in milliseconds.

    Returns None if parsing fails.
    """
    dt = parse_datetime(text)
    if not dt:
        return None
    return int(dt.timestamp() * 1000)