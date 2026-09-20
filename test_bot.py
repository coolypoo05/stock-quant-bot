"""네트워크 없이 도는 스모크 테스트: python test_bot.py (또는 pytest)."""
import os
import tempfile

os.environ.setdefault("BOT_TOKEN", "test")

import pandas as pd  # noqa: E402

import backtest, config, data, scoring, screening, sector  # noqa: E402


def test_weighted_overall():
    assert scoring.calc_weighted_overall(80, 60, 40) == 64  # 32+24+8
    assert scoring.calc_weighted_overall(80, 60, 0) == 66   # 데이터 없는 팩터는 중립 50 (32+24+10)
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


def test_roe_units_over_100_percent():
    # yfinance ROE는 항상 소수: 1.4875 = 148.75% (자사주 매입으로 자본이 작은 기업)
    score, details = scoring.score_quality({"roe": 1.4875, "sector": "Technology", "name": "X"})
    assert any("148.75%" in d and "매우 우수" in d for d in details)
    _, details = scoring.score_quality({"roe": 0.034, "sector": "Technology", "name": "X"})
    assert any("3.40%" in d for d in details)


def test_coverage_shrinks_toward_neutral():
    # PER 하나(가중치 20/100)만 있으면 85점이 아니라 50+35*0.2=57점
    score, details = scoring.score_value({"pe_ratio": 8})
    assert score == 57 and any("커버리지 20%" in d for d in details)
    assert scoring._finalize([(85, 100)], [], 100)[0] == 85  # 전부 있으면 보정 없음
    assert scoring._finalize([], [], 100) == (0, ["데이터 부족"])


def test_kr_flow_uses_cumulative_amount():
    import numpy as np
    idx = pd.date_range("2025-01-01", periods=200)
    hist = pd.DataFrame({"Close": np.linspace(100, 150, 200), "High": 151.0, "Low": 99.0, "Volume": 1000.0}, index=idx)
    base = {"history": hist, "market": "KR"}
    _, d = scoring.score_momentum({**base, "foreigner_amt_20d": 50000, "institution_amt_20d": 30000,
                                   "foreigner_amt_5d": 1000, "institution_amt_5d": -500})
    assert any("동반 순매수" in x for x in d) and any("500억" in x for x in d)
    _, d = scoring.score_momentum({**base, "foreigner_amt_20d": -50000, "institution_amt_20d": -30000,
                                   "foreigner_amt_5d": 1, "institution_amt_5d": 1})
    assert any("동반 순매도" in x for x in d)
    _, d = scoring.score_momentum(base)  # KIS 수급 없음 → 수급 섹션 생략
    assert not any("누적 순매수" in x for x in d)


def test_dividend_percent_units():
    # yfinance 0.2.54+ 배당수익률은 % 단위: 0.32 = 0.32%
    assert any("0.32%" in l for l in scoring.get_dividend_info({"dividend_yield": 0.32}))
    assert any("2.40%" in l for l in scoring.get_dividend_info({"dividend_yield": 2.4}))


def test_sector_comparison_roe_units():
    sector.SECTOR_CACHE.clear()
    sector.SECTOR_CACHE["Technology"] = {"per": 20.0, "pbr": 5.0, "roe": 15.0, "operating_margin": 20.0, "count": 5, "ko_name": "기술"}
    lines = sector.get_sector_comparison("Technology", {"roe": 1.4875, "operating_margin": 0.3})
    assert any("ROE: 148.8 vs 평균 15.0" in l for l in lines)
    assert any("영업이익률: 30.0 vs 평균 20.0" in l for l in lines)


def test_screening_units():
    class FakeTicker:
        def __init__(self, _):
            self.info = {"currentPrice": 10, "currency": "USD", "marketCap": 1e9, "returnOnEquity": 1.4875,
                         "operatingMargins": 0.3, "dividendYield": 0.32, "payoutRatio": 0.6246, "revenueGrowth": 0.166}
    orig = screening.yf.Ticker
    screening.yf.Ticker = FakeTicker
    try:
        d = screening.fetch_stock_quick({"code": "X", "suffix": "", "name": "X", "market": "SP500"})
    finally:
        screening.yf.Ticker = orig
    assert round(d["roe"], 2) == 148.75 and round(d["operating_margin"], 1) == 30.0
    assert d["dividend_yield"] == 0.32 and d["payout_ratio"] == 62.5 and round(d["revenue_growth"], 1) == 16.6


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("OK")
