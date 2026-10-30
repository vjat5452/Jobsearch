"""Human-paced interaction: random pauses, natural typing, scrolling."""
from __future__ import annotations

import random
import time

from .config import section


def _pacing(key: str, default):
    return section("pacing").get(key) or default


def pause(lo: float | None = None, hi: float | None = None) -> None:
    if lo is None:
        lo, hi = _pacing("think", [1.0, 3.0])
    time.sleep(random.uniform(lo, hi))


def between_pages() -> None:
    pause(*_pacing("between_pages", [4, 9]))


def between_applications(log=print) -> None:
    lo, hi = _pacing("between_applications", [60, 180])
    secs = random.uniform(lo, hi)
    log(f"   (next application in {secs:.0f}s)")
    time.sleep(secs)


def type_text(locator, text) -> None:
    locator.click()
    locator.fill("")
    locator.press_sequentially(str(text), delay=random.randint(35, 110))


def scroll(page, times: int | None = None) -> None:
    for _ in range(times or random.randint(2, 4)):
        page.mouse.wheel(0, random.randint(300, 700))
        time.sleep(random.uniform(0.4, 1.2))
