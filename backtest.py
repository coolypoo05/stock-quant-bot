"""단일/포트폴리오 백테스팅"""

import io
import re
from datetime import datetime
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yfinance as yf
from config import logger
from data import search_kor_stock


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
# 포트폴리오 백테스팅
# ============================================================

def parse_portfolio_args(args: list) -> tuple:
    """
    /backtest_portfolio AAPL:4 MSFT:3 GOOGL:3 2020-01-01 10000
    /backtest_portfolio 삼성전자:4 SK하이닉스:3 NAVER:3 2020-01-01 10000000
    반환: (종목_비율_리스트, 시작일, 종료일, 초기금액)
    """
    if len(args) < 3:
        return None, None, None, None

    holdings = []
    start_date = None
    end_date = None
    initial = None

    for arg in args:
        # 날짜 형식
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", arg):
            if start_date is None:
                start_date = arg
            else:
                end_date = arg
        # 숫자 (초기 투자금)
        elif re.fullmatch(r"\d+", arg):
            initial = int(arg)
        # 종목:비율
        elif ":" in arg:
            parts = arg.split(":")
            if len(parts) == 2:
                ticker = parts[0].strip()
                try:
                    weight = float(parts[1])
                    holdings.append({"ticker": ticker, "weight": weight})
                except ValueError:
                    pass

    if not holdings or not start_date:
        return None, None, None, None

    # 비율 정규화 (합이 1이 되도록)
    total_weight = sum(h["weight"] for h in holdings)
    for h in holdings:
        h["weight"] = h["weight"] / total_weight

    # 기본 초기 투자금
    if initial is None:
        initial = 10_000_000  # 기본 천만원

    return holdings, start_date, end_date, initial

def run_portfolio_backtest(holdings: list, start_date: str, end_date: str = None,
                           initial: int = 10_000_000) -> dict | None:
    """포트폴리오 백테스팅 실행."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # 1. 각 종목 데이터 병렬 수집
    def fetch_ticker(h):
        query = h["ticker"]
        ticker = None
        name = query
        currency = "USD"
        suffix = ""

        # 한국 주식 시도
        result = search_kor_stock(query)
        if result:
            code, kor_name, sfx = result
            ticker = code + sfx
            name = kor_name
            currency = "KRW"
        # 영문 → 미국 주식
        elif re.fullmatch(r"[A-Za-z.\-]{1,10}", query):
            ticker = query.upper()
            name = query.upper()
            currency = "USD"
            try:
                info = yf.Ticker(ticker).info
                name = info.get("longName") or info.get("shortName") or ticker
            except Exception:
                pass

        if not ticker:
            return None

        try:
            t = yf.Ticker(ticker)
            if end_date:
                hist = t.history(start=start_date, end=end_date)
            else:
                hist = t.history(start=start_date)
            prices = hist["Close"].dropna()  # NaN 제거
            if prices.empty or len(prices) < 2:
                return None
            return {
                "ticker": ticker,
                "name": name,
                "weight": h["weight"],
                "currency": currency,
                "prices": prices,
            }
        except Exception as e:
            logger.warning(f"포트폴리오 데이터 수집 실패 ({ticker}): {e}")
            return None

    results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(fetch_ticker, h) for h in holdings]
        for f in as_completed(futures):
            r = f.result()
            if r:
                results.append(r)

    if not results:
        return None

    # 통화 혼용 체크
    currencies = set(r["currency"] for r in results)
    if len(currencies) > 1:
        return {"error": "mixed_currency", "currencies": list(currencies)}

    currency = list(currencies)[0]

    # 2. 공통 날짜 범위로 정렬
    common_dates = results[0]["prices"].index
    for r in results[1:]:
        common_dates = common_dates.intersection(r["prices"].index)

    if len(common_dates) < 2:
        return None

    # 3. 포트폴리오 가치 계산 (매수 후 방치)
    portfolio_values = pd.Series(0.0, index=common_dates)
    individual_results = []

    for r in results:
        prices = r["prices"].reindex(common_dates).ffill().dropna()
        if prices.empty:
            continue
        start_price = float(prices.iloc[0])
        end_price = float(prices.iloc[-1])
        if start_price == 0 or np.isnan(start_price) or np.isnan(end_price):
            continue
        alloc = initial * r["weight"]
        shares = alloc / start_price
        values = prices * shares

        total_ret = ((end_price - start_price) / start_price) * 100
        days = (prices.index[-1] - prices.index[0]).days
        years = days / 365.25
        cagr = ((end_price / start_price) ** (1 / years) - 1) * 100 if years > 0 else 0

        portfolio_values = portfolio_values.add(values, fill_value=0)
        individual_results.append({
            "ticker": r["ticker"],
            "name": r["name"],
            "weight": r["weight"],
            "alloc": alloc,
            "start_price": start_price,
            "end_price": end_price,
            "shares": shares,
            "final_value": float(values.iloc[-1]),
            "total_return": total_ret,
            "cagr": cagr,
            "prices": prices,
        })

    # 4. 포트폴리오 전체 지표
    portfolio_values = portfolio_values.dropna()
    if portfolio_values.empty or len(portfolio_values) < 2:
        return None
    port_start = float(portfolio_values.iloc[0])
    port_end = float(portfolio_values.iloc[-1])
    if port_start == 0 or np.isnan(port_start) or np.isnan(port_end):
        return None
    total_return = ((port_end - port_start) / port_start) * 100
    days = (common_dates[-1] - common_dates[0]).days
    years = days / 365.25
    cagr = ((port_end / port_start) ** (1 / years) - 1) * 100 if years > 0 else 0

    daily_returns = portfolio_values.pct_change().dropna()
    annual_vol = daily_returns.std() * np.sqrt(252) * 100
    sharpe = (cagr / annual_vol) if annual_vol > 0 else 0

    # MDD
    cummax = portfolio_values.cummax()
    dd = (portfolio_values - cummax) / cummax * 100
    mdd = float(dd.min())
    mdd_date = dd.idxmin().strftime("%Y-%m-%d") if pd.notna(dd.idxmin()) else ""

    # 5. 벤치마크
    bench_ticker = "^GSPC" if currency == "USD" else "^KS11"
    bench_name = "S&P 500" if currency == "USD" else "KOSPI"
    bench_return = None
    bench_prices = None
    try:
        tb = yf.Ticker(bench_ticker)
        if end_date:
            bh = tb.history(start=start_date, end=end_date)
        else:
            bh = tb.history(start=start_date)
        if bh is not None and not bh.empty:
            bp = bh["Close"].dropna()
            common_b = common_dates.intersection(bp.index)
            if len(common_b) > 1:
                bench_prices = bp.loc[common_b]
                bench_return = ((float(bench_prices.iloc[-1]) - float(bench_prices.iloc[0]))
                                / float(bench_prices.iloc[0])) * 100
    except Exception:
        pass

    return {
        "currency": currency,
        "initial": initial,
        "actual_start": common_dates[0].strftime("%Y-%m-%d"),
        "actual_end": common_dates[-1].strftime("%Y-%m-%d"),
        "years": years,
        "port_start": port_start,
        "port_end": port_end,
        "total_return": total_return,
        "cagr": cagr,
        "annual_vol": annual_vol,
        "sharpe": sharpe,
        "mdd": mdd,
        "mdd_date": mdd_date,
        "bench_return": bench_return,
        "bench_name": bench_name,
        "portfolio_values": portfolio_values,
        "bench_prices": bench_prices,
        "individual": individual_results,
    }

def create_portfolio_chart(result: dict) -> bytes:
    """포트폴리오 백테스팅 차트 생성."""
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), gridspec_kw={"height_ratios": [2, 1]})

    try:
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass

    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
              "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]

    ax1 = axes[0]
    port_vals = result["portfolio_values"]
    norm_port = (port_vals / port_vals.iloc[0]) * 100
    ax1.plot(norm_port.index, norm_port.values, label="포트폴리오",
             color="black", linewidth=2.5, zorder=5)

    # 개별 종목
    for i, item in enumerate(result["individual"]):
        norm = (item["prices"] / item["prices"].iloc[0]) * 100
        label = f"{item['name']} ({item['weight']*100:.0f}%)"
        ax1.plot(norm.index, norm.values, label=label,
                 color=colors[i % len(colors)], linewidth=1.2, alpha=0.7, linestyle="--")

    # 벤치마크
    if result["bench_prices"] is not None:
        bp = result["bench_prices"]
        norm_b = (bp / bp.iloc[0]) * 100
        ax1.plot(norm_b.index, norm_b.values, label=result["bench_name"],
                 color="gray", linewidth=1.5, alpha=0.8, linestyle=":")

    ax1.set_title(f"포트폴리오 백테스팅 ({result['actual_start']} ~ {result['actual_end']})",
                  fontsize=13, fontweight="bold")
    ax1.set_ylabel("정규화 수익률 (시작 = 100)")
    ax1.legend(loc="upper left", fontsize=8)
    ax1.grid(True, alpha=0.3)
    ax1.axhline(100, color="gray", linestyle="--", linewidth=0.8, alpha=0.5)

    # 낙폭
    ax2 = axes[1]
    cummax = port_vals.cummax()
    dd = (port_vals - cummax) / cummax * 100
    ax2.fill_between(dd.index, dd.values, 0, color="red", alpha=0.3)
    ax2.plot(dd.index, dd.values, color="darkred", linewidth=1)
    ax2.set_ylabel("포트폴리오 낙폭 (%)")
    ax2.set_xlabel("날짜")
    ax2.grid(True, alpha=0.3)
    ax2.axhline(0, color="gray", linewidth=0.5)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf.read()

def format_portfolio_message(result: dict) -> str:
    """포트폴리오 백테스팅 결과 메시지."""
    cur = result["currency"]
    sym = "₩" if cur == "KRW" else "$"
    fmt = lambda v: f"{sym}{v:,.0f}" if cur == "KRW" else f"{sym}{v:,.2f}"

    msg = "📊 포트폴리오 백테스팅 결과\n"
    msg += "━━━━━━━━━━━━━━━\n"
    msg += f"📅 기간: {result['actual_start']} ~ {result['actual_end']}\n"
    msg += f"   (약 {result['years']:.1f}년)\n\n"

    # 구성
    msg += f"⚙️ 구성 (초기 투자금: {fmt(result['initial'])})\n"
    for item in result["individual"]:
        alloc = fmt(item["alloc"])
        msg += f"   {item['name']} {item['weight']*100:.0f}% ({alloc})\n"
    msg += "\n"

    # 개별 수익률
    msg += "📈 개별 수익률\n"
    for item in result["individual"]:
        sign = "+" if item["total_return"] >= 0 else ""
        msg += (f"   {item['name']}: {sign}{item['total_return']:.1f}%"
                f" → {fmt(item['final_value'])}\n")
    msg += "\n"

    # 포트폴리오 전체
    sign = "+" if result["total_return"] >= 0 else ""
    msg += "💼 포트폴리오 전체\n"
    msg += f"   최종 평가금액: {fmt(result['port_end'])}\n"
    msg += f"   총 수익률: {sign}{result['total_return']:.2f}%\n"
    msg += f"   연평균 (CAGR): {sign}{result['cagr']:.2f}%\n\n"

    # 벤치마크
    if result["bench_return"] is not None:
        bsign = "+" if result["bench_return"] >= 0 else ""
        excess = result["total_return"] - result["bench_return"]
        esign = "+" if excess >= 0 else ""
        msg += f"📊 vs {result['bench_name']}\n"
        msg += f"   {result['bench_name']}: {bsign}{result['bench_return']:.2f}%\n"
        msg += f"   초과 수익: {esign}{excess:.2f}%p\n\n"

    # 리스크
    msg += "⚠️ 리스크\n"
    msg += f"   최대 낙폭 (MDD): {result['mdd']:.2f}% ({result['mdd_date']})\n"
    msg += f"   연환산 변동성: {result['annual_vol']:.2f}%\n"
    msg += f"   샤프 비율: {result['sharpe']:.2f}\n"
    msg += "\n💡 과거 수익률이 미래를 보장하지 않습니다."
    return msg
