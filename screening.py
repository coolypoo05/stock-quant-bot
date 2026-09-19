"""스크리닝 유니버스/조건 파싱/빠른 조회"""

import io
import re
import pandas as pd
import requests
import yfinance as yf
from config import HEADERS, logger, notify_admin


# ============================================================
# 스크리닝 (종목 검색)
# ============================================================

SCREENING_UNIVERSE = []  # [{"code", "name", "suffix", "market"}]

ACTIVE_SCREENINGS = {}   # {chat_id: {"cancel": False}}

UNIVERSE_EXPECTED = {"KOSPI200": (150, 250), "KOSDAQ150": (100, 200), "SP500": (450, 550)}

def load_screening_universe():
    """코스피200 + 코스닥150(대형주) + S&P500 로딩."""
    SCREENING_UNIVERSE.clear()
    logger.info("스크리닝 유니버스 로딩 중...")

    # 1. 코스피200 (Wikipedia)
    try:
        url = "https://en.wikipedia.org/wiki/KOSPI_200"
        res = requests.get(url, headers=HEADERS, timeout=15)
        tables = pd.read_html(io.StringIO(res.text))
        for df in tables:
            code_col = None
            name_col = None
            for c in df.columns:
                c_str = str(c).lower()
                if any(k in c_str for k in ["ticker", "code", "symbol"]):
                    code_col = c
                elif any(k in c_str for k in ["name", "company"]):
                    name_col = c
            if code_col is None:
                continue
            count = 0
            for _, row in df.iterrows():
                code_clean = re.sub(r"\D", "", str(row[code_col])).zfill(6)[-6:]
                if not re.fullmatch(r"\d{6}", code_clean):
                    continue
                name = str(row[name_col]).strip() if name_col else code_clean
                SCREENING_UNIVERSE.append({
                    "code": code_clean, "name": name,
                    "suffix": ".KS", "market": "KOSPI200",
                })
                count += 1
            if count > 0:
                logger.info(f"KOSPI200 로딩: {count}개")
                break
    except Exception as e:
        logger.warning(f"KOSPI200 로딩 실패: {e}")

    # 2. 코스닥150: 네이버 모바일 API에서 시가총액 상위 150개 조회
    try:
        stocks = []
        for page in (1, 2):  # API 최대 pageSize=100
            res = requests.get(
                "https://m.stock.naver.com/api/stocks/marketValue/KOSDAQ",
                params={"page": page, "pageSize": 100}, headers=HEADERS, timeout=15,
            )
            res.raise_for_status()
            stocks += res.json()["stocks"]
        kosdaq_top = [
            {"code": s["itemCode"], "name": s["stockName"]}
            for s in stocks
            if re.fullmatch(r"\d{6}", s.get("itemCode", ""))
        ][:150]

        for item in kosdaq_top:
            SCREENING_UNIVERSE.append({
                "code": item["code"], "name": item["name"],
                "suffix": ".KQ", "market": "KOSDAQ150",
            })
        logger.info(f"KOSDAQ150 로딩: {len(kosdaq_top)}개 (네이버 시가총액 상위)")
    except Exception as e:
        logger.warning(f"KOSDAQ150 로딩 실패: {e}")

    # 3. S&P500 (Wikipedia)
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        res = requests.get(url, headers=HEADERS, timeout=15)
        df = pd.read_html(io.StringIO(res.text))[0]
        count = 0
        for _, row in df.iterrows():
            symbol = str(row.get("Symbol", "")).replace(".", "-")
            name = row.get("Security", "")
            if symbol and symbol != "nan":
                SCREENING_UNIVERSE.append({
                    "code": symbol, "name": name,
                    "suffix": "", "market": "SP500",
                })
                count += 1
        logger.info(f"S&P500 로딩: {count}개")
    except Exception as e:
        logger.error(f"S&P500 로딩 실패: {e}")

    logger.info(f"스크리닝 유니버스 로딩 완료: {len(SCREENING_UNIVERSE)}개")

    # 소스 구조가 바뀌면 조용히 0건이 되므로 기대 개수 범위를 벗어나면 경고
    counts = {}
    for it in SCREENING_UNIVERSE:
        counts[it["market"]] = counts.get(it["market"], 0) + 1
    for market, (lo, hi) in UNIVERSE_EXPECTED.items():
        n = counts.get(market, 0)
        if not lo <= n <= hi:
            msg = f"스크리닝 유니버스 이상 [{market}]: {n}개 (기대 {lo}~{hi})"
            logger.warning(msg)
            notify_admin(f"universe:{market}", f"⚠️ {msg}")

def parse_screen_conditions(text: str) -> list:
    """'PER<10 ROE>15' 같은 조건 파싱."""
    conditions = []
    metric_map = {
        # 밸류
        "PER": "pe_ratio",
        "FORWARDPER": "forward_pe",
        "PBR": "pb_ratio",
        "PSR": "ps_ratio",
        "EVEBITDA": "ev_ebitda",
        "PEG": "peg_ratio",
        # 퀄리티
        "ROE": "roe",
        "ROA": "roa",
        "OPMARGIN": "operating_margin",
        "NETMARGIN": "net_margin",
        "GROSSMARGIN": "gross_margin",
        "DEBT": "debt_to_equity",
        "CURRENTRATIO": "current_ratio",
        "INTEREST": "interest_coverage",
        # 배당
        "DIV": "dividend_yield",
        "PAYOUT": "payout_ratio",
        # 성장
        "REVGROWTH": "revenue_growth",
        "EPSGROWTH": "earnings_growth",
        # 규모
        "MARKETCAP": "market_cap_bil",   # 한국: 억원, 미국: 백만달러
        "PRICE": "price",
    }
    pattern = re.compile(r"([A-Z]+)\s*(<=|>=|<|>|=)\s*(-?[\d.]+)", re.IGNORECASE)
    for m in pattern.finditer(text):
        metric = m.group(1).upper().replace("/", "")
        op = m.group(2)
        val = float(m.group(3))
        if metric in metric_map:
            conditions.append({
                "key": metric_map[metric],
                "op": op,
                "val": val,
                "raw": metric,
            })
    # 특수 키워드 파싱 (두 지표 간 비교)
    special_keywords = {
        "IMPROVING": {"type": "compare", "key1": "pe_ratio", "key2": "forward_pe", "op": ">",
                      "desc": "PER > Forward PER (실적 개선 기대)"},
        "DETERIORATING": {"type": "compare", "key1": "pe_ratio", "key2": "forward_pe", "op": "<",
                          "desc": "PER < Forward PER (실적 둔화 우려)"},
        "PROFITABLE": {"type": "positive", "key": "eps", "desc": "EPS 흑자"},
        "DIVIDEND": {"type": "positive", "key": "dividend_yield", "desc": "배당 지급 종목"},
    }
    for keyword, spec in special_keywords.items():
        if re.search(rf"\b{keyword}\b", text, re.IGNORECASE):
            conditions.append({"type": spec["type"], **spec, "raw": keyword})

    return conditions

def check_condition(data: dict, cond: dict) -> bool:
    """조건 1개 체크."""
    cond_type = cond.get("type", "normal")

    # 두 지표 간 비교 (예: PER > Forward PER)
    if cond_type == "compare":
        v1 = data.get(cond["key1"])
        v2 = data.get(cond["key2"])
        if v1 is None or v2 is None or v1 <= 0 or v2 <= 0:
            return False
        op = cond["op"]
        if op == ">":
            return v1 > v2
        elif op == "<":
            return v1 < v2
        elif op == ">=":
            return v1 >= v2
        elif op == "<=":
            return v1 <= v2
        return False

    # 양수 여부 체크
    if cond_type == "positive":
        val = data.get(cond["key"])
        return val is not None and val > 0

    # 일반 숫자 조건
    val = data.get(cond["key"])
    if val is None:
        return False
    op = cond["op"]
    target = cond["val"]
    # 부동소수점 오차 방지: 소수점 4자리로 반올림
    val = round(float(val), 4)
    if op == "<":
        return val < target
    elif op == "<=":
        return val <= target
    elif op == ">":
        return val > target
    elif op == ">=":
        return val >= target
    elif op == "=":
        return abs(val - target) < 0.01
    return False

def fetch_stock_quick(item: dict) -> dict | None:
    """스크리닝용 빠른 데이터 수집 (yfinance t.info만 사용)."""
    try:
        ticker_str = item["code"] + item["suffix"]
        t = yf.Ticker(ticker_str)
        info = t.info
        if not info or (info.get("regularMarketPrice") is None and info.get("currentPrice") is None):
            return None

        def pct(val):
            """소수 → % 변환 (0.18 → 18.0), 이미 %면 그대로."""
            if val is None:
                return None
            return val * 100 if abs(val) < 10 else val

        def div_pct(val):
            """배당수익률: yfinance가 소수(0.035) 또는 %(3.5) 혼용 반환."""
            if val is None:
                return None
            if val <= 0:
                return None
            if val < 0.2:
                return round(val * 100, 2)  # 소수 → %
            elif val <= 100:
                return round(val, 2)  # 이미 %
            return None  # 100% 초과는 오류

        price = info.get("currentPrice") or info.get("regularMarketPrice")
        market_cap = info.get("marketCap")
        # 한국: 억원, 미국: 백만달러로 통일
        currency = info.get("currency", "USD")
        if market_cap:
            if currency == "KRW":
                market_cap_bil = market_cap / 1e8  # 억원
            else:
                market_cap_bil = market_cap / 1e6  # 백만달러

        # PEG 직접 계산
        pe = info.get("trailingPE")
        earnings_growth = info.get("earningsGrowth")
        peg = None
        if pe and pe > 0 and earnings_growth and earnings_growth > 0:
            peg = pe / (earnings_growth * 100)

        return {
            "code": item["code"],
            "name": item["name"],
            "market": item["market"],
            # 가격/규모
            "price": price,
            "market_cap_bil": market_cap_bil if market_cap else None,
            # 밸류
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "pb_ratio": info.get("priceToBook"),
            "ps_ratio": info.get("priceToSalesTrailing12Months"),
            "ev_ebitda": None,
            "peg_ratio": peg,
            # 퀄리티 (% 변환)
            "roe": pct(info.get("returnOnEquity")),
            "roa": pct(info.get("returnOnAssets")),
            "operating_margin": pct(info.get("operatingMargins")),
            "net_margin": pct(info.get("profitMargins")),
            "gross_margin": pct(info.get("grossMargins")),
            "debt_to_equity": info.get("debtToEquity"),
            "current_ratio": info.get("currentRatio"),
            "interest_coverage": None,  # t.info에 없어서 생략
            # 배당 (yfinance는 항상 소수로 반환 → 무조건 × 100)
            "dividend_yield": div_pct(info.get("dividendYield")),
            "payout_ratio": div_pct(info.get("payoutRatio")),
            # 성장 (% 변환)
            "revenue_growth": pct(info.get("revenueGrowth")),
            "earnings_growth": pct(info.get("earningsGrowth")),
            # 업종 (상대평가용)
            "sector": info.get("sector", "") or "",
            "industry": info.get("industry", "") or "",
        }
    except Exception:
        return None
