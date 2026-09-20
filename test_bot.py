"""네트워크 없이 도는 스모크 테스트: python test_bot.py (또는 pytest)."""
import os
import tempfile

os.environ.setdefault("BOT_TOKEN", "test")

import pandas as pd  # noqa: E402

import backtest, config, data, flow, kis_api, scoring, screening, sector  # noqa: E402


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


def test_sector_cache_date_uses_kst():
    # 서버가 UTC여도 저장 날짜는 KST 기준 (UTC 21:00 = KST 다음날 06:00)
    import datetime as real

    class FakeDT:
        @staticmethod
        def now(tz=None):
            return real.datetime(2026, 9, 21, 6, 0, tzinfo=tz) if tz else real.datetime(2026, 9, 20, 21, 0)

    saved = (sector.datetime, sector.fetch_stock_quick, sector.time.sleep, sector.SECTOR_CACHE_PATH, list(sector.SCREENING_UNIVERSE))
    sector.datetime = FakeDT
    sector.fetch_stock_quick = lambda item: {"sector": "Technology", "pe_ratio": 10, "pb_ratio": 1, "roe": 15}
    sector.time.sleep = lambda s: None
    sector.SECTOR_CACHE_PATH = os.path.join(tempfile.mkdtemp(), "sc.json")
    sector.SCREENING_UNIVERSE[:] = [{"code": "1", "name": "X", "suffix": "", "market": "SP500"}]
    try:
        sector.build_sector_cache()
        assert sector.SECTOR_CACHE_DATE == "2026-09-21"
    finally:
        sector.datetime, sector.fetch_stock_quick, sector.time.sleep, sector.SECTOR_CACHE_PATH = saved[:4]
        sector.SCREENING_UNIVERSE[:] = saved[4]


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


def _kr_base():
    import numpy as np
    idx = pd.date_range("2025-01-01", periods=200)
    hist = pd.DataFrame({"Close": np.linspace(100, 150, 200), "High": 151.0, "Low": 99.0, "Volume": 1000.0}, index=idx)
    return {"history": hist, "market": "KR", "foreigner_amt_20d": 50000, "institution_amt_20d": 30000,
            "foreigner_amt_5d": 1000, "institution_amt_5d": -500}


def test_kr_flow_intensity_and_liquidity():
    base = _kr_base()
    # 유동성 충분: 거래량 대비 강도로 채점
    _, d = scoring.score_momentum({**base, "flow_ratio_20d": 10.0, "adv_eok_20d": 500})
    assert any("+10.0%" in x and "강한 순매수" in x for x in d) and any("500억" in x for x in d)
    hi, _ = scoring.score_momentum({**base, "flow_ratio_20d": 10.0, "adv_eok_20d": 500})
    # 유동성 부족(신뢰도 50%): 같은 강도라도 중립 쪽으로 보정
    mid, d = scoring.score_momentum({**base, "flow_ratio_20d": 10.0, "adv_eok_20d": 5})
    assert any("신뢰도 50%" in x for x in d) and mid < hi
    # 저유동성: 수급 점수 제외
    _, d = scoring.score_momentum({**base, "flow_ratio_20d": 10.0, "adv_eok_20d": 2})
    assert any("저유동성" in x for x in d) and not any("→ 강한 순매수" in x for x in d)
    # 거래량 조회 실패: 방향만
    _, d = scoring.score_momentum(base)
    assert any("동반 순매수" in x for x in d)
    _, d = scoring.score_momentum({**base, "foreigner_amt_20d": -5, "institution_amt_20d": -3})
    assert any("동반 순매도" in x for x in d)
    # KIS 수급 자체가 없으면 섹션 생략
    _, d = scoring.score_momentum({"history": base["history"], "market": "KR"})
    assert not any("누적 순매수" in x for x in d)
    assert scoring._flow_score(0.0) == (50, "중립") and scoring._flow_score(-99)[0] == 10 and scoring._flow_score(99)[0] == 90


def test_calc_flow_intensity():
    dates = [f"202601{d:02d}" for d in range(1, 21)]
    flows = [(d, 60, 40) for d in dates]                       # 매일 순매수 100주
    daily = [{"date": d, "close": 10000, "volume": 1000} for d in dates]
    ratio, adv = kis_api.calc_flow_intensity(flows, daily)
    assert round(ratio, 2) == 10.0 and round(adv, 2) == 0.1    # 거래량 대비 10%, 일 거래대금 0.1억
    assert kis_api.calc_flow_intensity(flows, daily[:10]) == (None, None)  # 겹치는 일수 부족
    assert kis_api.calc_flow_intensity(None, daily) == (None, None)


def test_kis_get_retries_on_rate_limit():
    class Resp:
        def __init__(self, body):
            self.status_code, self._b = 200, body

        def json(self):
            return self._b

    seq = [Resp({"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수 초과"}), Resp({"rt_cd": "0", "output": [1]})]
    orig = (kis_api.get_headers, kis_api.requests.get, kis_api.time.sleep)
    kis_api.get_headers = lambda tr: {"h": 1}
    kis_api.requests.get = lambda *a, **k: seq.pop(0)
    kis_api.time.sleep = lambda s: None
    try:
        assert kis_api._kis_get("X", "/p", {"FID_INPUT_ISCD": "1"}) == {"rt_cd": "0", "output": [1]}
        seq[:] = [Resp({"rt_cd": "1", "msg1": "err"})]
        assert kis_api._kis_get("X", "/p", {"FID_INPUT_ISCD": "1"}) is None  # 다른 오류는 재시도 없이 None
    finally:
        kis_api.get_headers, kis_api.requests.get, kis_api.time.sleep = orig


def test_kis_token_file_reuse_and_failure_cooldown():
    class Resp:
        def __init__(self, ok=True):
            self.ok = ok

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError("403")

        def json(self):
            return {"access_token": "TOK", "expires_in": 86400}

    posts = []
    orig = (kis_api.requests.post, kis_api.time.sleep, kis_api.APPKEY, kis_api.APPSECRET, kis_api.KIS_TOKEN_PATH)
    kis_api.time.sleep = lambda s: None
    kis_api.APPKEY, kis_api.APPSECRET = "k", "s"
    kis_api.KIS_TOKEN_PATH = os.path.join(tempfile.mkdtemp(), "tok.json")

    def reset():
        kis_api._token_cache.update(token=None, expires_at=0)

    try:
        # 발급 실패 → None, 대기 중에는 네트워크 호출 없이 None
        reset()
        kis_api._token_retry_after = 0.0
        kis_api.requests.post = lambda *a, **k: posts.append(1) or Resp(ok=False)
        assert kis_api.get_access_token() is None and len(posts) == 1
        assert kis_api.get_access_token() is None and len(posts) == 1
        # 대기 해제 후 발급 성공 → 파일 저장
        kis_api._token_retry_after = 0.0
        kis_api.requests.post = lambda *a, **k: posts.append(1) or Resp()
        assert kis_api.get_access_token() == "TOK" and len(posts) == 2
        if os.name != "nt":  # 토큰 파일은 소유자만 접근 가능해야 함 (Windows는 권한 비트가 무의미)
            assert os.stat(kis_api.KIS_TOKEN_PATH).st_mode & 0o077 == 0
        # 재시작 흉내: 메모리 캐시 삭제 → 파일에서 재사용, 발급 호출 없음
        reset()
        kis_api.requests.post = lambda *a, **k: (_ for _ in ()).throw(AssertionError("발급 호출 금지"))
        assert kis_api.get_access_token() == "TOK"
        # 다른 앱키로 저장된 토큰은 재사용하지 않음
        reset()
        kis_api.APPKEY = "other"
        kis_api._token_retry_after = 0.0
        kis_api.requests.post = lambda *a, **k: posts.append(1) or Resp()
        assert kis_api.get_access_token() == "TOK" and len(posts) == 3
    finally:
        kis_api.requests.post, kis_api.time.sleep, kis_api.APPKEY, kis_api.APPSECRET, kis_api.KIS_TOKEN_PATH = orig
        reset()
        kis_api._token_retry_after = 0.0


def test_bulleted_details_no_double_bullets():
    nl = chr(10)
    out = scoring._bulleted(["PER: 10", "📈 수익률", "   • 1M: +1%", nl + "📊 RSI: 50", "  → 실적 개선"])
    expected = nl.join(["   • PER: 10", "📈 수익률", "   • 1M: +1%", "", "📊 RSI: 50", "  → 실적 개선", ""])
    assert out == expected
    assert "•    •" not in out


def test_flow_summary_drift_and_snapshot():
    import numpy as np
    ok = flow.summarize(list(np.linspace(-12, 12, 101)))
    assert ok["n"] == 101 and flow.check_drift(ok) is None       # 밴드 설계에 맞는 분포
    shifted = flow.summarize(list(np.linspace(2, 30, 101)))      # 시장 전체가 순매수로 쏠림
    msg = flow.check_drift(shifted)
    assert msg and "중앙값" in msg
    path = os.path.join(tempfile.mkdtemp(), "snap.jsonl")
    flow.save_snapshot(ok, path)
    flow.save_snapshot(shifted, path)
    lines = open(path, encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 2 and __import__("json").loads(lines[0])["n"] == 101


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
