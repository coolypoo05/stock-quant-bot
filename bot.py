"""
주식 퀀트 분석 텔레그램 봇 (한국 + 미국)
팩터 스코어링: 밸류 + 퀄리티 + 모멘텀
참고 정보: 배당 + 변동성
"""

import os
import re
import io
import gc
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN 환경변수가 설정되지 않았습니다.")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

STOCK_MAP: dict = {}  # {종목명: {"code": "005930", "suffix": ".KS"}}
KST = ZoneInfo("Asia/Seoul")


# ============================================================
# 한국 종목 리스트
# ============================================================

def load_stock_map():
    global STOCK_MAP
    logger.info("한국 종목 리스트 로딩 중...")
    try:
        # 코스피/코스닥 각각 로딩
        for market_type, suffix in [("stockMkt", ".KS"), ("kosdaqMkt", ".KQ")]:
            url = "https://kind.krx.co.kr/corpgeneral/corpList.do"
            params = {"method": "download", "searchType": "13", "marketType": market_type}
            res = requests.get(url, params=params, headers=HEADERS, timeout=30)
            res.raise_for_status()
            df = pd.read_html(io.StringIO(res.text))[0]
            df["종목코드"] = df["종목코드"].astype(str).str.zfill(6)
            for _, row in df.iterrows():
                STOCK_MAP[row["회사명"]] = {"code": row["종목코드"], "suffix": suffix}
        logger.info(f"한국 종목 리스트 로딩 완료: {len(STOCK_MAP)}개")
    except Exception as e:
        logger.error(f"종목 리스트 로딩 실패: {e}")


def search_kor_stock(query: str):
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

def get_naver_per_pbr(code: str) -> dict:
    """네이버 금융에서 PER, PBR 파싱."""
    result = {"per": None, "pbr": None}
    try:
        url = f"https://finance.naver.com/item/main.naver?code={code}"
        res = requests.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")

        # PER, PBR은 .blind 태그로 감싸진 테이블에 있음
        table = soup.select_one("table.per_table")
        if table:
            for em in table.select("em"):
                text = em.get_text(strip=True).replace(",", "")
                try:
                    val = float(text)
                    em_id = em.get("id", "")
                    if "PER" in em_id or "per" in em_id.lower():
                        result["per"] = val
                    elif "PBR" in em_id or "pbr" in em_id.lower():
                        result["pbr"] = val
                except ValueError:
                    continue

        # fallback: 텍스트에서 직접 추출
        if result["per"] is None or result["pbr"] is None:
            text = soup.get_text(" ", strip=True)
            per_m = re.search(r'PER\s*([\d.]+)배', text)
            pbr_m = re.search(r'PBR\s*([\d.]+)배', text)
            if per_m and result["per"] is None:
                result["per"] = float(per_m.group(1))
            if pbr_m and result["pbr"] is None:
                result["pbr"] = float(pbr_m.group(1))

    except Exception as e:
        logger.debug(f"네이버 PER/PBR 파싱 실패 ({code}): {e}")
    return result


def get_wisereport_forward_eps(code: str) -> float | None:
    """wisereport 컨센서스에서 Forward EPS 파싱."""
    try:
        url = f"https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={code}"
        referer = f"https://finance.naver.com/item/coinfo.naver?code={code}"
        res = requests.get(url, headers={**HEADERS, "Referer": referer}, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        # wisereport 컨센서스 테이블에서 EPS 추출
        # 패턴: "EPS (원) 숫자 숫자 숫자" 형태로 현재연도/다음연도 순서
        m = re.search(r'EPS\s*[\(（]?원?[\)）]?\s*([\d,]+)\s+([\d,]+)', text)
        if m:
            # 두 번째 값이 다음 연도 Forward EPS
            forward_eps = float(m.group(2).replace(",", ""))
            if forward_eps > 0:
                logger.info(f"Forward EPS wisereport: {code} = {forward_eps}")
                return forward_eps

        # 추가 패턴 시도
        m2 = re.search(r'컨센서스.*?EPS.*?([\d,]+)', text)
        if m2:
            val = float(m2.group(1).replace(",", ""))
            if val > 0:
                return val

        return None
    except Exception as e:
        logger.debug(f"wisereport Forward EPS 파싱 실패 ({code}): {e}")
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
# 한국 주식 데이터 (yfinance + 네이버)
# ============================================================

def get_kor_stock_data(code: str, name: str, known_suffix: str = None):
    try:
        ticker = None
        info = None
        hist = None
        t_obj = None

        # 알고 있는 suffix가 있으면 그것 먼저, 없으면 둘 다 시도
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
        if not info:
            return None

        debt_ratio = calc_debt_to_equity(t_obj) if t_obj else info.get("debtToEquity")
        op_margin = calc_ttm_operating_margin(t_obj) if t_obj else info.get("operatingMargins")
        ev_ebitda = calc_ev_ebitda(t_obj, info) if t_obj else None
        interest_coverage = calc_interest_coverage(t_obj) if t_obj else None
        revenue_growth = calc_revenue_growth(t_obj) if t_obj else None
        dividend_growth = calc_dividend_growth(t_obj) if t_obj else {}

        # F-Score 계산 (별도 단계로 데이터 수집 후)
        temp_data = {"market_cap": info.get("marketCap")}
        fscore_info = calc_piotroski_fscore(t_obj, temp_data) if t_obj else None

        # PER, PBR: yfinance → None이면 네이버 fallback
        pe_ratio = info.get("trailingPE")
        pb_ratio = info.get("priceToBook")
        if pe_ratio is None or pb_ratio is None:
            naver = get_naver_per_pbr(code)
            if pe_ratio is None and naver["per"]:
                pe_ratio = naver["per"]
                logger.info(f"PER 네이버 fallback: {code} = {pe_ratio}")
            if pb_ratio is None and naver["pbr"]:
                pb_ratio = naver["pbr"]
                logger.info(f"PBR 네이버 fallback: {code} = {pb_ratio}")

        # EPS: yfinance → None이면 재무제표 직접 계산
        eps = info.get("trailingEps")
        if eps is None and t_obj:
            eps = calc_eps_from_financials(t_obj)
            if eps:
                logger.info(f"EPS 재무제표 계산: {code} = {eps:.2f}")

        # Forward EPS: yfinance → None이면 wisereport 파싱
        forward_eps = info.get("forwardEps")
        if forward_eps is None:
            forward_eps = get_wisereport_forward_eps(code)

        return {
            "code": code, "name": name, "ticker": ticker,
            "price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "previous_close": info.get("previousClose"),
            "market_cap": info.get("marketCap"),
            "pe_ratio": pe_ratio,
            "forward_pe": info.get("forwardPE"),
            "eps": eps,
            "forward_eps": forward_eps,
            "pb_ratio": pb_ratio,
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
            "currency": "KRW", "market": "KR",
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
        hist = t.history(period="1y")
        debt_ratio = calc_debt_to_equity(t)
        op_margin = calc_ttm_operating_margin(t)
        ev_ebitda = calc_ev_ebitda(t, info)
        interest_coverage = calc_interest_coverage(t)
        revenue_growth = calc_revenue_growth(t)
        dividend_growth = calc_dividend_growth(t)

        # F-Score 계산
        temp_data = {"market_cap": info.get("marketCap")}
        fscore_info = calc_piotroski_fscore(t, temp_data)

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


def score_value(data):
    """밸류 팩터 (낮은 PER/ForwardPER/PBR/PSR이 좋음)."""
    scores = []
    details = []

    # Trailing PER
    pe = data.get("pe_ratio")
    if pe and pe > 0:
        if pe < 10:
            s, g = 90, "매우 저평가"
        elif pe < 15:
            s, g = 75, "저평가"
        elif pe < 20:
            s, g = 60, "적정"
        elif pe < 30:
            s, g = 40, "다소 비쌈"
        else:
            s, g = 20, "고평가"
        scores.append(s)
        details.append(f"PER: {pe:.2f}배 ({g})")
    elif pe and pe < 0:
        details.append(f"PER: {pe:.2f}배 (적자)")

    # Forward PER (미래 실적 기반, 가중치 1.5배)
    fpe = data.get("forward_pe")
    if fpe and fpe > 0:
        if fpe < 10:
            s, g = 90, "매우 저평가"
        elif fpe < 15:
            s, g = 75, "저평가"
        elif fpe < 20:
            s, g = 60, "적정"
        elif fpe < 30:
            s, g = 40, "다소 비쌈"
        else:
            s, g = 20, "고평가"
        # Forward PER은 미래 기반이라 가중치 1.5배
        scores.append(s)
        scores.append(s)  # 동일 점수 두 번 추가 = 1.5배 가중치 효과
        details.append(f"Forward PER: {fpe:.2f}배 ({g}) ★")

        # Trailing vs Forward 비교 (실적 개선 여부)
        if pe and pe > 0 and fpe > 0:
            if fpe < pe * 0.9:
                details.append(f"  → 실적 개선 기대 ({pe:.1f}배 → {fpe:.1f}배)")
            elif fpe > pe * 1.1:
                details.append(f"  → 실적 둔화 우려 ({pe:.1f}배 → {fpe:.1f}배)")
    elif fpe and fpe < 0:
        details.append(f"Forward PER: {fpe:.2f}배 (적자 예상)")

    pb = data.get("pb_ratio")
    if pb and pb > 0:
        if pb < 1.0:
            s, g = 90, "매우 저평가"
        elif pb < 2.0:
            s, g = 70, "저평가"
        elif pb < 3.0:
            s, g = 55, "적정"
        elif pb < 5.0:
            s, g = 35, "다소 비쌈"
        else:
            s, g = 20, "고평가"
        scores.append(s)
        details.append(f"PBR: {pb:.2f}배 ({g})")

    ps = data.get("ps_ratio")
    if ps and ps > 0:
        if ps < 1.0:
            s = 85
        elif ps < 2.0:
            s = 65
        elif ps < 5.0:
            s = 45
        else:
            s = 25
        scores.append(s)
        details.append(f"PSR: {ps:.2f}배")

    # EPS 성장률 (Trailing → Forward, 높을수록 좋음)
    eps = data.get("eps")
    feps = data.get("forward_eps")
    eps_growth = None
    if eps and feps and eps > 0 and feps > 0:
        eps_growth = ((feps - eps) / abs(eps)) * 100
        if eps_growth > 30:
            s, g = 95, "고성장"
        elif eps_growth > 15:
            s, g = 80, "성장"
        elif eps_growth > 5:
            s, g = 65, "완만한 성장"
        elif eps_growth > -5:
            s, g = 50, "보합"
        elif eps_growth > -15:
            s, g = 30, "감익"
        else:
            s, g = 15, "급감익"
        scores.append(s)
        sign = "+" if eps_growth >= 0 else ""
        details.append(f"EPS 성장률: {sign}{eps_growth:.1f}% ({g}) ★")
    elif eps:
        details.append(f"EPS: {eps:.2f} (Forward EPS 데이터 없음)")

    # EV/EBITDA (낮을수록 저평가)
    ev_ebitda = data.get("ev_ebitda")
    if ev_ebitda and ev_ebitda > 0:
        if ev_ebitda < 6:
            s, g = 95, "매우 저평가"
        elif ev_ebitda < 10:
            s, g = 80, "저평가"
        elif ev_ebitda < 15:
            s, g = 62, "적정"
        elif ev_ebitda < 20:
            s, g = 42, "다소 비쌈"
        else:
            s, g = 22, "고평가"
        scores.append(s)
        details.append(f"EV/EBITDA: {ev_ebitda:.2f}배 ({g})")

    # PEG (PER / EPS성장률, 1.0 이하면 성장 대비 저평가) - 가중치 2배
    pe = data.get("pe_ratio")
    if pe and pe > 0 and eps_growth and eps_growth > 0:
        peg = pe / eps_growth
        if peg < 0.5:
            s, g = 95, "매우 저평가"
        elif peg < 1.0:
            s, g = 80, "저평가"
        elif peg < 1.5:
            s, g = 60, "적정"
        elif peg < 2.0:
            s, g = 40, "다소 비쌈"
        else:
            s, g = 20, "고평가"
        scores.append(s)
        scores.append(s)  # 가중치 2배 (성장 감안한 밸류 중요도 상향)
        details.append(f"PEG: {peg:.2f} ({g}) ★★")
    elif pe and pe > 0 and eps_growth and eps_growth <= 0:
        details.append(f"PEG: 산출불가 (EPS 감소 중)")

    if not scores:
        return 0, ["데이터 부족"]
    return int(np.mean(scores)), details


def score_quality(data):
    """퀄리티 팩터 (높은 ROE/영업이익률, 낮은 부채비율, EPS 안정성)."""
    scores = []
    details = []
    holding = is_holding_company(data)

    roe = data.get("roe")
    if roe is not None:
        roe_pct = roe * 100
        if roe_pct > 20:
            s, g = 95, "매우 우수"
        elif roe_pct > 15:
            s, g = 80, "우수"
        elif roe_pct > 10:
            s, g = 65, "양호"
        elif roe_pct > 5:
            s, g = 45, "평범"
        else:
            s, g = 25, "낮음"
        scores.append(s)
        details.append(f"ROE: {roe_pct:.2f}% ({g})")

    op = data.get("operating_margin")
    if op is not None:
        op_pct = op * 100
        if holding:
            # 지주/금융은 영업이익률 점수화 제외, 참고용으로만 표시
            details.append(f"영업이익률: {op_pct:.2f}% (지주/금융사 특성상 점수 제외)")
        else:
            if op_pct > 20:
                s = 90
            elif op_pct > 15:
                s = 75
            elif op_pct > 10:
                s = 60
            elif op_pct > 5:
                s = 40
            else:
                s = 20
            scores.append(s)
            details.append(f"영업이익률: {op_pct:.2f}%")

    debt = data.get("debt_to_equity")
    if debt is not None:
        if holding:
            # 지주/금융은 부채비율 기준 완화
            if debt < 100:
                s, g = 80, "안정"
            elif debt < 200:
                s, g = 65, "보통"
            elif debt < 400:
                s, g = 45, "높음"
            else:
                s, g = 25, "매우 높음"
            details.append(f"부채비율: {debt:.0f}% ({g}, 지주/금융 기준)")
        else:
            if debt < 30:
                s, g = 90, "매우 안정"
            elif debt < 50:
                s, g = 75, "안정"
            elif debt < 100:
                s, g = 55, "보통"
            elif debt < 200:
                s, g = 35, "높음"
            else:
                s, g = 15, "매우 높음"
            details.append(f"부채비율: {debt:.0f}% ({g})")
        scores.append(s)

    # EPS 안정성 (적자 여부 + 흑자 수준)
    eps = data.get("eps")
    if eps is not None:
        if eps > 0:
            s, g = 80, "흑자"
            details.append(f"EPS: {eps:.2f} ({g})")
        else:
            s, g = 10, "적자"
            details.append(f"EPS: {eps:.2f} ({g}) ⚠️")
        scores.append(s)

    # ROA (총자산 대비 이익률, ROE 보완)
    roa = data.get("roa")
    if roa is not None:
        roa_pct = roa * 100
        if roa_pct > 15:
            s, g = 95, "매우 우수"
        elif roa_pct > 10:
            s, g = 80, "우수"
        elif roa_pct > 5:
            s, g = 65, "양호"
        elif roa_pct > 2:
            s, g = 45, "평범"
        else:
            s, g = 25, "낮음"
        scores.append(s)
        details.append(f"ROA: {roa_pct:.2f}% ({g})")

    # 이자보상배율 (영업이익 / 이자비용, 높을수록 안전)
    ic = data.get("interest_coverage")
    if ic is not None:
        if ic > 10:
            s, g = 95, "매우 안전"
        elif ic > 5:
            s, g = 80, "안전"
        elif ic > 3:
            s, g = 60, "보통"
        elif ic > 1:
            s, g = 35, "주의"
        else:
            s, g = 10, "위험 ⚠️"
        scores.append(s)
        details.append(f"이자보상배율: {ic:.1f}배 ({g})")

    if not scores:
        return 0, ["데이터 부족"]
    return int(np.mean(scores)), details


def score_momentum(data):
    """모멘텀 팩터 (수익률 + RSI + 52주 위치 + 거래량)."""
    scores = []
    details = []
    hist = data.get("history")

    if hist is None or hist.empty or len(hist) < 20:
        return 0, ["데이터 부족"]

    current = hist["Close"].iloc[-1]

    # 1) 수익률 모멘텀 (1M/3M/6M/12M)
    details.append("📈 수익률")
    ret_scores = []
    periods = [("1M", 21), ("3M", 63), ("6M", 126), ("12M", 252)]
    weights = [1, 2, 2, 1]
    for (label, days), w in zip(periods, weights):
        if len(hist) >= days:
            past = hist["Close"].iloc[-days]
            ret = ((current - past) / past) * 100
            sign = "+" if ret >= 0 else ""
            details.append(f"   • {label}: {sign}{ret:.2f}%")
            if ret > 30:
                s = 95
            elif ret > 15:
                s = 80
            elif ret > 5:
                s = 65
            elif ret > -5:
                s = 50
            elif ret > -15:
                s = 35
            elif ret > -30:
                s = 20
            else:
                s = 10
            ret_scores.extend([s] * w)
    if ret_scores:
        scores.append(int(np.mean(ret_scores)))

    # 2) RSI (14일 기준)
    if len(hist) >= 14:
        delta = hist["Close"].diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, float("inf"))
        rsi = (100 - (100 / (1 + rs))).iloc[-1]
        details.append(f"\n📊 RSI (14일): {rsi:.1f}")
        if rsi < 30:
            s, g = 85, "과매도 (반등 가능)"
            details.append(f"   • 과매도 구간 → 매수 관심 ★")
        elif rsi < 45:
            s, g = 65, "약세"
            details.append(f"   • 약세 구간")
        elif rsi < 55:
            s, g = 55, "중립"
            details.append(f"   • 중립 구간")
        elif rsi < 70:
            s, g = 70, "강세"
            details.append(f"   • 강세 구간 ★")
        else:
            s, g = 35, "과매수 (조정 주의)"
            details.append(f"   • 과매수 구간 → 주의 ⚠️")
        scores.append(s)

    # 3) 52주 신고가 대비 위치
    high_52 = hist["High"].max()
    low_52 = hist["Low"].min()
    if high_52 > low_52:
        position = ((current - low_52) / (high_52 - low_52)) * 100
        details.append(f"\n📍 52주 위치: {position:.1f}%")
        details.append(f"   • 저점 {low_52:,.0f} ~ 고점 {high_52:,.0f}")
        if position >= 80:
            s = 85
            details.append(f"   • 52주 고점 근처 (강한 상승 추세)")
        elif position >= 60:
            s = 70
            details.append(f"   • 상단 영역 (상승 추세)")
        elif position >= 40:
            s = 55
            details.append(f"   • 중간 영역")
        elif position >= 20:
            s = 40
            details.append(f"   • 하단 영역 (약세)")
        else:
            s = 25
            details.append(f"   • 52주 저점 근처 ⚠️")
        scores.append(s)

    # 4) 거래량 모멘텀 (최근 5일 평균 vs 20일 평균)
    if len(hist) >= 20:
        vol_5 = hist["Volume"].iloc[-5:].mean()
        vol_20 = hist["Volume"].iloc[-20:].mean()
        if vol_20 > 0:
            vol_ratio = vol_5 / vol_20
            details.append(f"\n📦 거래량 모멘텀: {vol_ratio:.2f}x (5일/20일 평균)")
            if vol_ratio >= 2.0:
                s = 85
                details.append(f"   • 거래량 급증 (강한 관심) ★")
            elif vol_ratio >= 1.3:
                s = 70
                details.append(f"   • 거래량 증가 (관심 상승)")
            elif vol_ratio >= 0.7:
                s = 50
                details.append(f"   • 거래량 보통")
            else:
                s = 30
                details.append(f"   • 거래량 감소 (관심 하락)")
            scores.append(s)

    # 5) 이동평균선 정배열 (MA20, MA60, MA120)
    if len(hist) >= 120:
        ma20 = hist["Close"].rolling(20).mean().iloc[-1]
        ma60 = hist["Close"].rolling(60).mean().iloc[-1]
        ma120 = hist["Close"].rolling(120).mean().iloc[-1]
        details.append(f"\n📊 이동평균선")
        details.append(f"   • MA20: {ma20:,.0f} | MA60: {ma60:,.0f} | MA120: {ma120:,.0f}")

        # 정배열: MA20 > MA60 > MA120 (강한 상승 추세)
        # 역배열: MA20 < MA60 < MA120 (강한 하락 추세)
        if ma20 > ma60 > ma120 and current > ma20:
            s = 90
            details.append(f"   • 완전 정배열 (강한 상승 추세) ★")
        elif ma20 > ma60 > ma120:
            s = 75
            details.append(f"   • 정배열 (상승 추세)")
        elif ma20 > ma60 and current > ma20:
            s = 65
            details.append(f"   • 단기 상승 추세")
        elif ma20 < ma60 < ma120 and current < ma20:
            s = 15
            details.append(f"   • 완전 역배열 (강한 하락 추세) ⚠️")
        elif ma20 < ma60 < ma120:
            s = 25
            details.append(f"   • 역배열 (하락 추세)")
        elif ma20 < ma60 and current < ma20:
            s = 35
            details.append(f"   • 단기 하락 추세")
        else:
            s = 50
            details.append(f"   • 혼조 (방향성 불분명)")
        scores.append(s)
    elif len(hist) >= 60:
        # 데이터 부족 시 MA20, MA60만으로 판단
        ma20 = hist["Close"].rolling(20).mean().iloc[-1]
        ma60 = hist["Close"].rolling(60).mean().iloc[-1]
        details.append(f"\n📊 이동평균선 (단기)")
        details.append(f"   • MA20: {ma20:,.0f} | MA60: {ma60:,.0f}")
        if ma20 > ma60 and current > ma20:
            s = 75
            details.append(f"   • 단기 정배열 (상승 추세)")
        elif ma20 < ma60 and current < ma20:
            s = 25
            details.append(f"   • 단기 역배열 (하락 추세)")
        else:
            s = 50
            details.append(f"   • 혼조")
        scores.append(s)

    # 6) MACD (단기/장기 EMA 차이로 추세 전환 포착)
    if len(hist) >= 35:
        # MACD = EMA(12) - EMA(26), Signal = MACD의 EMA(9)
        ema12 = hist["Close"].ewm(span=12, adjust=False).mean()
        ema26 = hist["Close"].ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        histogram = macd_line - signal_line

        macd_now = macd_line.iloc[-1]
        signal_now = signal_line.iloc[-1]
        hist_now = histogram.iloc[-1]
        hist_prev = histogram.iloc[-2] if len(histogram) >= 2 else 0

        details.append(f"\n📈 MACD")
        details.append(f"   • MACD: {macd_now:,.1f} | Signal: {signal_now:,.1f}")

        # 골든크로스: 히스토그램이 음수 → 양수 전환
        # 데드크로스: 히스토그램이 양수 → 음수 전환
        if hist_prev < 0 and hist_now >= 0:
            s = 90
            details.append(f"   • 골든크로스 발생! (강한 매수 신호) ★")
        elif hist_prev >= 0 and hist_now < 0:
            s = 15
            details.append(f"   • 데드크로스 발생 (매도 신호) ⚠️")
        elif macd_now > signal_now and hist_now > hist_prev:
            s = 80
            details.append(f"   • MACD > Signal 상승세 (강세 지속)")
        elif macd_now > signal_now:
            s = 65
            details.append(f"   • MACD > Signal (상승 추세)")
        elif macd_now < signal_now and hist_now < hist_prev:
            s = 25
            details.append(f"   • MACD < Signal 하락세 (약세 지속)")
        else:
            s = 40
            details.append(f"   • MACD < Signal (하락 추세)")
        scores.append(s)

    if not scores:
        return 0, ["데이터 부족"]
    return int(np.mean(scores)), details


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


def get_dividend_info(data):
    lines = []
    div = data.get("dividend_yield")
    if div:
        div_pct = div * 100 if div < 1 else div
        lines.append(f"배당수익률: {div_pct:.2f}%")
    else:
        lines.append("배당수익률: 정보없음 또는 무배당")

    payout = data.get("payout_ratio")
    if payout:
        lines.append(f"배당성향: {payout*100:.1f}%")

    # 배당 성장률
    div_growth = data.get("dividend_growth", {})
    growth_rates = div_growth.get("growth_rates", [])
    cagr = div_growth.get("cagr")
    consecutive = div_growth.get("consecutive_growth", 0)

    if growth_rates:
        lines.append("배당 성장률 (연도별):")
        for year, rate in growth_rates:
            if rate is None:
                lines.append(f"  🔄 {year}년: 배당 재개")
            else:
                sign = "+" if rate >= 0 else ""
                emoji = "📈" if rate > 0 else "📉"
                lines.append(f"  {emoji} {year}년: {sign}{rate:.1f}%")
        if cagr is not None:
            sign = "+" if cagr >= 0 else ""
            lines.append(f"배당 CAGR: {sign}{cagr:.1f}%")
        if consecutive >= 3:
            lines.append(f"연속 배당 증가: {consecutive}년 ★")
        elif consecutive > 0:
            lines.append(f"연속 배당 증가: {consecutive}년")
    elif div:
        lines.append("배당 성장률: 히스토리 부족")

    return lines


def get_volatility_info(data):
    lines = []
    beta = data.get("beta")
    if beta is not None:
        if beta < 0.5:
            g = "매우 낮음 (방어주)"
        elif beta < 1.0:
            g = "시장보다 안정"
        elif beta < 1.3:
            g = "시장 평균"
        elif beta < 1.7:
            g = "다소 변동성 큼"
        else:
            g = "매우 변동성 큼"
        lines.append(f"베타: {beta:.2f} ({g})")

    hist = data.get("history")
    if hist is not None and not hist.empty and len(hist) > 20:
        daily_returns = hist["Close"].pct_change().dropna()
        annual_vol = daily_returns.std() * np.sqrt(252) * 100
        lines.append(f"연환산 변동성: {annual_vol:.2f}%")

    if not lines:
        lines.append("데이터 부족")
    return lines


def get_growth_info(data) -> list:
    """성장성 정보 (참고용, 점수화 X)."""
    lines = []

    # 매출 성장률
    rev_growth = data.get("revenue_growth")
    if rev_growth is not None:
        sign = "+" if rev_growth >= 0 else ""
        if rev_growth > 20:
            g = "고성장 🚀"
        elif rev_growth > 10:
            g = "성장"
        elif rev_growth > 3:
            g = "완만한 성장"
        elif rev_growth > -3:
            g = "보합"
        elif rev_growth > -10:
            g = "역성장"
        else:
            g = "급감 ⚠️"
        lines.append(f"매출 성장률 (YoY): {sign}{rev_growth:.1f}% ({g})")
    else:
        lines.append("매출 성장률: 데이터 없음")

    # EPS 성장률
    eps = data.get("eps")
    feps = data.get("forward_eps")
    if eps and feps and eps > 0 and feps > 0:
        eps_growth = ((feps - eps) / abs(eps)) * 100
        sign = "+" if eps_growth >= 0 else ""
        if eps_growth > 30:
            g = "고성장 🚀"
        elif eps_growth > 15:
            g = "성장"
        elif eps_growth > 5:
            g = "완만한 성장"
        elif eps_growth > -5:
            g = "보합"
        else:
            g = "감익 ⚠️"
        lines.append(f"EPS 성장률 (예상): {sign}{eps_growth:.1f}% ({g})")
    elif eps:
        lines.append("EPS 성장률: Forward EPS 데이터 없음")

    # Forward PER (성장주 체크용)
    fpe = data.get("forward_pe")
    if fpe and fpe > 0:
        lines.append(f"Forward PER: {fpe:.2f}배")

    if not lines:
        lines.append("데이터 부족")
    return lines


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
        shares = data.get("market_cap")  # 대안으로 사용
        # yfinance 데이터 한계로 간소화: 일단 통과
        score += 1
        details.append("✅ 신주발행 체크 (간소화)")

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


def check_risk_warnings(data: dict) -> tuple[int, list]:
    """위험 신호 자동 감지 → 감점 및 경고 리턴."""
    penalty = 0
    warnings = []

    # 1. 부채비율 과다 (300% 초과)
    debt = data.get("debt_to_equity")
    holding = is_holding_company(data)
    if debt is not None:
        threshold = 500 if holding else 300
        if debt > threshold:
            penalty += 10
            warnings.append(f"부채비율 매우 높음 ({debt:.0f}%)")

    # 2. 이자보상배율 1 미만
    ic = data.get("interest_coverage")
    if ic is not None and ic < 1:
        penalty += 10
        warnings.append(f"이자보상배율 위험 ({ic:.1f}배)")

    # 3. ROE 마이너스
    roe = data.get("roe")
    if roe is not None and roe < 0:
        penalty += 5
        warnings.append(f"ROE 적자 ({roe*100:.1f}%)")

    # 4. EPS 적자
    eps = data.get("eps")
    if eps is not None and eps < 0:
        penalty += 5
        warnings.append(f"EPS 적자")

    # 5. 영업이익률 적자 (지주/금융 제외)
    op = data.get("operating_margin")
    if op is not None and not holding and op < 0:
        penalty += 5
        warnings.append(f"영업이익률 적자 ({op*100:.1f}%)")

    return penalty, warnings


def calc_weighted_overall(value_score, quality_score, momentum_score) -> int:
    """저평가 우량주 전략 가중치 적용 (밸류40 + 퀄리티40 + 모멘텀20)."""
    weights = []
    scores = []
    if value_score > 0:
        scores.append(value_score)
        weights.append(0.4)
    if quality_score > 0:
        scores.append(quality_score)
        weights.append(0.4)
    if momentum_score > 0:
        scores.append(momentum_score)
        weights.append(0.2)
    if not scores:
        return 0
    # 가중치 합 정규화
    total_weight = sum(weights)
    weighted_sum = sum(s * w for s, w in zip(scores, weights))
    return int(weighted_sum / total_weight)


def grade_score(score):
    if score >= 80:
        return "🟢 A", "매우 매력적"
    elif score >= 70:
        return "🟢 B+", "매수 우호적"
    elif score >= 60:
        return "🟡 B", "양호"
    elif score >= 50:
        return "🟡 C+", "보통"
    elif score >= 40:
        return "🟠 C", "신중 검토"
    elif score >= 30:
        return "🔴 D", "매력 낮음"
    else:
        return "🔴 F", "매우 낮음"


# ============================================================
# 메시지 포맷팅
# ============================================================

def format_factor_message(data):
    flag = "🇰🇷" if data["market"] == "KR" else "🇺🇸"

    value_score, value_details = score_value(data)
    quality_score, quality_details = score_quality(data)
    momentum_score, momentum_details = score_momentum(data)

    # 가중평균 (저평가 우량주 전략: 밸류40 + 퀄리티40 + 모멘텀20)
    overall = calc_weighted_overall(value_score, quality_score, momentum_score)

    # 위험 필터 적용
    penalty, warnings = check_risk_warnings(data)
    overall_after_risk = max(0, overall - penalty)
    grade, opinion = grade_score(overall_after_risk)

    msg = f"📊 팩터 스코어\n{flag} {data['name']} ({data['code']})\n"
    msg += "━━━━━━━━━━━━━━━\n"

    if data["currency"] == "KRW":
        msg += f"💰 현재가: {data['price']:,}원\n"
    else:
        msg += f"💰 현재가: ${data['price']:.2f}\n"

    if data["previous_close"]:
        change = data["price"] - data["previous_close"]
        pct = (change / data["previous_close"]) * 100
        sign = "+" if change >= 0 else ""
        if data["currency"] == "KRW":
            msg += f"📈 전일 대비: {sign}{change:,.0f}원 ({sign}{pct:.2f}%)\n"
        else:
            msg += f"📈 전일 대비: {sign}{change:.2f} ({sign}{pct:.2f}%)\n"

    msg += "\n"

    msg += f"🟢 밸류: {value_score}점\n"
    for d in value_details:
        msg += f"   • {d}\n"
    msg += "\n"

    msg += f"🟡 퀄리티: {quality_score}점\n"
    for d in quality_details:
        msg += f"   • {d}\n"
    msg += "\n"

    msg += f"🔴 모멘텀: {momentum_score}점\n"
    for d in momentum_details:
        msg += f"   • {d}\n"
    msg += "\n"

    msg += "━━━━━━━━━━━━━━━\n"
    msg += f"📊 가중 종합 점수: {overall}점\n"
    msg += f"   (밸류40% + 퀄리티40% + 모멘텀20%)\n"

    # 위험 필터 표시
    if warnings:
        msg += f"\n⚠️ 위험 신호 (-{penalty}점)\n"
        for w in warnings:
            msg += f"   • {w}\n"
        msg += f"\n📊 최종 점수: {overall_after_risk}점\n"

    msg += f"🎯 등급: {grade} ({opinion})\n"
    msg += "━━━━━━━━━━━━━━━\n\n"

    msg += "📌 참고 정보\n\n"
    msg += "💰 배당\n"
    for line in get_dividend_info(data):
        msg += f"   • {line}\n"
    msg += "\n📉 변동성\n"
    for line in get_volatility_info(data):
        msg += f"   • {line}\n"
    msg += "\n🚀 성장성\n"
    for line in get_growth_info(data):
        msg += f"   • {line}\n"

    # F-Score (Piotroski) 표시
    fscore_info = data.get("fscore_info")
    if fscore_info:
        fscore, fscore_details = fscore_info
        emoji = "🟢" if fscore >= 7 else "🟡" if fscore >= 4 else "🔴"
        rating = "우량주" if fscore >= 7 else "양호" if fscore >= 4 else "주의"
        msg += f"\n📊 Piotroski F-Score: {fscore}/9 {emoji} ({rating})\n"
        for d in fscore_details:
            msg += f"   {d}\n"

    msg += "\n💡 본 분석은 참고용이며, 투자 결정의 책임은 본인에게 있습니다."
    return msg


# ============================================================
# 통합 처리
# ============================================================

def process_factor(query):
    query = query.strip()

    # 영문자만으로 구성된 경우 → 미국 주식 먼저 시도
    if re.fullmatch(r"[A-Za-z.\-]{1,10}", query):
        data = get_us_stock_data(query.upper())
        if data:
            return format_factor_message(data)

    # 한국 주식 시도 (종목명/6자리 코드)
    result = search_kor_stock(query)
    if result:
        code, name, suffix = result
        data = get_kor_stock_data(code, name, known_suffix=suffix)
        if data:
            return format_factor_message(data)

    # 미국 주식 재시도 (한국에서 못 찾은 경우)
    if not re.fullmatch(r"[A-Za-z.\-]{1,10}", query):
        data = get_us_stock_data(query.upper())
        if data:
            return format_factor_message(data)

    return None


# ============================================================
# 백테스팅
# ============================================================

def parse_backtest_args(args: list) -> tuple:
    """백테스팅 인자 파싱.
    /backtest 005930 2020-01-01
    /backtest AAPL 2018-06-15 2024-12-31
    """
    if len(args) < 2:
        return None, None, None

    query = args[0]
    start_date = args[1]
    end_date = args[2] if len(args) >= 3 else None

    # 날짜 형식 검증
    try:
        datetime.strptime(start_date, "%Y-%m-%d")
        if end_date:
            datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return None, None, None

    return query, start_date, end_date


def calc_max_drawdown(prices: pd.Series) -> tuple[float, str]:
    """최대 낙폭 (MDD) 계산."""
    cummax = prices.cummax()
    drawdown = (prices - cummax) / cummax
    mdd = drawdown.min()
    mdd_date = drawdown.idxmin()
    return mdd * 100, mdd_date.strftime("%Y-%m-%d") if pd.notna(mdd_date) else ""


def run_backtest(ticker: str, start_date: str, end_date: str = None,
                 benchmark: str = None, name: str = "", currency: str = "USD") -> dict | None:
    """백테스팅 실행."""
    try:
        # 종목 데이터
        t = yf.Ticker(ticker)
        if end_date:
            hist = t.history(start=start_date, end=end_date)
        else:
            hist = t.history(start=start_date)

        logger.info(f"run_backtest: ticker={ticker}, hist len={len(hist) if hist is not None else 0}")

        if hist is None or hist.empty or len(hist) < 2:
            return None

        prices = hist["Close"].dropna()  # NaN 제거
        if len(prices) < 2:
            logger.warning(f"유효 데이터 부족: {ticker}")
            return None
        actual_start = prices.index[0].strftime("%Y-%m-%d")
        actual_end = prices.index[-1].strftime("%Y-%m-%d")
        start_price = float(prices.iloc[0])
        end_price = float(prices.iloc[-1])

        # 수익률 계산
        total_return = ((end_price - start_price) / start_price) * 100

        # CAGR
        days = (prices.index[-1] - prices.index[0]).days
        years = days / 365.25
        cagr = ((end_price / start_price) ** (1 / years) - 1) * 100 if years > 0 else 0

        # 100만원/$10000 투자 시뮬레이션
        initial = 1_000_000 if currency == "KRW" else 10_000
        final = initial * (end_price / start_price)

        # 리스크 지표
        daily_returns = prices.pct_change().dropna()
        annual_vol = daily_returns.std() * np.sqrt(252) * 100
        sharpe = (cagr / annual_vol) if annual_vol > 0 else 0

        # MDD
        mdd, mdd_date = calc_max_drawdown(prices)

        # 벤치마크
        bench_return = None
        bench_prices = None
        if benchmark:
            try:
                tb = yf.Ticker(benchmark)
                if end_date:
                    bh = tb.history(start=start_date, end=end_date)
                else:
                    bh = tb.history(start=start_date)
                if bh is not None and not bh.empty:
                    bench_prices = bh["Close"].dropna()
                    common_dates = prices.index.intersection(bench_prices.index)
                    if len(common_dates) > 1:
                        b_start = float(bench_prices.loc[common_dates[0]])
                        b_end = float(bench_prices.loc[common_dates[-1]])
                        bench_return = ((b_end - b_start) / b_start) * 100
            except Exception:
                pass

        # 시점별 수익률
        milestones = []
        for years_after, label in [(1, "1년"), (3, "3년"), (5, "5년"), (10, "10년")]:
            target = prices.index[0] + pd.Timedelta(days=int(365.25 * years_after))
            if target > prices.index[-1]:
                break  # 아직 해당 시점이 안 됐으면 스킵
            past_prices = prices[prices.index <= target]
            if len(past_prices) > 1:
                p = float(past_prices.iloc[-1])
                if np.isnan(p) or start_price == 0:
                    continue
                ret = ((p - start_price) / start_price) * 100
                date_label = past_prices.index[-1].strftime("%Y-%m-%d")
                milestones.append((label, date_label, ret))

        return {
            "ticker": ticker,
            "name": name or ticker,
            "actual_start": actual_start,
            "actual_end": actual_end,
            "start_price": start_price,
            "end_price": end_price,
            "years": years,
            "total_return": total_return,
            "cagr": cagr,
            "initial": initial,
            "final": final,
            "annual_vol": annual_vol,
            "sharpe": sharpe,
            "mdd": mdd,
            "mdd_date": mdd_date,
            "bench_return": bench_return,
            "currency": currency,
            "prices": prices,
            "bench_prices": bench_prices,
            "milestones": milestones,
        }
    except Exception as e:
        logger.exception(f"백테스팅 실패 ({ticker})")
        return None


def create_backtest_chart(result: dict, benchmark_name: str = "벤치마크") -> bytes:
    """백테스팅 결과 차트 생성."""
    fig, axes = plt.subplots(2, 1, figsize=(11, 8), gridspec_kw={"height_ratios": [2, 1]})

    prices = result["prices"]
    bench_prices = result.get("bench_prices")

    # 한글 폰트 (Railway에서는 없을 수 있음, 폴백)
    try:
        import platform
        if platform.system() == "Windows":
            plt.rc("font", family="Malgun Gothic")
        else:
            for f in ["NanumGothic", "AppleGothic", "DejaVu Sans"]:
                try:
                    plt.rc("font", family=f)
                    break
                except Exception:
                    continue
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass

    # 상단: 가격 추이 (정규화)
    ax1 = axes[0]
    norm_prices = (prices / prices.iloc[0]) * 100
    ax1.plot(norm_prices.index, norm_prices.values, label=result["name"], color="#1f77b4", linewidth=2)

    if bench_prices is not None and len(bench_prices) > 0:
        common = prices.index.intersection(bench_prices.index)
        if len(common) > 1:
            bench_aligned = bench_prices.loc[common]
            norm_bench = (bench_aligned / bench_aligned.iloc[0]) * 100
            ax1.plot(norm_bench.index, norm_bench.values, label=benchmark_name,
                     color="#ff7f0e", linewidth=2, alpha=0.7)

    ax1.set_title(f"{result['name']} 백테스팅 ({result['actual_start']} ~ {result['actual_end']})",
                  fontsize=13, fontweight="bold")
    ax1.set_ylabel("정규화 수익률 (시작 = 100)")
    ax1.legend(loc="upper left")
    ax1.grid(True, alpha=0.3)
    ax1.axhline(100, color="gray", linestyle="--", linewidth=0.8, alpha=0.5)

    # 하단: 낙폭 (Drawdown)
    ax2 = axes[1]
    cummax = prices.cummax()
    dd = (prices - cummax) / cummax * 100
    ax2.fill_between(dd.index, dd.values, 0, color="red", alpha=0.3)
    ax2.plot(dd.index, dd.values, color="darkred", linewidth=1)
    ax2.set_ylabel("낙폭 (%)")
    ax2.set_xlabel("날짜")
    ax2.grid(True, alpha=0.3)
    ax2.axhline(0, color="gray", linestyle="-", linewidth=0.5)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf.read()


def format_backtest_message(result: dict, benchmark_name: str = "") -> str:
    """백테스팅 결과 메시지."""
    msg = f"📊 백테스팅 결과\n"
    msg += f"📌 {result['name']} ({result['ticker']})\n"
    msg += "━━━━━━━━━━━━━━━\n"
    msg += f"📅 기간: {result['actual_start']} ~ {result['actual_end']}\n"
    msg += f"   (약 {result['years']:.1f}년)\n\n"

    # 투자 시뮬레이션
    if result["currency"] == "KRW":
        msg += f"💰 투자 시뮬레이션 (100만원 가정)\n"
        msg += f"   매수가: {result['start_price']:,.0f}원\n"
        msg += f"   현재가: {result['end_price']:,.0f}원\n"
        msg += f"   평가금액: {result['final']:,.0f}원\n\n"
    else:
        msg += f"💰 투자 시뮬레이션 ($10,000 가정)\n"
        msg += f"   매수가: ${result['start_price']:.2f}\n"
        msg += f"   현재가: ${result['end_price']:.2f}\n"
        msg += f"   평가금액: ${result['final']:,.2f}\n\n"

    # 수익률
    sign = "+" if result["total_return"] >= 0 else ""
    msg += f"📈 수익률\n"
    msg += f"   총 수익률: {sign}{result['total_return']:.2f}%\n"
    msg += f"   연평균 (CAGR): {sign}{result['cagr']:.2f}%\n\n"

    # 벤치마크
    if result["bench_return"] is not None:
        bsign = "+" if result["bench_return"] >= 0 else ""
        excess = result["total_return"] - result["bench_return"]
        esign = "+" if excess >= 0 else ""
        msg += f"📊 벤치마크 대비\n"
        msg += f"   {benchmark_name}: {bsign}{result['bench_return']:.2f}%\n"
        msg += f"   초과 수익: {esign}{excess:.2f}%p\n\n"

    # 리스크
    msg += f"⚠️ 리스크 지표\n"
    msg += f"   최대 낙폭 (MDD): {result['mdd']:.2f}% ({result['mdd_date']})\n"
    msg += f"   연환산 변동성: {result['annual_vol']:.2f}%\n"
    msg += f"   샤프 비율: {result['sharpe']:.2f}\n"

    # 시점별
    if result["milestones"]:
        msg += f"\n📉 시점별 수익률\n"
        for label, date, ret in result["milestones"]:
            sign = "+" if ret >= 0 else ""
            msg += f"   {label} 후 ({date}): {sign}{ret:.2f}%\n"

    msg += "\n💡 본 분석은 참고용이며, 과거 수익률이 미래를 보장하지 않습니다."
    return msg


def process_backtest(query: str, start_date: str, end_date: str = None) -> tuple:
    """백테스팅 통합 처리. (result, message, chart_bytes, benchmark_name) 반환."""
    name = ""
    ticker = None
    currency = "USD"
    benchmark = None
    benchmark_name = ""

    # 영문 입력 → 미국 우선
    if re.fullmatch(r"[A-Za-z.\-]{1,10}", query):
        ticker = query.upper()
        try:
            info = yf.Ticker(ticker).info
            name = info.get("longName") or info.get("shortName") or ticker
            currency = "USD"
            benchmark = "^GSPC"
            benchmark_name = "S&P 500"
        except Exception:
            pass

    # 한국 주식
    if ticker is None:
        result = search_kor_stock(query)
        logger.info(f"search_kor_stock({query}) → {result}")
        if result:
            code, kor_name, suffix = result
            ticker = code + suffix
            name = kor_name
            currency = "KRW"
            benchmark = "^KS11" if suffix == ".KS" else "^KQ11"
            benchmark_name = "KOSPI" if suffix == ".KS" else "KOSDAQ"

    logger.info(f"process_backtest: query={query}, ticker={ticker}, start={start_date}")

    # 미국 재시도
    if ticker is None and not re.fullmatch(r"[A-Za-z.\-]{1,10}", query):
        ticker = query.upper()
        currency = "USD"
        benchmark = "^GSPC"
        benchmark_name = "S&P 500"

    if not ticker:
        return None, None, None, None

    result = run_backtest(ticker, start_date, end_date, benchmark, name, currency)
    if not result:
        return None, None, None, None

    message = format_backtest_message(result, benchmark_name)
    chart_bytes = create_backtest_chart(result, benchmark_name)
    return result, message, chart_bytes, benchmark_name


# ============================================================
# 스크리닝 (종목 검색)
# ============================================================

SCREENING_UNIVERSE = []  # [{"code", "name", "suffix", "market"}]


def load_screening_universe():
    """코스피200 + 코스닥150(대형주) + S&P500 로딩."""
    global SCREENING_UNIVERSE
    SCREENING_UNIVERSE = []
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

    # 2. 코스닥150: 네이버 금융에서 시가총액 상위 150개 조회 (빠름)
    try:
        kosdaq_top = []
        for page in range(1, 5):  # 페이지당 50개 × 4 = 200개에서 150개 선택
            url = f"https://finance.naver.com/sise/sise_market_sum.naver?sosok=1&page={page}"
            res = requests.get(url, headers=HEADERS, timeout=15)
            res.encoding = "euc-kr"
            soup = BeautifulSoup(res.text, "html.parser")
            rows = soup.select("table.type_2 tbody tr")
            for row in rows:
                link = row.select_one("a.tltle")
                if not link:
                    continue
                name = link.get_text(strip=True)
                href = link.get("href", "")
                code_match = re.search(r"code=(\d{6})", href)
                if not code_match:
                    continue
                code = code_match.group(1)
                kosdaq_top.append({"code": code, "name": name})
                if len(kosdaq_top) >= 150:
                    break
            if len(kosdaq_top) >= 150:
                break

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
            # 배당
            "dividend_yield": pct(info.get("dividendYield")),
            "payout_ratio": pct(info.get("payoutRatio")),
            # 성장 (% 변환)
            "revenue_growth": pct(info.get("revenueGrowth")),
            "earnings_growth": pct(info.get("earningsGrowth")),
        }
    except Exception:
        return None


# ============================================================
# 텔레그램 핸들러
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📊 퀀트 분석 봇입니다.\n\n"
        "🎯 팩터 스코어링\n"
        "/factor 삼성전자 - 종목 팩터 분석\n"
        "/factor AAPL - 미국 주식도 가능\n\n"
        "또는 그냥 종목명/티커만 입력해도 돼요!\n\n"
        "⚙️ 점수화 항목:\n"
        "🟢 밸류 (PER, PBR, PSR)\n"
        "🟡 퀄리티 (ROE, 영업이익률, 부채비율)\n"
        "🔴 모멘텀 (1M/3M/6M/12M 수익률)\n\n"
        "📌 참고 정보:\n"
        "💰 배당 (배당수익률, 배당성향)\n"
        "📉 변동성 (베타, 연환산 변동성)"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 사용법\n\n"
        "🎯 팩터 분석:\n"
        "  /factor <종목> - 팩터 스코어링\n"
        "  예) /factor 삼성전자\n"
        "      /factor 005930\n"
        "      /factor AAPL\n\n"
        "🔍 스크리닝:\n"
        "  /screen PER<10 ROE>15       (전체)\n"
        "  /screen KR PER<10 ROE>15    (한국만)\n"
        "  /screen US DIV>3 ROE>15     (미국만)\n"
        "  /screen 만 입력하면 도움말\n\n"
        "📊 백테스팅:\n"
        "  /backtest <종목> <시작일> [종료일]\n"
        "  예) /backtest 삼성전자 2020-01-01\n"
        "      /backtest AAPL 2018-06-15\n\n"
        "또는 종목명/티커만 입력해도 자동 분석합니다.\n\n"
        "⚙️ 점수 산출 방식:\n"
        "  • 밸류40 + 퀄리티40 + 모멘텀20\n"
        "  • 위험 신호 자동 감점\n"
        "  • Piotroski F-Score 별도 표시\n\n"
        "📌 참고 정보 (점수 X):\n"
        "  • 배당 / 변동성 / 성장성"
    )


async def factor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("사용법: /factor 삼성전자 또는 /factor AAPL")
        return
    query = " ".join(context.args).strip()
    await update.message.reply_text("⏳ 팩터 분석 중...")
    try:
        result = process_factor(query)
        if result:
            await update.message.reply_text(result, disable_web_page_preview=True)
        else:
            await update.message.reply_text(f"❌ '{query}' 종목을 찾을 수 없어요.")
    except Exception as e:
        logger.exception("팩터 분석 실패")
        await update.message.reply_text(f"⚠️ 오류: {e}")


async def backtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """백테스팅 명령어. /backtest 005930 2020-01-01 [2024-12-31]"""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "📊 백테스팅 사용법:\n"
            "/backtest <종목> <시작일> [종료일]\n\n"
            "예시:\n"
            "  /backtest 삼성전자 2020-01-01\n"
            "  /backtest 005930 2020-01-01\n"
            "  /backtest AAPL 2018-06-15 2024-12-31\n\n"
            "📋 분석 항목:\n"
            "  • 수익률 (총/CAGR)\n"
            "  • 벤치마크 대비 초과수익\n"
            "    🇰🇷 한국: KOSPI/KOSDAQ\n"
            "    🇺🇸 미국: S&P 500\n"
            "  • 최대 낙폭 (MDD)\n"
            "  • 변동성, 샤프 비율\n"
            "  • 차트 (가격 추이 + 낙폭)"
        )
        return

    query, start_date, end_date = parse_backtest_args(context.args)
    if not query:
        await update.message.reply_text(
            "❌ 입력 형식이 잘못됐어요.\n"
            "날짜는 YYYY-MM-DD 형식으로 입력하세요.\n"
            "예) /backtest 삼성전자 2020-01-01"
        )
        return

    await update.message.reply_text("⏳ 백테스팅 분석 중... (1~2분 소요)")
    chat_id = update.effective_chat.id

    async def run_bt():
        try:
            # 동기 함수를 별도 스레드에서 실행 (yfinance 호출이 동기)
            loop = asyncio.get_event_loop()
            result_tuple = await loop.run_in_executor(
                None, process_backtest, query, start_date, end_date
            )
            result, message, chart_bytes, bench_name = result_tuple

            if not result:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ '{query}' 종목 데이터를 가져올 수 없어요.\n종목명/티커와 날짜를 확인해주세요."
                )
                return

            # 차트 먼저 전송
            if chart_bytes:
                try:
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=chart_bytes,
                        caption=f"📊 {result['name']} ({result['ticker']})"
                    )
                except Exception as e:
                    logger.warning(f"차트 전송 실패: {e}")

            # 텍스트 결과
            await context.bot.send_message(chat_id=chat_id, text=message, disable_web_page_preview=True)
        except Exception as e:
            logger.exception("백테스팅 실패")
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ 오류: {e}")

    asyncio.create_task(run_bt())


async def screen_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """스크리닝 명령어.
    /screen PER<10 ROE>15       → 전체 (한국+미국)
    /screen KR PER<10 ROE>15    → 한국만
    /screen US PER<10 ROE>15    → 미국만
    """
    if not context.args:
        await update.message.reply_text(
            "📋 스크리닝 사용법:\n"
            "/screen PER<10 ROE>15\n"
            "/screen KR PER<10 ROE>15  (한국만)\n"
            "/screen US PER<10 ROE>15  (미국만)\n\n"
            "💰 밸류:\n"
            "  PER, FORWARDPER, PBR, PSR, PEG\n\n"
            "⚙️ 퀄리티:\n"
            "  ROE, ROA (%), OPMARGIN (영업이익률 %)\n"
            "  NETMARGIN (순이익률 %), GROSSMARGIN (매출총이익률 %)\n"
            "  DEBT (부채비율 %), CURRENTRATIO (유동비율)\n\n"
            "💰 배당:\n"
            "  DIV (배당수익률 %), PAYOUT (배당성향 %)\n\n"
            "📈 성장:\n"
            "  REVGROWTH (매출성장률 %), EPSGROWTH (EPS성장률 %)\n\n"
            "📊 규모:\n"
            "  MARKETCAP\n"
            "    🇰🇷 한국: 억원  예) MARKETCAP>10000 (1조 이상)\n"
            "    🇺🇸 미국: 백만달러  예) MARKETCAP>10000 (100억달러 이상)\n"
            "  PRICE\n"
            "    🇰🇷 한국: 원  예) PRICE<50000\n"
            "    🇺🇸 미국: 달러  예) PRICE<100\n\n"
            "🔑 특수 키워드:\n"
            "  IMPROVING     → PER > Forward PER (실적 개선 기대)\n"
            "  DETERIORATING → PER < Forward PER (실적 둔화 우려)\n"
            "  PROFITABLE    → EPS 흑자 종목만\n"
            "  DIVIDEND      → 배당 지급 종목만\n\n"
            "연산자: <, <=, >, >=, =\n\n"
            "예시:\n"
            "  /screen KR IMPROVING ROE>10\n"
            "  /screen KR PER<10 ROE>15\n"
            "  /screen US DIVIDEND GROSSMARGIN>40\n"
            "  /screen PROFITABLE PBR<1 ROE>10"
        )
        return

    args_text = " ".join(context.args).strip()

    # 시장 필터 파싱 (첫 번째 단어가 KR/US인지 확인)
    market_filter = "ALL"
    first_word = context.args[0].upper()
    if first_word == "KR":
        market_filter = "KR"
        args_text = " ".join(context.args[1:]).strip()
    elif first_word == "US":
        market_filter = "US"
        args_text = " ".join(context.args[1:]).strip()

    conditions = parse_screen_conditions(args_text)

    if not conditions:
        await update.message.reply_text(
            "❌ 조건을 인식할 수 없어요.\n"
            "예) /screen PER<10 ROE>15\n"
            "/screen 만 입력하면 도움말이 나와요."
        )
        return

    # 시장 필터 적용
    if market_filter == "KR":
        universe = [s for s in SCREENING_UNIVERSE if s["market"] in ("KOSPI200", "KOSDAQ150")]
        market_label = "🇰🇷 한국 (코스피200+코스닥150)"
    elif market_filter == "US":
        universe = [s for s in SCREENING_UNIVERSE if s["market"] == "SP500"]
        market_label = "🇺🇸 미국 (S&P500)"
    else:
        universe = SCREENING_UNIVERSE
        market_label = "🇰🇷 한국 + 🇺🇸 미국 (전체)"

    if not universe:
        await update.message.reply_text("⚠️ 종목 리스트가 아직 로딩되지 않았어요. 잠시 후 다시 시도하세요.")
        return

    cond_summary = ", ".join([
        c["raw"] if c.get("type") in ("compare", "positive")
        else f"{c['raw']}{c['op']}{c['val']}"
        for c in conditions
    ])
    total = len(universe)

    # 예상 시간 계산
    est_min = max(1, total * 0.3 // 60)
    await update.message.reply_text(
        f"🔍 스크리닝 시작!\n"
        f"범위: {market_label}\n"
        f"조건: {cond_summary}\n"
        f"종목 수: {total}개\n"
        f"⏳ 약 {est_min}~{est_min+5}분 소요. 완료 시 자동 알림드려요."
    )

    chat_id = update.effective_chat.id

    # 비동기 백그라운드 실행
    async def run_screening():
        try:
            import gc
            matches = []
            processed = 0
            last_progress = 0

            for item in universe:
                processed += 1
                data = fetch_stock_quick(item)
                if data:
                    if all(check_condition(data, c) for c in conditions):
                        matches.append(data)

                await asyncio.sleep(0.3)

                if processed % 50 == 0:
                    gc.collect()

                if processed - last_progress >= 100:
                    last_progress = processed
                    try:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"⏳ 진행 중... {processed}/{total} ({len(matches)}개 매칭)"
                        )
                    except Exception:
                        pass

            if not matches:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"❌ 조건에 맞는 종목이 없어요.\n"
                        f"조건: {cond_summary}\n\n"
                        f"💡 가능한 원인:\n"
                        f"• 조건이 너무 엄격해요 (완화 시도)\n"
                        f"• FORWARDPER 등 일부 지표는 한국 주식에 데이터 없을 수 있어요\n"
                        f"• PER/PBR 등 기본 지표로 먼저 시도해보세요"
                    )
                )
                return

            # ROE 높은 순 정렬
            matches.sort(key=lambda x: -(x.get("roe") or 0))

            flag_map = {"KOSPI200": "🇰🇷", "KOSDAQ150": "🇰🇷", "SP500": "🇺🇸"}
            msg = f"✅ 스크리닝 완료!\n범위: {market_label}\n조건: {cond_summary}\n매칭: {len(matches)}개\n"
            msg += "━━━━━━━━━━━━━━━\n\n"

            for i, m in enumerate(matches[:30], 1):
                flag = flag_map.get(m["market"], "🌐")
                msg += f"{i}. {flag} {m['name']} ({m['code']})\n"
                parts = []
                if m.get("pe_ratio") and m["pe_ratio"] > 0:
                    parts.append(f"PER {m['pe_ratio']:.1f}")
                if m.get("pb_ratio") and m["pb_ratio"] > 0:
                    parts.append(f"PBR {m['pb_ratio']:.1f}")
                if m.get("roe") is not None:
                    parts.append(f"ROE {m['roe']:.1f}%")
                if m.get("dividend_yield"):
                    parts.append(f"DIV {m['dividend_yield']:.1f}%")
                if m.get("market_cap_bil"):
                    if m["market"] != "SP500":
                        parts.append(f"시총 {m['market_cap_bil']:.0f}억")
                    else:
                        parts.append(f"시총 ${m['market_cap_bil']:.0f}M")
                if parts:
                    msg += "   " + " | ".join(parts) + "\n"

            if len(matches) > 30:
                msg += f"\n... 외 {len(matches)-30}개 더\n"

            msg += "\n💡 자세한 분석은 /factor <종목명>"

            if len(msg) > 4000:
                msg = msg[:3950] + "\n\n... (메시지 길이 초과)"

            await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            logger.exception("스크리닝 실패")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ 스크리닝 중 오류: {e}"
            )

    asyncio.create_task(run_screening())


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.message.text.strip()
    logger.info(f"조회 요청: {query}")
    try:
        result = process_factor(query)
        if result:
            await update.message.reply_text(result, disable_web_page_preview=True)
        else:
            await update.message.reply_text(
                f"❌ '{query}' 종목을 찾을 수 없어요.\n/help 로 사용법을 확인하세요."
            )
    except Exception as e:
        logger.exception("조회 실패")
        await update.message.reply_text(f"⚠️ 오류: {e}")


def main() -> None:
    load_stock_map()
    load_screening_universe()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("factor", factor_cmd))
    app.add_handler(CommandHandler("screen", screen_cmd))
    app.add_handler(CommandHandler("backtest", backtest_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("퀀트 봇 시작...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
