"""Text and version filters (the same rules as NinjaOneLibrary's software filters).

Text filters (vendor, name) are case-insensitive substrings, or globs if the pattern has
`*`, `?` or `[`. The version filter is one of:

    "<24.09", ">=2.4,<2.4.62", "!=1.0"    PEP 440 specifier set (starts with < > = ! ~)
    "24.*", "1.2.?"                       glob on the raw version string
    "24.09"                               exact match on the raw version string

Reported versions are not always PEP 440 ("2.4.58 (x64)", "1.2.3-beta"). For specifiers
the leading dotted number is used; a version with no such number never matches one.
"""

from __future__ import annotations

import fnmatch
import re

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

_LEADING_VERSION = re.compile(r"\d+(?:\.\d+)*")
_EPOCH = re.compile(r"^\s*\d+:")
_SPECIFIER_START = ("<", ">", "=", "!", "~")
_GLOB = "*?["


def is_glob(pattern: str) -> bool:
    return any(char in pattern for char in _GLOB)


def text_matches(value: str | None, pattern: str | None) -> bool:
    """Case-insensitive: a glob if the pattern has glob characters, else a substring."""
    if not pattern:
        return True
    if value is None:
        return False
    value, pattern = value.lower(), pattern.lower()
    if is_glob(pattern):
        return fnmatch.fnmatchcase(value, pattern)
    return pattern in value


def name_matches(value: str | None, pattern: str) -> bool:
    """Stricter than text_matches, for picking objects to change: a glob, else an exact
    (case-insensitive) name. "Google" must not select "Google Chrome Helper"."""
    if value is None:
        return False
    if is_glob(pattern):
        return fnmatch.fnmatchcase(value.lower(), pattern.lower())
    return value.strip().casefold() == pattern.strip().casefold()


def parse_version(raw: str | None) -> Version | None:
    """The leading dotted number of a version string, as a comparable Version."""
    if not raw:
        return None
    match = _LEADING_VERSION.search(_EPOCH.sub("", raw))
    if not match:
        return None
    try:
        return Version(match.group(0))
    except InvalidVersion:
        return None


def version_specifier(wanted: str) -> SpecifierSet | None:
    """The SpecifierSet for a version filter that is one, else None (glob/exact)."""
    text = wanted.strip()
    if not text.startswith(_SPECIFIER_START):
        return None
    try:
        return SpecifierSet(text)
    except InvalidSpecifier as exc:
        raise ValueError(f"{wanted!r} is not a version specifier (try '<24.09' or '>=2.4,<2.4.62')") from exc


def version_matches(raw: str | None, wanted: str | None) -> bool:
    """Whether a reported version satisfies a version filter (see the module docstring)."""
    if not wanted or not wanted.strip():
        return True
    specifier = version_specifier(wanted)
    if specifier is not None:
        version = parse_version(raw)
        return version is not None and specifier.contains(version, prereleases=True)
    if raw is None:
        return False
    pattern = wanted.strip().lower()
    if is_glob(pattern):
        return fnmatch.fnmatchcase(raw.strip().lower(), pattern)
    return raw.strip().lower() == pattern


def version_key(raw: str | None) -> tuple[bool, Version | str, str]:
    """Sort key: parseable versions by value, then the rest as text."""
    parsed = parse_version(raw)
    return (parsed is not None, parsed or "", raw or "")
