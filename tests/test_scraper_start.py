"""Retrying Playwright's intermittent driver startup crash (no browser needed)."""

import pytest

import playwright.sync_api
from cbbsq import scraper as scraper_mod
from cbbsq.config import Settings
from cbbsq.scraper import Scraper


class FlakyStarter:
    def __init__(self, failures, message="Connection.init: Connection closed while reading from the driver"):
        self.failures, self.message, self.calls = failures, message, 0

    def __call__(self):
        return self

    def start(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise Exception(self.message)
        return "playwright"


@pytest.fixture
def scraper(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper_mod.time, "sleep", lambda s: None)
    s = Settings(email=None, password=None, scorecenter_url="https://example.test/sc", login_url=None,
                 date_mode="auto", db_path=tmp_path / "db", raw_dir=tmp_path / "raw",
                 profile_dir=tmp_path / "profile", browser_channel=None, delay_seconds=0)
    msgs = []
    sc = Scraper(s, log=msgs.append)
    sc.msgs = msgs
    return sc


def test_driver_crash_is_retried(scraper, monkeypatch):
    starter = FlakyStarter(failures=2)
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", starter)
    assert scraper._start_playwright() == "playwright"
    assert starter.calls == 3 and len(scraper.msgs) == 2


def test_driver_crash_gives_up_after_all_attempts(scraper, monkeypatch):
    starter = FlakyStarter(failures=99)
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", starter)
    with pytest.raises(Exception, match="Connection closed"):
        scraper._start_playwright(attempts=3)
    assert starter.calls == 3


def test_other_errors_are_not_retried(scraper, monkeypatch):
    starter = FlakyStarter(failures=1, message="something else")
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", starter)
    with pytest.raises(Exception, match="something else"):
        scraper._start_playwright()
    assert starter.calls == 1
