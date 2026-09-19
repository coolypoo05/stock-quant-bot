"""상관계수 분석 + 팩터 비교"""

import io
import re
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import yfinance as yf
from data import get_kor_stock_data, get_us_stock_data, load_stock_map, search_kor_stock
from config import STOCK_MAP, logger
from scoring import calc_weighted_overall, check_risk_warnings, grade_score, score_momentum, score_quality, score_value


# ============================================================
# 상관계수 분석
# ============================================================

PERIOD_MAP = {
    "6m": ("6mo", "6개월"),
    "1y": ("1y",  "1년"),
    "3y": ("3y",  "3년"),
}

def fetch_prices_for_corr(query: str, period: str) -> tuple[str, str, pd.Series | None]:
    """종목 가격 데이터 수집. (ticker, name, prices) 반환."""
    # STOCK_MAP 로딩 확인
    if not STOCK_MAP:
        load_stock_map()

    # 숫자 6자리 → 한국 주식만 시도
    if re.fullmatch(r"\d{6}", query):
        result = search_kor_stock(query)
        if result:
            code, name, suffix = result
            ticker = code + suffix
            try:
                t = yf.Ticker(ticker)
                hist = t.history(period=period)
                if hist is not None and not hist.empty:
                    return ticker, name, hist["Close"].dropna()
            except Exception:
                pass
        return query, query, None

    # 영문 → 미국 주식 우선 시도
    if re.fullmatch(r"[A-Za-z.\-]{1,10}", query):
        ticker = query.upper()
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period=period)
            if hist is not None and not hist.empty:
                info = t.info or {}
                name = info.get("shortName") or info.get("longName") or ticker
                return ticker, name, hist["Close"].dropna()
        except Exception:
            pass

    # 한글 또는 영문 검색 실패 → 한국 주식 STOCK_MAP 검색
    result = search_kor_stock(query)
    if result:
        code, name, suffix = result
        ticker = code + suffix
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period=period)
            if hist is not None and not hist.empty:
                return ticker, name, hist["Close"].dropna()
        except Exception:
            pass

    return query, query, None

def run_correlation(queries: list[str], period: str) -> dict | None:
    """상관계수 분석 실행."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    yf_period, period_label = PERIOD_MAP.get(period, ("1y", "1년"))

    # 병렬로 가격 데이터 수집
    results = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_map = {executor.submit(fetch_prices_for_corr, q, yf_period): q for q in queries}
        for future in as_completed(future_map):
            q = future_map[future]
            try:
                ticker, name, prices = future.result()
                if prices is not None and len(prices) > 10:
                    results[q] = {"ticker": ticker, "name": name, "prices": prices}
            except Exception as e:
                logger.warning(f"상관계수 데이터 수집 실패 ({q}): {e}")

    if len(results) < 2:
        return None

    # 일별 수익률 계산
    names = []
    returns_list = []
    prices_list = []

    for q in queries:
        if q in results:
            r = results[q]
            names.append(r["name"])
            prices_list.append(r["prices"])
            returns_list.append(r["prices"].pct_change().dropna())

    # 공통 날짜로 정렬
    # 타임존 제거 후 날짜만 사용 (한국/미국 혼용 시 타임존 차이 해결)
    for i in range(len(returns_list)):
        returns_list[i].index = pd.to_datetime(returns_list[i].index).tz_localize(None).normalize()
        prices_list[i].index = pd.to_datetime(prices_list[i].index).tz_localize(None).normalize()

    common_idx = returns_list[0].index
    for ret in returns_list[1:]:
        common_idx = common_idx.intersection(ret.index)

    if len(common_idx) < 20:
        return None

    aligned_returns = [ret.loc[common_idx] for ret in returns_list]
    aligned_prices = [p.reindex(common_idx).ffill() for p in prices_list]

    # 상관계수 행렬
    df_returns = pd.DataFrame({names[i]: aligned_returns[i] for i in range(len(names))})
    corr_matrix = df_returns.corr()

    # 개별 수익률
    individual_returns = []
    for i, name in enumerate(names):
        p = aligned_prices[i]
        ret = ((float(p.iloc[-1]) - float(p.iloc[0])) / float(p.iloc[0])) * 100
        individual_returns.append((name, ret))

    return {
        "names": names,
        "period_label": period_label,
        "corr_matrix": corr_matrix,
        "aligned_prices": aligned_prices,
        "individual_returns": individual_returns,
        "start_date": str(common_idx[0].date()),
        "end_date": str(common_idx[-1].date()),
        "n_days": len(common_idx),
    }

def create_correlation_chart(result: dict) -> bytes:
    """수익률 비교 차트 + 상관계수 히트맵."""
    names = result["names"]
    n = len(names)
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    try:
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass

    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
              "#8c564b", "#e377c2", "#7f7f7f"]

    # 왼쪽: 정규화 수익률 비교
    ax1 = axes[0]
    for i, (name, prices) in enumerate(zip(names, result["aligned_prices"])):
        norm = (prices / prices.iloc[0]) * 100
        ax1.plot(norm.index, norm.values, label=name,
                 color=colors[i % len(colors)], linewidth=2)

    ax1.set_title(f"수익률 비교 ({result['period_label']})", fontsize=12, fontweight="bold")
    ax1.set_ylabel("정규화 수익률 (시작 = 100)")
    ax1.legend(loc="upper left", fontsize=9)
    ax1.grid(True, alpha=0.3)
    ax1.axhline(100, color="gray", linestyle="--", linewidth=0.8, alpha=0.5)

    # 오른쪽: 상관계수 히트맵
    ax2 = axes[1]
    corr = result["corr_matrix"].values
    im = ax2.imshow(corr, cmap="RdYlGn", vmin=-1, vmax=1, aspect="auto")
    plt.colorbar(im, ax=ax2, shrink=0.8)

    ax2.set_xticks(range(n))
    ax2.set_yticks(range(n))

    # 이름이 길면 줄임
    short_names = [name[:8] + ".." if len(name) > 8 else name for name in names]
    ax2.set_xticklabels(short_names, rotation=30, ha="right", fontsize=9)
    ax2.set_yticklabels(short_names, fontsize=9)
    ax2.set_title("상관계수 히트맵", fontsize=12, fontweight="bold")

    # 각 셀에 상관계수 값 표시
    for i in range(n):
        for j in range(n):
            val = corr[i][j]
            color = "black" if 0.3 < abs(val) < 0.8 else "white"
            ax2.text(j, i, f"{val:.2f}", ha="center", va="center",
                     fontsize=10, fontweight="bold", color=color)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf.read()

def format_correlation_message(result: dict) -> str:
    """상관계수 분석 결과 메시지."""
    names = result["names"]
    corr = result["corr_matrix"]

    msg = "📊 상관계수 분석\n"
    msg += "━━━━━━━━━━━━━━━\n"
    msg += f"📅 기간: {result['period_label']} ({result['start_date']} ~ {result['end_date']})\n"
    msg += f"   ({result['n_days']}거래일 기준)\n\n"

    # 개별 수익률
    msg += "📈 기간 수익률\n"
    for name, ret in result["individual_returns"]:
        sign = "+" if ret >= 0 else ""
        msg += f"   • {name}: {sign}{ret:.1f}%\n"
    msg += "\n"

    # 상관계수 쌍별 해석
    msg += "🔗 상관계수\n"
    pairs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            val = float(corr.iloc[i, j])
            pairs.append((names[i], names[j], val))

    for n1, n2, val in pairs:
        if val >= 0.8:
            grade = "매우 높음 🔴"
            comment = "거의 같이 움직임 (분산 효과 낮음)"
        elif val >= 0.6:
            grade = "높음 🟠"
            comment = "유사한 움직임"
        elif val >= 0.4:
            grade = "보통 🟡"
            comment = "어느 정도 연동"
        elif val >= 0.2:
            grade = "낮음 🟢"
            comment = "독립적 움직임"
        elif val >= -0.2:
            grade = "매우 낮음 🟢"
            comment = "거의 무관"
        else:
            grade = "음의 상관 🟢"
            comment = "반대로 움직임 (분산 효과 높음)"

        msg += f"   • {n1} ↔ {n2}\n"
        msg += f"     상관계수: {val:.3f} ({grade})\n"
        msg += f"     → {comment}\n"

    # 포트폴리오 분산 효과 평가
    avg_corr = sum(p[2] for p in pairs) / len(pairs) if pairs else 0
    msg += "\n⚖️ 포트폴리오 분산 효과\n"
    if avg_corr >= 0.7:
        msg += f"   평균 상관계수: {avg_corr:.2f} → 분산 효과 낮음 ⚠️\n"
        msg += "   💡 상관관계 낮은 종목 추가를 권장해요"
    elif avg_corr >= 0.4:
        msg += f"   평균 상관계수: {avg_corr:.2f} → 분산 효과 보통 🟡\n"
        msg += "   💡 추가 분산 투자 고려해보세요"
    else:
        msg += f"   평균 상관계수: {avg_corr:.2f} → 분산 효과 우수 🟢\n"
        msg += "   💡 잘 분산된 포트폴리오예요"

    msg += "\n\n💡 상관계수는 과거 데이터 기준이며 미래를 보장하지 않습니다."
    return msg

def run_compare(q1: str, q2: str) -> tuple:
    """두 종목 팩터 데이터 병렬 조회."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def fetch(query):
        query = query.strip()
        if re.fullmatch(r"[A-Za-z.\-]{1,10}", query):
            data = get_us_stock_data(query.upper())
            if data:
                return data
        result = search_kor_stock(query)
        if result:
            code, name, suffix = result
            return get_kor_stock_data(code, name, suffix)
        return None

    results = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_map = {
            executor.submit(fetch, q1): "d1",
            executor.submit(fetch, q2): "d2",
        }
        for future in as_completed(future_map):
            key = future_map[future]
            try:
                results[key] = future.result()
            except Exception:
                results[key] = None

    return results.get("d1"), results.get("d2")

def format_compare_result(data1: dict, data2: dict) -> str:
    """팩터 비교 결과 메시지 생성."""
    name1 = (data1.get("name") or "종목1")[:8]
    name2 = (data2.get("name") or "종목2")[:8]

    # 점수 계산
    v1, _ = score_value(data1)
    q1_s, _ = score_quality(data1)
    m1, _ = score_momentum(data1)
    total1 = calc_weighted_overall(v1, q1_s, m1)
    penalty1, _ = check_risk_warnings(data1)
    final1 = max(0, total1 - penalty1)

    v2, _ = score_value(data2)
    q2_s, _ = score_quality(data2)
    m2, _ = score_momentum(data2)
    total2 = calc_weighted_overall(v2, q2_s, m2)
    penalty2, _ = check_risk_warnings(data2)
    final2 = max(0, total2 - penalty2)

    wins1 = 0
    wins2 = 0

    def add_row(label, val1, val2, lower_better=False):
        nonlocal wins1, wins2
        if val1 is None and val2 is None:
            return ""
        s1 = "N/A" if val1 is None else f"{val1:.2f}"
        s2 = "N/A" if val2 is None else f"{val2:.2f}"
        w1 = w2 = False
        if val1 is not None and val2 is not None and abs(val1 - val2) > 0.01:
            w1 = (val1 < val2) if lower_better else (val1 > val2)
            w2 = not w1
        if w1: wins1 += 1
        if w2: wins2 += 1
        return f"  {label:<12} {s1:>8} {'🏆' if w1 else '  '}  {s2:>8} {'🏆' if w2 else '  '}\n"

    def to_pct(val):
        if val is None: return None
        return val * 100 if abs(val) < 1 else val

    msg = "📊 팩터 비교\n"
    msg += f"{'':>15} {name1:>10}    {name2:>10}\n"
    msg += "━━━━━━━━━━━━━━━\n"

    # 밸류
    msg += "💰 밸류\n"
    msg += add_row("PER",         data1.get("pe_ratio"),    data2.get("pe_ratio"),    lower_better=True)
    msg += add_row("Forward PER", data1.get("forward_pe"),  data2.get("forward_pe"),  lower_better=True)
    msg += add_row("PBR",         data1.get("pb_ratio"),    data2.get("pb_ratio"),    lower_better=True)
    msg += add_row("EV/EBITDA",   data1.get("ev_ebitda"),   data2.get("ev_ebitda"),   lower_better=True)
    msg += f"  {'밸류 점수':<12} {v1:>8}점    {v2:>8}점\n\n"

    # 퀄리티
    msg += "⚙️ 퀄리티\n"
    msg += add_row("ROE(%)",      to_pct(data1.get("roe")),              to_pct(data2.get("roe")),              lower_better=False)
    msg += add_row("영업이익률",  to_pct(data1.get("operating_margin")), to_pct(data2.get("operating_margin")), lower_better=False)
    msg += add_row("부채비율",    data1.get("debt_to_equity"),           data2.get("debt_to_equity"),           lower_better=True)
    msg += add_row("이자보상배율",data1.get("interest_coverage"),        data2.get("interest_coverage"),        lower_better=False)

    fs1 = data1.get("fscore_info")
    fs2 = data2.get("fscore_info")
    fs1v = fs1[0] if fs1 else None
    fs2v = fs2[0] if fs2 else None
    if fs1v is not None or fs2v is not None:
        w1 = (fs1v or 0) > (fs2v or 0)
        w2 = (fs2v or 0) > (fs1v or 0)
        if w1: wins1 += 1
        if w2: wins2 += 1
        msg += f"  {'F-Score':<12} {str(fs1v)+'/9' if fs1v else 'N/A':>8} {'🏆' if w1 else '  '}  {str(fs2v)+'/9' if fs2v else 'N/A':>8} {'🏆' if w2 else '  '}\n"
    msg += f"  {'퀄리티 점수':<12} {q1_s:>8}점    {q2_s:>8}점\n\n"

    # 모멘텀
    msg += "📈 모멘텀\n"
    hist1 = data1.get("history")
    hist2 = data2.get("history")

    def calc_ret(hist, days):
        if hist is None or hist.empty or len(hist) < days: return None
        return ((hist["Close"].iloc[-1] - hist["Close"].iloc[-days]) / hist["Close"].iloc[-days]) * 100

    for label, days in [("1M", 21), ("3M", 63), ("6M", 126)]:
        r1, r2 = calc_ret(hist1, days), calc_ret(hist2, days)
        if r1 is None and r2 is None: continue
        s1 = "N/A" if r1 is None else f"{r1:+.1f}%"
        s2 = "N/A" if r2 is None else f"{r2:+.1f}%"
        w1 = r1 is not None and r2 is not None and r1 > r2
        w2 = r1 is not None and r2 is not None and r2 > r1
        if w1: wins1 += 1
        if w2: wins2 += 1
        msg += f"  {label:<12} {s1:>8} {'🏆' if w1 else '  '}  {s2:>8} {'🏆' if w2 else '  '}\n"
    msg += f"  {'모멘텀 점수':<12} {m1:>8}점    {m2:>8}점\n\n"

    # 종합
    msg += "━━━━━━━━━━━━━━━\n"
    msg += "📊 종합\n"
    msg += f"  {'최종 점수':<12} {final1:>8}점    {final2:>8}점\n"
    g1, o1 = grade_score(final1)
    g2, o2 = grade_score(final2)
    msg += f"  {name1}: {g1} ({o1})\n"
    msg += f"  {name2}: {g2} ({o2})\n\n"

    if wins1 > wins2:
        msg += f"🏆 종합 우위: {name1} ({wins1}:{wins2})\n"
    elif wins2 > wins1:
        msg += f"🏆 종합 우위: {name2} ({wins2}:{wins1})\n"
    else:
        msg += f"🤝 팽팽한 접전! ({wins1}:{wins2})\n"

    msg += "\n💡 본 분석은 참고용이며, 투자 결정의 책임은 본인에게 있습니다."
    return msg
