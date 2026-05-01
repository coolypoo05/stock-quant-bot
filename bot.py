"""
주식 퀀트 분석 텔레그램 봇 (한국 + 미국)
팩터 스코어링: 밸류 + 퀄리티 + 모멘텀
참고 정보: 배당 + 변동성
"""

import os
import re
import io
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
import numpy as np
import pandas as pd
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

STOCK_MAP: dict = {}
KST = ZoneInfo("Asia/Seoul")


# ============================================================
# 한국 종목 리스트
# ============================================================

def load_stock_map():
    global STOCK_MAP
    logger.info("한국 종목 리스트 로딩 중...")
    try:
        url = "https://kind.krx.co.kr/corpgeneral/corpList.do"
        params = {"method": "download", "searchType": "13"}
        res = requests.get(url, params=params, headers=HEADERS, timeout=30)
        res.raise_for_status()
        df = pd.read_html(io.StringIO(res.text))[0]
        df["종목코드"] = df["종목코드"].astype(str).str.zfill(6)
        for _, row in df.iterrows():
            STOCK_MAP[row["회사명"]] = row["종목코드"]
        logger.info(f"한국 종목 리스트 로딩 완료: {len(STOCK_MAP)}개")
    except Exception as e:
        logger.error(f"종목 리스트 로딩 실패: {e}")


def search_kor_stock(query: str):
    query = query.strip()
    if re.fullmatch(r"\d{6}", query):
        for name, code in STOCK_MAP.items():
            if code == query:
                return query, name
        return query, query
    if query in STOCK_MAP:
        return STOCK_MAP[query], query
    for name, code in STOCK_MAP.items():
        if name.lower() == query.lower():
            return code, name
    for name, code in STOCK_MAP.items():
        if query.lower() in name.lower():
            return code, name
    return None


# ============================================================
# 한국 주식 데이터 (yfinance + 네이버)
# ============================================================

def get_kor_stock_data(code: str, name: str):
    try:
        ticker = None
        info = None
        hist = None
        for suffix in [".KS", ".KQ"]:
            try:
                t = yf.Ticker(f"{code}{suffix}")
                test_info = t.info
                if test_info and (test_info.get("regularMarketPrice") or test_info.get("currentPrice")):
                    ticker = f"{code}{suffix}"
                    info = test_info
                    hist = t.history(period="1y")
                    break
            except Exception:
                continue
        if not info:
            return None

        return {
            "code": code, "name": name, "ticker": ticker,
            "price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "previous_close": info.get("previousClose"),
            "market_cap": info.get("marketCap"),
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "eps": info.get("trailingEps"),
            "forward_eps": info.get("forwardEps"),
            "pb_ratio": info.get("priceToBook"),
            "ps_ratio": info.get("priceToSalesTrailing12Months"),
            "roe": info.get("returnOnEquity"),
            "operating_margin": info.get("operatingMargins"),
            "debt_to_equity": info.get("debtToEquity"),
            "dividend_yield": info.get("dividendYield"),
            "payout_ratio": info.get("payoutRatio"),
            "beta": info.get("beta"),
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
            "roe": info.get("returnOnEquity"),
            "operating_margin": info.get("operatingMargins"),
            "debt_to_equity": info.get("debtToEquity"),
            "dividend_yield": info.get("dividendYield"),
            "payout_ratio": info.get("payoutRatio"),
            "beta": info.get("beta"),
            "history": hist,
            "currency": "USD", "market": "US",
        }
    except Exception as e:
        logger.error(f"미국 주식 데이터 실패 ({ticker}): {e}")
        return None


# ============================================================
# 팩터 스코어링
# ============================================================

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

    if not scores:
        return 0, ["데이터 부족"]
    return int(np.mean(scores)), details


def score_quality(data):
    """퀄리티 팩터 (높은 ROE/영업이익률, 낮은 부채비율, EPS 안정성)."""
    scores = []
    details = []

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
        scores.append(s)
        details.append(f"부채비율: {debt:.0f}% ({g})")

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

    if not scores:
        return 0, ["데이터 부족"]
    return int(np.mean(scores)), details


# ============================================================
# 참고 정보 (점수화 X)
# ============================================================

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

    total_scores = [s for s in [value_score, quality_score, momentum_score] if s > 0]
    overall = int(np.mean(total_scores)) if total_scores else 0
    grade, opinion = grade_score(overall)

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
    msg += f"📊 종합 점수: {overall}점\n"
    msg += f"🎯 등급: {grade} ({opinion})\n"
    msg += "━━━━━━━━━━━━━━━\n\n"

    msg += "📌 참고 정보\n\n"
    msg += "💰 배당\n"
    for line in get_dividend_info(data):
        msg += f"   • {line}\n"
    msg += "\n📉 변동성\n"
    for line in get_volatility_info(data):
        msg += f"   • {line}\n"

    msg += "\n💡 본 분석은 참고용이며, 투자 결정의 책임은 본인에게 있습니다."
    return msg


# ============================================================
# 통합 처리
# ============================================================

def process_factor(query):
    result = search_kor_stock(query)
    if result:
        code, name = result
        data = get_kor_stock_data(code, name)
        if data:
            return format_factor_message(data)

    if re.fullmatch(r"[A-Za-z.\-]{1,10}", query):
        data = get_us_stock_data(query.upper())
        if data:
            return format_factor_message(data)

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
        "또는 종목명/티커만 입력해도 자동 분석합니다.\n\n"
        "⚙️ 점수 산출 방식:\n"
        "  • 밸류, 퀄리티, 모멘텀 3개 팩터\n"
        "  • 각각 0~100점 → 평균 = 종합점수\n"
        "  • 등급: A(80+), B+(70+), B(60+)\n\n"
        "📌 참고 정보 (점수 X):\n"
        "  • 배당 정보\n"
        "  • 변동성 정보"
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
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("factor", factor_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("퀀트 봇 시작...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
