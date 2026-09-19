"""네트워크 없이 도는 스모크 테스트: python test_bot.py (또는 pytest)."""
import os
import tempfile

os.environ.setdefault("BOT_TOKEN", "test")

import pandas as pd  # noqa: E402

import backtest, config, data, scoring, screening, sector  # noqa: E402


def test_weighted_overall():
    assert scoring.calc_weighted_overall(80, 60, 40) == 64  # 32+24+8
    assert scoring.calc_weighted_overall(80, 60, 0) == 70   # 모멘텀 없으면 가중치 정규화
    assert scoring.calc_weighted_overall(0, 0, 0) == 0


def test_grade_score():
    assert scoring.grade_score(80)[0] == "🟢 A"
    assert scoring.grade_score(29)[0] == "🔴 F"


def test_parse_and_check_conditions():
    conds = screening.parse_screen_conditions("PER<10 ROE>=15 PROFITABLE")
    assert [c["raw"] for c in conds] == ["PER", "ROE", "PROFITABLE"]
    assert all(screening.check_condition({"pe_ratio": 8, "roe": 15, "eps": 3}, c) for c in conds)
    assert not screening.check_condition({"pe_ratio": 12}, conds[0])
    assert not screening.check_condition({}, conds[0])  # 값 없으면 탈락


def test_max_drawdown():
    prices = pd.Series([100, 120, 60, 90], index=pd.date_range("2024-01-01", periods=4))
    mdd, date = backtest.calc_max_drawdown(prices)
    assert round(mdd, 1) == -50.0 and date == "2024-01-03"


def test_record_scrape_and_alert():
    sent = []
    orig = config.requests.post
    config.requests.post = lambda url, **kw: sent.append(kw["json"]["text"])
    config.ADMIN_CHAT_ID, config._last_alert = "1", {}
    try:
        data.SCRAPE_STATS.clear()
        data.record_scrape("x", True)
        data.record_scrape("x", False)
        assert data.SCRAPE_STATS["x"] == [1, 1]
        data.SCRAPE_STATS.clear()
        for _ in range(50):
            data.record_scrape("naver", False)
        assert len(sent) == 1 and "naver" in sent[0]
    finally:
        config.requests.post, config.ADMIN_CHAT_ID = orig, None


def test_notify_admin_disabled_and_cooldown():
    sent = []
    orig = config.requests.post
    config.requests.post = lambda url, **kw: sent.append(kw["json"]["text"])
    try:
        config.ADMIN_CHAT_ID, config._last_alert = None, {}
        config.notify_admin("k", "a")
        assert sent == []                      # 미설정이면 전송 안 함
        config.ADMIN_CHAT_ID = "1"
        config.notify_admin("k", "a")
        config.notify_admin("k", "b")          # 쿨다운
        config.notify_admin("k2", "c")
        assert sent == ["a", "c"]
    finally:
        config.requests.post, config.ADMIN_CHAT_ID = orig, None


def test_sector_cache_roundtrip():
    sector.SECTOR_CACHE_PATH = os.path.join(tempfile.mkdtemp(), "sc.json")
    assert sector.load_sector_cache() is False
    sector.SECTOR_CACHE.clear()
    sector.SECTOR_CACHE.update({"Technology": {"per": 18.2, "count": 3}})
    sector.SECTOR_CACHE_DATE = "2026-09-20"
    sector.save_sector_cache()
    sector.SECTOR_CACHE.clear()
    sector.SECTOR_CACHE_DATE = None
    assert sector.load_sector_cache() is True
    assert sector.SECTOR_CACHE["Technology"]["per"] == 18.2 and sector.SECTOR_CACHE_DATE == "2026-09-20"


def test_factor_cache():
    calls = []
    orig = scoring._process_factor
    scoring._FACTOR_CACHE.clear()
    scoring._process_factor = lambda q: calls.append(q) or "MSG"
    try:
        assert scoring.process_factor(" 삼성전자 ") == "MSG" and scoring.process_factor("삼성전자") == "MSG"
        assert len(calls) == 1
        scoring._process_factor = lambda q: None
        assert scoring.process_factor("없는종목") is None
        assert "없는종목" not in scoring._FACTOR_CACHE  # 실패는 캐시 안 함
    finally:
        scoring._process_factor = orig


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("OK")
