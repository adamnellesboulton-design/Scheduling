"""British Columbia statutory holidays (BCNU Provincial Collective Agreement,
Art. 17 "Statutory Holidays").

Used to size each line's paid statutory-holiday entitlement over a rotation.
Dates are computed per calendar year (Easter via the anonymous Gregorian
algorithm); the named list mirrors the holidays recognized in the agreement.
"""

from __future__ import annotations

from datetime import date, timedelta


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian (Meeus/Jones/Butcher) algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th `weekday` (0=Mon) of a month, e.g. 3rd Monday of February."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _victoria_day(year: int) -> date:
    """Monday on or before May 24 (the Monday preceding May 25)."""
    d = date(year, 5, 24)
    return d - timedelta(days=(d.weekday()))  # back up to Monday


def bc_statutory_holidays(year: int) -> list[tuple[date, str]]:
    """The BCNU Art. 17 statutory holidays for a given year."""
    easter = _easter_sunday(year)
    return [
        (date(year, 1, 1), "New Year's Day"),
        (_nth_weekday(year, 2, 0, 3), "Family Day"),
        (easter - timedelta(days=2), "Good Friday"),
        (easter + timedelta(days=1), "Easter Monday"),
        (_victoria_day(year), "Victoria Day"),
        (date(year, 7, 1), "Canada Day"),
        (_nth_weekday(year, 8, 0, 1), "British Columbia Day"),
        (_nth_weekday(year, 9, 0, 1), "Labour Day"),
        (date(year, 9, 30), "National Day for Truth and Reconciliation"),
        (_nth_weekday(year, 10, 0, 2), "Thanksgiving Day"),
        (date(year, 11, 11), "Remembrance Day"),
        (date(year, 12, 25), "Christmas Day"),
        (date(year, 12, 26), "Boxing Day"),
    ]


def holidays_in_range(start: date, end_exclusive: date) -> list[tuple[date, str]]:
    """All statutory holidays with start <= date < end_exclusive, sorted."""
    out = []
    for year in range(start.year, end_exclusive.year + 1):
        for d, name in bc_statutory_holidays(year):
            if start <= d < end_exclusive:
                out.append((d, name))
    out.sort(key=lambda t: t[0])
    return out
