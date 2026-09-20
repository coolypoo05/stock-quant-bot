"""네트워크 없이 도는 스모크 테스트: python test_bot.py (또는 pytest)."""
import os
import tempfile

os.environ.setdefault("BOT_TOKEN", "test")

import pandas as pd  # noqa: E402

import backtest, config, data, flow, kis_api, scoring, screening, sector, snapshot  # noqa: E402


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


def _snap_db():
    snapshot.SNAPSHOT_DB_PATH = os.path.join(tempfile.mkdtemp(), "snap.db")


def _quick_row(code="AAPL", **kw):
    row = {"code": code, "name": code, "market": "SP500", "price": 100.0, "market_cap_bil": 3000000.0,
           "pe_ratio": 25.0, "forward_pe": 22.0, "pb_ratio": 8.0, "ps_ratio": 6.0, "peg_ratio": 1.5,
           "roe": 148.75, "roa": 20.0, "operating_margin": 30.0, "net_margin": 25.0, "gross_margin": 44.0,
           "debt_to_equity": 150.0, "current_ratio": 1.0, "dividend_yield": 0.32, "payout_ratio": 15.0,
           "revenue_growth": 8.0, "earnings_growth": 10.0, "sector": "Technology", "industry": "Consumer Electronics"}
    row.update(kw)
    return row


def test_snapshot_save_maps_columns_and_is_idempotent():
    import sqlite3
    _snap_db()
    rows = [_quick_row(), _quick_row("MSFT", roe=None, sector="", pe_ratio=float("nan"))]
    assert snapshot.save_factor_rows(rows, date="2026-09-21") == 2
    assert snapshot.save_factor_rows([_quick_row(price=111.0)], date="2026-09-21") == 1  # 같은 날 재저장은 덮어쓰기
    conn = sqlite3.connect(snapshot.SNAPSHOT_DB_PATH)
    got = conn.execute("SELECT code, price, roe, sector, op_margin, div_yield, pe FROM factor_snapshot ORDER BY code").fetchall()
    conn.close()
    assert got == [("AAPL", 111.0, 148.75, "Technology", 30.0, 0.32, 25.0),
                   ("MSFT", 100.0, None, "", 30.0, 0.32, None)]  # None/NaN은 NULL, 업종 없는 종목도 저장


def test_snapshot_summary_and_describe():
    _snap_db()
    assert snapshot.summary() == {"days": 0, "last_date": None, "factor_rows": 0, "flow_rows": 0}
    assert "아직" in snapshot.describe(snapshot.summary())
    snapshot.save_factor_rows([_quick_row("A"), _quick_row("B")], date="2026-09-20")
    snapshot.save_factor_rows([_quick_row("A"), _quick_row("B"), _quick_row("C")], date="2026-09-21")
    snapshot.save_flow_rows([{"code": "005930", "ratio": -8.1, "adv_eok": 44936,
                              "foreigner_amt_20d": -7099400, "institution_amt_20d": 185900}], date="2026-09-21")
    s = snapshot.summary()
    assert s == {"days": 2, "last_date": "2026-09-21", "factor_rows": 3, "flow_rows": 1}
    text = snapshot.describe(s)
    assert "2026-09-21" in text and "2일" in text and "3행" in text and "1행" in text


def test_snapshot_low_coverage_alert_and_error_swallowed():
    _snap_db()
    sent = []
    orig = snapshot.notify_admin
    snapshot.notify_admin = lambda key, text, **kw: sent.append(key)
    try:
        snapshot.save_factor_rows([_quick_row("A")], date="2026-09-21", expected=10)  # 1/10 < 60%
        snapshot.save_factor_rows([_quick_row(str(i)) for i in range(7)], date="2026-09-22", expected=10)  # 정상
        assert sent == ["snapshot:factor"]
        flow_row = {"code": "1", "ratio": 1.0, "adv_eok": 5.0, "foreigner_amt_20d": 1, "institution_amt_20d": 1}
        snapshot.save_flow_rows([flow_row], date="2026-09-21", expected=10)
        assert sent == ["snapshot:factor", "snapshot:flow"]
        snapshot.SNAPSHOT_DB_PATH = os.path.join(tempfile.mkdtemp(), "no_such_dir", "x.db")  # 열 수 없는 경로
        assert snapshot.save_factor_rows([_quick_row()], date="2026-09-21") == 0  # 예외를 삼키고 0 반환
        assert sent[-1] == "snapshot:error"
    finally:
        snapshot.notify_admin = orig


def test_build_sector_cache_returns_all_rows_including_no_sector():
    saved = (sector.fetch_stock_quick, sector.time.sleep, sector.SECTOR_CACHE_PATH, list(sector.SCREENING_UNIVERSE))
    results = {"A": {"code": "A", "sector": "Technology", "pe_ratio": 10, "pb_ratio": 1, "roe": 15},
               "B": {"code": "B", "sector": "", "pe_ratio": 20},  # 업종 없음
               "C": None}                                        # 조회 실패
    sector.fetch_stock_quick = lambda item: results[item["code"]]
    sector.time.sleep = lambda s: None
    sector.SECTOR_CACHE_PATH = os.path.join(tempfile.mkdtemp(), "sc.json")
    sector.SCREENING_UNIVERSE[:] = [{"code": c, "name": c, "suffix": "", "market": "SP500"} for c in "ABC"]
    try:
        rows = sector.build_sector_cache()
        assert [r["code"] for r in rows] == ["A", "B"]      # 조회 실패는 제외, 업종 없는 종목은 스냅샷용으로 포함
        assert list(sector.SECTOR_CACHE) == ["Technology"]  # 업종 평균은 업종이 있는 종목만
        sector.SCREENING_UNIVERSE.clear()
        assert sector.build_sector_cache() == []            # 유니버스가 비면 빈 목록
    finally:
        sector.fetch_stock_quick, sector.time.sleep, sector.SECTOR_CACHE_PATH = saved[:3]
        sector.SCREENING_UNIVERSE[:] = saved[3]


def test_collect_flow_rows_keeps_all_kr_stocks_with_ratio():
    dates = [f"202601{d:02d}" for d in range(1, 21)]

    def inv(code):
        if code == "111111":
            return None  # 조회 실패
        return {"flows": [(d, 60, 40) for d in dates], "foreigner_amt_20d": 5, "institution_amt_20d": 7}

    def daily(code):
        close = 1_000_000 if code == "005930" else 10_000
        return [{"date": d, "close": close, "volume": 1000} for d in dates]

    orig = (flow.kis_api.get_investor_trend, flow.kis_api.get_daily_volume)
    flow.kis_api.get_investor_trend, flow.kis_api.get_daily_volume = inv, daily
    try:
        universe = [{"code": "005930", "market": "KOSPI200"}, {"code": "999999", "market": "KOSDAQ150"},
                    {"code": "111111", "market": "KOSPI200"}, {"code": "AAPL", "market": "SP500"}]
        rows = flow.collect_flow_rows(universe, pause=0)
    finally:
        flow.kis_api.get_investor_trend, flow.kis_api.get_daily_volume = orig
    assert [r["code"] for r in rows] == ["005930", "999999"]  # 미국·조회 실패 제외, 유동성 낮은 종목도 포함
    assert rows[0] == {"code": "005930", "ratio": 10.0, "adv_eok": 10.0, "foreigner_amt_20d": 5, "institution_amt_20d": 7}
    assert round(rows[1]["adv_eok"], 2) == 0.1


def test_run_flow_snapshot_saves_all_rows_but_distribution_uses_liquid_only():
    import numpy as np
    liquid = [{"code": f"L{i}", "ratio": float(r), "adv_eok": 50.0, "foreigner_amt_20d": 1, "institution_amt_20d": 1}
              for i, r in enumerate(np.linspace(-12, 12, 60))]
    illiquid = [{"code": f"I{i}", "ratio": 100.0, "adv_eok": 1.0, "foreigner_amt_20d": 1, "institution_amt_20d": 1}
                for i in range(5)]
    saved = {}
    orig = (flow.collect_flow_rows, flow.snapshot.save_flow_rows, flow.FLOW_SNAPSHOT_PATH, list(flow.SCREENING_UNIVERSE))
    flow.collect_flow_rows = lambda: liquid + illiquid
    flow.snapshot.save_flow_rows = lambda rows, **kw: saved.update(rows=rows, **kw)
    flow.FLOW_SNAPSHOT_PATH = os.path.join(tempfile.mkdtemp(), "flow.jsonl")
    flow.SCREENING_UNIVERSE[:] = [{"code": "1", "market": "KOSPI200"}, {"code": "2", "market": "KOSDAQ150"},
                                  {"code": "3", "market": "KOSPI200"}, {"code": "AAPL", "market": "SP500"}]
    try:
        result = flow.run_flow_snapshot()
    finally:
        flow.collect_flow_rows, flow.snapshot.save_flow_rows, flow.FLOW_SNAPSHOT_PATH = orig[:3]
        flow.SCREENING_UNIVERSE[:] = orig[3]
    assert len(saved["rows"]) == 65 and saved["expected"] == 3  # 저장은 전체, 기대 행 수는 한국 유니버스 크기
    assert result["n"] == 60 and result["p90"] < 20             # 분포는 유동성 충분 종목만 (비유동 100%가 섞이면 크게 왜곡)


def test_bot_health_text_and_snapshot_wiring():
    import bot
    _snap_db()
    data.SCRAPE_STATS.clear()
    text = bot.format_health()
    assert "아직 스크래핑 기록이 없어요" in text and "스냅샷: 아직" in text
    data.SCRAPE_STATS["naver"] = [8, 2]
    snapshot.save_factor_rows([_quick_row("A")], date="2026-09-21")
    text = bot.format_health()
    assert "naver: 성공 8 / 실패 2 (20% 실패)" in text and "2026-09-21" in text
    data.SCRAPE_STATS.clear()
    # 업종 캐시 재빌드 결과가 팩터 스냅샷으로 넘어가고, 기대 행 수는 유니버스 크기
    calls = {}
    orig = (bot.build_sector_cache, bot.save_factor_rows, list(bot.SCREENING_UNIVERSE))
    bot.build_sector_cache = lambda: [{"code": "A"}]
    bot.save_factor_rows = lambda rows, **kw: calls.update(rows=rows, **kw)
    bot.SCREENING_UNIVERSE[:] = [{"code": "A"}, {"code": "B"}]
    try:
        bot.rebuild_sector_cache_and_snapshot()
    finally:
        bot.build_sector_cache, bot.save_factor_rows = orig[:2]
        bot.SCREENING_UNIVERSE[:] = orig[2]
    assert calls == {"rows": [{"code": "A"}], "expected": 2}


def test_fetch_stock_quick_fills_kr_per_pbr_from_naver():
    base = {"currentPrice": 10, "currency": "KRW", "marketCap": 1e12, "earningsGrowth": 0.25}
    infos = {"005930.KS": dict(base),
             "AAPL": {**base, "currency": "USD", "trailingPE": 30.0, "priceToBook": 40.0},
             "000660.KS": {**base, "trailingPE": 8.0},  # 한국 종목인데 PER만 yfinance에 있음
             "035420.KS": dict(base)}

    class FakeTicker:
        def __init__(self, tk):
            self.info = dict(infos[tk])

    naver_calls = []

    def fake_naver(code):
        naver_calls.append(code)
        return {"per": None, "pbr": None} if code == "035420" else {"per": 12.5, "pbr": 1.2}

    orig = (screening.yf.Ticker, screening.get_naver_per_pbr)
    screening.yf.Ticker, screening.get_naver_per_pbr = FakeTicker, fake_naver
    try:
        def quick(code, suffix, market):
            return screening.fetch_stock_quick({"code": code, "suffix": suffix, "name": code, "market": market})
        kr = quick("005930", ".KS", "KOSPI200")
        us = quick("AAPL", "", "SP500")
        partial = quick("000660", ".KS", "KOSPI200")
        missing = quick("035420", ".KS", "KOSPI200")
    finally:
        screening.yf.Ticker, screening.get_naver_per_pbr = orig
    assert kr["pe_ratio"] == 12.5 and kr["pb_ratio"] == 1.2 and round(kr["peg_ratio"], 2) == 0.5  # 네이버로 채우고 PEG도 계산
    assert us["pe_ratio"] == 30.0 and us["pb_ratio"] == 40.0
    assert partial["pe_ratio"] == 8.0 and partial["pb_ratio"] == 1.2  # yfinance 값 우선, 없는 것만 채움
    assert missing["pe_ratio"] is None and missing["pb_ratio"] is None  # 네이버도 값이 없으면 None(적자 등)
    assert naver_calls == ["005930", "000660", "035420"]  # 미국 종목은 호출하지 않음


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("OK")
