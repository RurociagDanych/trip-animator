"""Normalize activity names for portable accent-insensitive substring search."""

import unicodedata


def normalize_activity_search(value: str) -> str:
    """Fold case and diacritics, including the non-decomposing Polish letter ł."""
    folded = unicodedata.normalize("NFKD", value).casefold().replace("ł", "l")
    return "".join(character for character in folded if not unicodedata.combining(character))
