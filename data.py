"""종목 리스트 + 한국/미국 데이터 수집"""

import io
import re
import time
from datetime import datetime
import pandas as pd
import requests
import yfinance as yf
from config import HEADERS, STOCK_MAP, logger, notify_admin


# ============================================================
# 한국 종목 리스트
# ============================================================

def _fetch_stock_map_krx() -> dict:
    result = {}
    for market_type, suffix in [("stockMkt", ".KS"), ("kosdaqMkt", ".KQ")]:
        url = "https://kind.krx.co.kr/corpgeneral/corpList.do"
        params = {"method": "download", "searchType": "13", "marketType": market_type}
        res = requests.get(url, params=params, headers=HEADERS, timeout=30)
        res.raise_for_status()
        df = pd.read_html(io.StringIO(res.text))[0]
        df["종목코드"] = df["종목코드"].astype(str).str.zfill(6)
        for _, row in df.iterrows():
            result[row["회사명"]] = {"code": row["종목코드"], "suffix": suffix}
    return result


def _fetch_stock_map_naver() -> dict:
    """KRX 차단 시 폴백: 네이버 모바일 API 시가총액 목록 (ETF/ETN 제외)."""
    result = {}
    for market, suffix in [("KOSPI", ".KS"), ("KOSDAQ", ".KQ")]:
        page = 1
        while True:
            res = requests.get(
                f"https://m.stock.naver.com/api/stocks/marketValue/{market}",
                params={"page": page, "pageSize": 100}, headers=HEADERS, timeout=15,
            )
            res.raise_for_status()
            j = res.json()
            for st in j["stocks"]:
                if st.get("stockEndType") == "stock" and re.fullmatch(r"\d{6}", st.get("itemCode", "")):
                    result.setdefault(st["stockName"], {"code": st["itemCode"], "suffix": suffix})
            if not j["stocks"] or page * 100 >= j["totalCount"]:
                break
            page += 1
    return result


def load_stock_map():
    logger.info("한국 종목 리스트 로딩 중...")
    for source, fetch in (("KRX", _fetch_stock_map_krx), ("네이버", _fetch_stock_map_naver)):
        try:
            result = fetch()
            if result:
                STOCK_MAP.update(result)
                logger.info(f"한국 종목 리스트 로딩 완료 ({source}): {len(STOCK_MAP)}개")
                return
            logger.error(f"종목 리스트 로딩 실패 ({source}): 결과 없음")
        except Exception as e:
            logger.error(f"종목 리스트 로딩 실패 ({source}): {e}")


_last_stock_map_try = 0.0


def _ensure_stock_map() -> None:
    """사전이 비어 있으면 검색 시점에 재로딩 (60초 쿨다운)."""
    global _last_stock_map_try
    if STOCK_MAP or time.time() - _last_stock_map_try < 60:
        return
    _last_stock_map_try = time.time()
    load_stock_map()


def search_kor_stock(query: str):
    _ensure_stock_map()
    query = query.strip()
    # 6자리 코드로 검색
    if re.fullmatch(r"\d{6}", query):
        for name, info in STOCK_MAP.items():
            if info["code"] == query:
                return info["code"], name, info["suffix"]
        return query, query, None  # suffix 모름
    # 종목명으로 검색
    if query in STOCK_MAP:
        info = STOCK_MAP[query]
        return info["code"], query, info["suffix"]
    for name, info in STOCK_MAP.items():
        if name.lower() == query.lower():
            return info["code"], name, info["suffix"]
    for name, info in STOCK_MAP.items():
        if query.lower() in name.lower():
            return info["code"], name, info["suffix"]
    return None

def calc_ttm_operating_margin(t) -> float | None:
    """손익계산서에서 TTM(최근 12개월) 영업이익률 직접 계산."""
    try:
        # 분기 재무제표로 TTM 계산 (최근 4분기 합산)
        qf = t.quarterly_financials
        if qf is not None and not qf.empty and qf.shape[1] >= 4:
            op_income = None
            revenue = None
            for key in ["Operating Income", "Operating Revenue"]:
                if key in qf.index:
                    val = qf.loc[key].iloc[:4].sum()
                    if pd.notna(val):
                        if key == "Operating Income":
                            op_income = val
                        else:
                            revenue = val
            # 매출 찾기
            for key in ["Total Revenue", "Net Revenue", "Revenue"]:
                if key in qf.index:
                    val = qf.loc[key].iloc[:4].sum()
                    if pd.notna(val) and val > 0:
                        revenue = val
                        break
            if op_income is not None and revenue and revenue > 0:
                return op_income / revenue
        # 분기 데이터 없으면 연간 최신으로 fallback
        af = t.financials
        if af is not None and not af.empty:
            op_income = None
            revenue = None
            for key in ["Operating Income"]:
                if key in af.index:
                    val = af.loc[key].iloc[0]
                    if pd.notna(val):
                        op_income = val
            for key in ["Total Revenue", "Net Revenue", "Revenue"]:
                if key in af.index:
                    val = af.loc[key].iloc[0]
                    if pd.notna(val) and val > 0:
                        revenue = val
                        break
            if op_income is not None and revenue and revenue > 0:
                return op_income / revenue
        return None
    except Exception as e:
        logger.debug(f"TTM 영업이익률 계산 실패: {e}")
        return None

# 영업이익률이 의미없는 섹터 (지주/금융/부동산)
EXCLUDE_OP_MARGIN_SECTORS = {
    "Financial Services", "Financial", "Real Estate",
    "금융", "보험", "은행", "지주", "부동산",
}

def is_holding_company(data: dict) -> bool:
    """지주/금융/부동산 계열 여부 판단."""
    sector = data.get("sector", "") or ""
    industry = data.get("industry", "") or ""
    name = data.get("name", "") or ""
    combined = f"{sector} {industry} {name}".lower()
    keywords = ["지주", "holding", "financial", "insurance", "bank", "real estate",
                "금융", "보험", "은행", "부동산", "investment"]
    return any(k in combined for k in keywords)

def calc_ev_ebitda(t, info: dict) -> float | None:
    """EV/EBITDA 직접 계산."""
    try:
        # EV = 시가총액 + 총부채 - 현금
        market_cap = info.get("marketCap")
        if not market_cap:
            return None

        bs = t.balance_sheet
        if bs is None or bs.empty:
            return None

        # 총부채
        total_debt = 0
        for key in ["Total Debt", "Long Term Debt", "Total Liabilities Net Minority Interest"]:
            if key in bs.index:
                val = bs.loc[key].iloc[0]
                if pd.notna(val):
                    total_debt = val
                    break

        # 현금 및 현금성자산
        cash = 0
        for key in ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"]:
            if key in bs.index:
                val = bs.loc[key].iloc[0]
                if pd.notna(val):
                    cash = val
                    break

        ev = market_cap + total_debt - cash

        # EBITDA = 영업이익 + 감가상각
        qf = t.quarterly_financials
        ebitda = None
        if qf is not None and not qf.empty and qf.shape[1] >= 4:
            for key in ["EBITDA", "Normalized EBITDA"]:
                if key in qf.index:
                    val = qf.loc[key].iloc[:4].sum()
                    if pd.notna(val) and val > 0:
                        ebitda = val
                        break
        if not ebitda:
            af = t.financials
            if af is not None and not af.empty:
                for key in ["EBITDA", "Normalized EBITDA"]:
                    if key in af.index:
                        val = af.loc[key].iloc[0]
                        if pd.notna(val) and val > 0:
                            ebitda = val
                            break

        if ebitda and ebitda > 0 and ev > 0:
            return ev / ebitda
        return None
    except Exception as e:
        logger.debug(f"EV/EBITDA 계산 실패: {e}")
        return None

def calc_debt_to_equity(t) -> float | None:
    """밸런스시트에서 총부채/자기자본 직접 계산 (일반적인 부채비율)."""
    try:
        bs = t.balance_sheet
        if bs is None or bs.empty:
            return None

        # 총부채 찾기
        total_liabilities = None
        for key in ["Total Liabilities Net Minority Interest", "Total Liabilities"]:
            if key in bs.index:
                val = bs.loc[key].iloc[0]
                if pd.notna(val):
                    total_liabilities = val
                    break

        # 자기자본 찾기
        equity = None
        for key in ["Stockholders Equity", "Total Equity Gross Minority Interest", "Common Stock Equity"]:
            if key in bs.index:
                val = bs.loc[key].iloc[0]
                if pd.notna(val) and val > 0:
                    equity = val
                    break

        if total_liabilities is not None and equity:
            return (total_liabilities / equity) * 100
        return None
    except Exception as e:
        logger.debug(f"부채비율 계산 실패: {e}")
        return None

# ============================================================
# 네이버 금융 fallback (한국 주식 PER/PBR)
# ============================================================

SCRAPE_STATS: dict = {}  # {소스: [성공, 실패]}

def record_scrape(source: str, ok: bool) -> None:
    stats = SCRAPE_STATS.setdefault(source, [0, 0])
    stats[0 if ok else 1] += 1
    total = sum(stats)
    if total % 50 == 0 and stats[1] / total > 0.5:
        msg = f"스크래핑 실패율 높음 [{source}]: {stats[1]}/{total} — 사이트 구조 변경 의심"
        logger.warning(msg)
        notify_admin(f"scrape:{source}", f"⚠️ {msg}")

def _naver_infos(code: str) -> dict:
    """네이버 모바일 API totalInfos → {code: value}."""
    res = requests.get(
        f"https://m.stock.naver.com/api/stock/{code}/integration",
        headers=HEADERS, timeout=10,
    )
    res.raise_for_status()
    return {t["code"]: t.get("value") for t in res.json()["totalInfos"]}


def _naver_num(text) -> float | None:
    m = re.search(r"-?[\d,]+(?:\.\d+)?", text or "")
    return float(m.group().replace(",", "")) if m else None


def get_naver_per_pbr(code: str) -> dict:
    """네이버 금융(모바일 API)에서 PER, PBR 조회."""
    result = {"per": None, "pbr": None}
    try:
        infos = _naver_infos(code)
        result["per"] = _naver_num(infos.get("per"))
        result["pbr"] = _naver_num(infos.get("pbr"))
        record_scrape("naver", result["pbr"] is not None)
    except Exception as e:
        record_scrape("naver", False)
        logger.debug(f"네이버 PER/PBR 조회 실패 ({code}): {e}")
    return result


def get_naver_forward_eps(code: str) -> float | None:
    """네이버 컨센서스 추정EPS(Forward EPS) 조회."""
    try:
        eps = _naver_num(_naver_infos(code).get("cnsEps"))
        record_scrape("naver_fwd_eps", True)
        return eps if eps and eps > 0 else None
    except Exception as e:
        record_scrape("naver_fwd_eps", False)
        logger.debug(f"네이버 Forward EPS 조회 실패 ({code}): {e}")
        return None


def calc_eps_from_financials(t_obj) -> float | None:
    """재무제표에서 EPS 직접 계산 (당기순이익 / 발행주식수)."""
    try:
        # 분기 합산으로 TTM 순이익 계산
        qf = t_obj.quarterly_financials
        shares = t_obj.info.get("sharesOutstanding")
        if not shares or shares <= 0:
            return None

        net_income = None
        if qf is not None and not qf.empty and qf.shape[1] >= 4:
            for key in ["Net Income", "Net Income Common Stockholders"]:
                if key in qf.index:
                    val = qf.loc[key].iloc[:4].sum()
                    if pd.notna(val):
                        net_income = val
                        break

        # 분기 없으면 연간
        if net_income is None:
            af = t_obj.financials
            if af is not None and not af.empty:
                for key in ["Net Income", "Net Income Common Stockholders"]:
                    if key in af.index:
                        val = af.loc[key].iloc[0]
                        if pd.notna(val):
                            net_income = val
                            break

        if net_income is not None:
            return net_income / shares
        return None
    except Exception as e:
        logger.debug(f"EPS 계산 실패: {e}")
        return None

# ============================================================
# 한국 주식 데이터 (KIS API 우선 + yfinance fallback)
# ============================================================

def get_kor_stock_data(code: str, name: str, known_suffix: str = None):
    try:
        # ── 1단계: KIS API로 핵심 데이터 수집 ──────────────────
        kis_data = None
        try:
            from kis_api import get_full_stock_data as kis_get
            kis_data = kis_get(code)
        except Exception as e:
            logger.debug(f"KIS API 임포트/호출 실패: {e}")

        # ── 2단계: yfinance로 히스토리 + 보완 데이터 수집 ──────
        ticker = None
        info = None
        hist = None
        t_obj = None

        suffixes = [known_suffix] if known_suffix else [".KS", ".KQ"]
        if known_suffix:
            suffixes = [known_suffix, ".KS" if known_suffix == ".KQ" else ".KQ"]

        for suffix in suffixes:
            try:
                t = yf.Ticker(f"{code}{suffix}")
                test_info = t.info
                if test_info and (test_info.get("regularMarketPrice") or test_info.get("currentPrice")):
                    ticker = f"{code}{suffix}"
                    info = test_info
                    hist = t.history(period="1y")
                    t_obj = t
                    break
            except Exception:
                continue

        # KIS도 실패하고 yfinance도 실패하면 None
        if not kis_data and not info:
            return None

        # ── 3단계: 병렬로 보완 데이터 수집 ─────────────────────
        from concurrent.futures import ThreadPoolExecutor, as_completed

        tasks = {}
        if t_obj:
            tasks["ev"]       = lambda: calc_ev_ebitda(t_obj, info)
            tasks["div"]      = lambda: calc_dividend_growth(t_obj)
            tasks["fscore"]   = lambda: calc_piotroski_fscore(t_obj, {"market_cap": info.get("marketCap") if info else None})
            tasks["fwd_eps"]  = lambda: get_naver_forward_eps(code)
            tasks["op_margin"] = lambda: calc_ttm_operating_margin(t_obj)
            tasks["ic"]        = lambda: calc_interest_coverage(t_obj)

        # KIS에 없는 지표만 yfinance로 보완
        if not kis_data:
            if t_obj:
                tasks["debt"]  = lambda: calc_debt_to_equity(t_obj)
                tasks["rev"]   = lambda: calc_revenue_growth(t_obj)
                tasks["naver"] = lambda: get_naver_per_pbr(code)

        results = {}
        if tasks:
            with ThreadPoolExecutor(max_workers=6) as executor:
                future_map = {executor.submit(fn): key for key, fn in tasks.items()}
                for future in as_completed(future_map):
                    key = future_map[future]
                    try:
                        results[key] = future.result()
                    except Exception as e:
                        logger.debug(f"병렬 계산 실패 ({key}): {e}")
                        results[key] = None

        ev_ebitda      = results.get("ev")
        dividend_growth = results.get("div") or {}
        fscore_info    = results.get("fscore")
        forward_eps    = results.get("fwd_eps")

        # ── 4단계: KIS 데이터 우선, yfinance로 fallback ─────────
        if kis_data:
            # KIS API 데이터 사용
            price          = kis_data["price"]
            previous_close = kis_data["previous_close"]
            market_cap     = kis_data["market_cap"] * 100_000_000 if kis_data["market_cap"] else info.get("marketCap") if info else None
            pe_ratio       = kis_data["pe_ratio"]
            pb_ratio       = kis_data["pb_ratio"]
            eps            = kis_data["eps"] or (info.get("trailingEps") if info else None)
            roe            = kis_data["roe"] / 100 if kis_data["roe"] else None
            debt_ratio     = kis_data["debt_to_equity"]
            rev_growth     = kis_data["revenue_growth"] or None
            div_yield      = kis_data["dividend_yield"] or (info.get("dividendYield") if info else None)

            # KIS 미제공 → yfinance fallback
            roa       = info.get("returnOnAssets") if info else None
            op_margin = results.get("op_margin") or (info.get("operatingMargins") if info else None)
            interest_cov = results.get("ic") or (info.get("operatingIncome", 0) / info.get("interestExpense", 1) if info and info.get("interestExpense") else None)

            logger.info(f"KIS API 사용: {code} 현재가={price:,}원 PER={pe_ratio} ROE={roe}")
        else:
            # yfinance fallback
            logger.info(f"yfinance fallback 사용: {code}")
            naver      = results.get("naver") or {}
            price      = info.get("currentPrice") or info.get("regularMarketPrice")
            previous_close = info.get("previousClose")
            market_cap = info.get("marketCap")
            pe_ratio   = info.get("trailingPE") or naver.get("per")
            pb_ratio   = info.get("priceToBook") or naver.get("pbr")
            eps        = info.get("trailingEps") or calc_eps_from_financials(t_obj)
            roe        = info.get("returnOnEquity")
            roa        = info.get("returnOnAssets")
            op_margin  = results.get("op_margin") or info.get("operatingMargins")
            debt_ratio = results.get("debt") or info.get("debtToEquity")
            interest_cov = results.get("ic")
            rev_growth = results.get("rev")
            div_yield  = info.get("dividendYield")

        # Forward PE / Forward EPS (KIS 미지원 → yfinance + 네이버)
        forward_pe  = info.get("forwardPE") if info else None
        if not forward_eps and info:
            forward_eps = info.get("forwardEps") or get_naver_forward_eps(code)

        # PS ratio (KIS 미지원 → yfinance)
        ps_ratio = info.get("priceToSalesTrailing12Months") if info else None

        return {
            "code": code, "name": name,
            "ticker": ticker or f"{code}.KS",
            "price": price,
            "previous_close": previous_close,
            "market_cap": market_cap,
            "pe_ratio": pe_ratio,
            "forward_pe": forward_pe,
            "eps": eps,
            "forward_eps": forward_eps,
            "pb_ratio": pb_ratio,
            "ps_ratio": ps_ratio,
            "ev_ebitda": ev_ebitda,
            "roe": roe,
            "roa": roa,
            "operating_margin": op_margin,
            "debt_to_equity": debt_ratio,
            "interest_coverage": interest_cov,
            "revenue_growth": rev_growth,
            "dividend_yield": div_yield,
            "payout_ratio": info.get("payoutRatio") if info else None,
            "dividend_growth": dividend_growth,
            "fscore_info": fscore_info,
            "beta": info.get("beta") if info else None,
            "sector": info.get("sector", "") if info else "",
            "industry": info.get("industry", "") if info else "",
            "history": hist,
            "currency": "KRW", "market": "KR",
            # 수급 정보 (KIS API)
            "foreigner_net": kis_data.get("foreigner_net") if kis_data else None,
            "institution_net": kis_data.get("institution_net") if kis_data else None,
            **({k: kis_data.get(k) for k in ("foreigner_amt_5d", "institution_amt_5d", "foreigner_amt_20d", "institution_amt_20d")}
               if kis_data else {}),
        }
    except Exception as e:
        logger.error(f"한국 주식 데이터 실패 ({code}): {e}")
        return None

# ============================================================
# 미국 주식 데이터
# ============================================================

def get_us_stock_data(ticker: str):
    try:
        t = yf.Ticker(ticker)
        info = t.info
        if not info or (info.get("regularMarketPrice") is None and info.get("currentPrice") is None):
            return None

        # 병렬로 무거운 데이터 수집
        from concurrent.futures import ThreadPoolExecutor, as_completed

        tasks_us = {
            "hist":    lambda: t.history(period="1y"),
            "debt":    lambda: calc_debt_to_equity(t),
            "op":      lambda: calc_ttm_operating_margin(t),
            "ev":      lambda: calc_ev_ebitda(t, info),
            "ic":      lambda: calc_interest_coverage(t),
            "rev":     lambda: calc_revenue_growth(t),
            "div":     lambda: calc_dividend_growth(t),
            "fscore":  lambda: calc_piotroski_fscore(t, {"market_cap": info.get("marketCap")}),
        }

        res_us = {}
        with ThreadPoolExecutor(max_workers=6) as executor:
            future_map = {executor.submit(fn): key for key, fn in tasks_us.items()}
            for future in as_completed(future_map):
                key = future_map[future]
                try:
                    res_us[key] = future.result()
                except Exception as e:
                    logger.debug(f"US 병렬 계산 실패 ({key}): {e}")
                    res_us[key] = None

        hist = res_us.get("hist")
        debt_ratio = res_us.get("debt")
        op_margin = res_us.get("op")
        ev_ebitda = res_us.get("ev")
        interest_coverage = res_us.get("ic")
        revenue_growth = res_us.get("rev")
        dividend_growth = res_us.get("div") or {}
        fscore_info = res_us.get("fscore")

        return {
            "code": ticker.upper(), "name": info.get("longName") or info.get("shortName") or ticker,
            "ticker": ticker.upper(),
            "price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "previous_close": info.get("previousClose"),
            "market_cap": info.get("marketCap"),
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "eps": info.get("trailingEps"),
            "forward_eps": info.get("forwardEps"),
            "pb_ratio": info.get("priceToBook"),
            "ps_ratio": info.get("priceToSalesTrailing12Months"),
            "ev_ebitda": ev_ebitda,
            "roe": info.get("returnOnEquity"),
            "roa": info.get("returnOnAssets"),
            "operating_margin": op_margin,
            "debt_to_equity": debt_ratio,
            "interest_coverage": interest_coverage,
            "revenue_growth": revenue_growth,
            "dividend_yield": info.get("dividendYield"),
            "payout_ratio": info.get("payoutRatio"),
            "dividend_growth": dividend_growth,
            "fscore_info": fscore_info,
            "beta": info.get("beta"),
            "sector": info.get("sector", ""),
            "industry": info.get("industry", ""),
            "history": hist,
            "currency": "USD", "market": "US",
            # 미국 수급 대체 지표
            "institutional_pct": info.get("heldPercentInstitutions"),
            "insider_pct": info.get("heldPercentInsiders"),
            "short_ratio": info.get("shortRatio"),
        }
    except Exception as e:
        logger.error(f"미국 주식 데이터 실패 ({ticker}): {e}")
        return None

# ============================================================
# 팩터 스코어링
# ============================================================

def calc_interest_coverage(t_obj) -> float | None:
    """이자보상배율 = 영업이익 / 이자비용 (TTM)."""
    try:
        qf = t_obj.quarterly_financials
        op_income = None
        interest_exp = None

        if qf is not None and not qf.empty and qf.shape[1] >= 4:
            for key in ["Operating Income"]:
                if key in qf.index:
                    val = qf.loc[key].iloc[:4].sum()
                    if pd.notna(val):
                        op_income = val
            for key in ["Interest Expense", "Interest Expense Non Operating"]:
                if key in qf.index:
                    val = abs(qf.loc[key].iloc[:4].sum())
                    if pd.notna(val) and val > 0:
                        interest_exp = val
                        break

        if op_income is not None and interest_exp and interest_exp > 0:
            return op_income / interest_exp
        return None
    except Exception as e:
        logger.debug(f"이자보상배율 계산 실패: {e}")
        return None

def calc_revenue_growth(t_obj) -> float | None:
    """매출 성장률 = YoY (전년 대비 올해 매출 증가율, TTM 기준)."""
    try:
        qf = t_obj.quarterly_financials
        if qf is None or qf.empty or qf.shape[1] < 8:
            # 분기 8개 없으면 연간으로 시도
            af = t_obj.financials
            if af is not None and not af.empty and af.shape[1] >= 2:
                rev = None
                prev_rev = None
                for key in ["Total Revenue", "Revenue"]:
                    if key in af.index:
                        rev = af.loc[key].iloc[0]
                        prev_rev = af.loc[key].iloc[1]
                        break
                if rev and prev_rev and prev_rev > 0:
                    return ((rev - prev_rev) / abs(prev_rev)) * 100
            return None

        # 최근 4분기 vs 직전 4분기 비교
        rev_key = None
        for key in ["Total Revenue", "Revenue"]:
            if key in qf.index:
                rev_key = key
                break
        if not rev_key:
            return None

        recent = qf.loc[rev_key].iloc[:4].sum()
        prev = qf.loc[rev_key].iloc[4:8].sum()
        if prev and prev > 0:
            return ((recent - prev) / abs(prev)) * 100
        return None
    except Exception as e:
        logger.debug(f"매출 성장률 계산 실패: {e}")
        return None

# ============================================================
# 참고 정보 (점수화 X)
# ============================================================

def calc_dividend_growth(t_obj) -> dict:
    """최근 3년 배당 성장률 계산."""
    result = {"growth_rates": [], "cagr": None, "consecutive_growth": 0}
    try:
        divs = t_obj.dividends
        if divs is None or divs.empty:
            return result

        # 연도별 배당 합산 (현재 연도 제외 - 아직 완전하지 않음)
        divs.index = divs.index.tz_localize(None) if divs.index.tz else divs.index
        current_year = datetime.now().year
        divs = divs[divs.index.year < current_year]
        annual = divs.groupby(divs.index.year).sum()

        if len(annual) < 2:
            return result

        # 최근 4년치만 사용 (3년간 성장률 계산)
        annual = annual.iloc[-4:]
        years = list(annual.index)
        amounts = list(annual.values)

        # 연도 사이 gap 채우기 (배당 없던 연도를 0으로 채움)
        full_years = list(range(min(years), max(years) + 1))
        full_amounts = []
        for y in full_years:
            if y in years:
                full_amounts.append(amounts[years.index(y)])
            else:
                full_amounts.append(0.0)
        years = full_years
        amounts = full_amounts

        # 연도별 성장률 (직전 배당이 0이면 재개로 표시)
        for i in range(1, len(amounts)):
            if amounts[i-1] <= 0:
                if amounts[i] > 0:
                    result["growth_rates"].append((years[i], None))  # None = 재개
                continue
            rate = ((amounts[i] - amounts[i-1]) / amounts[i-1]) * 100
            result["growth_rates"].append((years[i], rate))

        # CAGR: 배당이 0인 연도 제외하고 계산
        valid_amounts = [(y, a) for y, a in zip(years, amounts) if a > 0]
        if len(valid_amounts) >= 2:
            first_year, first_amt = valid_amounts[0]
            last_year, last_amt = valid_amounts[-1]
            n = last_year - first_year
            if n > 0:
                result["cagr"] = ((last_amt / first_amt) ** (1 / n) - 1) * 100

        # 연속 성장 횟수 (재개 연도는 제외)
        consecutive = 0
        for _, rate in reversed(result["growth_rates"]):
            if rate is not None and rate > 0:
                consecutive += 1
            else:
                break
        result["consecutive_growth"] = consecutive

        return result
    except Exception as e:
        logger.debug(f"배당 성장률 계산 실패: {e}")
        return result

def calc_piotroski_fscore(t_obj, data: dict) -> tuple[int, list]:
    """Piotroski F-Score (0-9점) 계산. 우량주 판별 지표."""
    score = 0
    details = []
    try:
        af = t_obj.financials
        bs = t_obj.balance_sheet
        cf = t_obj.cashflow

        if af is None or af.empty or af.shape[1] < 2:
            return 0, ["재무 데이터 부족"]

        def safe_get(df, key, idx=0):
            if df is not None and not df.empty and key in df.index:
                val = df.loc[key].iloc[idx]
                return val if pd.notna(val) else None
            return None

        # === 수익성 (4개) ===
        # 1. ROA > 0
        ni = safe_get(af, "Net Income")
        ta = safe_get(bs, "Total Assets")
        if ni is not None and ta and ni > 0:
            score += 1
            details.append("✅ 순이익 흑자")
        else:
            details.append("❌ 순이익 적자")

        # 2. 영업현금흐름 > 0
        ocf = safe_get(cf, "Operating Cash Flow") or safe_get(cf, "Cash Flow From Continuing Operating Activities")
        if ocf and ocf > 0:
            score += 1
            details.append("✅ 영업현금흐름 양수")
        else:
            details.append("❌ 영업현금흐름 음수")

        # 3. ROA 개선 (전년 대비)
        ni_prev = safe_get(af, "Net Income", 1)
        ta_prev = safe_get(bs, "Total Assets", 1)
        if ni and ta and ni_prev and ta_prev:
            roa_now = ni / ta
            roa_prev = ni_prev / ta_prev
            if roa_now > roa_prev:
                score += 1
                details.append("✅ ROA 개선")
            else:
                details.append("❌ ROA 하락")

        # 4. 영업현금흐름 > 순이익 (이익 질 좋음)
        if ocf and ni and ocf > ni:
            score += 1
            details.append("✅ 영업현금흐름 > 순이익")
        else:
            details.append("❌ 영업현금흐름 < 순이익")

        # === 레버리지/유동성 (3개) ===
        # 5. 장기부채 감소
        ltd = safe_get(bs, "Long Term Debt")
        ltd_prev = safe_get(bs, "Long Term Debt", 1)
        if ltd is not None and ltd_prev is not None:
            if ltd < ltd_prev:
                score += 1
                details.append("✅ 장기부채 감소")
            else:
                details.append("❌ 장기부채 증가")

        # 6. 유동비율 개선 (Current Assets / Current Liabilities)
        ca = safe_get(bs, "Current Assets")
        cl = safe_get(bs, "Current Liabilities")
        ca_p = safe_get(bs, "Current Assets", 1)
        cl_p = safe_get(bs, "Current Liabilities", 1)
        if ca and cl and cl > 0 and ca_p and cl_p and cl_p > 0:
            cr_now = ca / cl
            cr_prev = ca_p / cl_p
            if cr_now > cr_prev:
                score += 1
                details.append("✅ 유동비율 개선")
            else:
                details.append("❌ 유동비율 하락")

        # 7. 신주발행 없음 (주식수 동일 or 감소)
        sh = safe_get(bs, "Ordinary Shares Number") or safe_get(bs, "Share Issued")
        sh_p = safe_get(bs, "Ordinary Shares Number", 1) or safe_get(bs, "Share Issued", 1)
        if sh and sh_p:
            if sh <= sh_p:
                score += 1
                details.append("✅ 신주발행 없음")
            else:
                details.append("❌ 주식수 증가 (신주발행)")

        # === 운영 효율 (2개) ===
        # 8. 매출총이익률 개선
        rev = safe_get(af, "Total Revenue")
        gp = safe_get(af, "Gross Profit")
        rev_p = safe_get(af, "Total Revenue", 1)
        gp_p = safe_get(af, "Gross Profit", 1)
        if rev and gp and rev_p and gp_p and rev > 0 and rev_p > 0:
            gm_now = gp / rev
            gm_prev = gp_p / rev_p
            if gm_now > gm_prev:
                score += 1
                details.append("✅ 매출총이익률 개선")
            else:
                details.append("❌ 매출총이익률 하락")

        # 9. 자산회전율 개선 (Revenue / Total Assets)
        if rev and ta and rev_p and ta_prev:
            atr_now = rev / ta
            atr_prev = rev_p / ta_prev
            if atr_now > atr_prev:
                score += 1
                details.append("✅ 자산회전율 개선")
            else:
                details.append("❌ 자산회전율 하락")

        return score, details
    except Exception as e:
        logger.debug(f"F-Score 계산 실패: {e}")
        return 0, ["계산 실패"]
