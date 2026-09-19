"""팩터 스코어링/메시지 포맷팅"""

import re
import numpy as np
from data import get_kor_stock_data, get_us_stock_data, is_holding_company, search_kor_stock
from sector import SECTOR_CACHE, get_sector_comparison


def score_value(data):
    """밸류 팩터 (가중치 기반).
    Forward PER 20% / PER 20% / PEG 15% / PBR 20% / EV/EBITDA 10% / EPS성장률 10% / PSR 5%
    Forward PER 없으면 PEG에 가중치 합산 (15→35%)
    """
    weighted_scores = []  # [(score, weight)]
    details = []

    has_forward_pe = False

    # 1. PER (20%)
    pe = data.get("pe_ratio")
    if pe and pe > 0:
        if pe < 5:    s, g = 95, "매우 저평가"
        elif pe < 10: s, g = 85, "저평가"
        elif pe < 15: s, g = 70, "적정 저평가"
        elif pe < 20: s, g = 55, "적정"
        elif pe < 30: s, g = 35, "다소 비쌈"
        else:         s, g = 15, "고평가"
        weighted_scores.append((s, 20))
        details.append(f"PER: {pe:.2f}배 ({g})")
    elif pe and pe < 0:
        details.append(f"PER: {pe:.2f}배 (적자)")

    # 2. Forward PER (20%, 없으면 PEG에 합산)
    fpe = data.get("forward_pe")
    if fpe and fpe > 0:
        has_forward_pe = True
        if fpe < 5:    s, g = 95, "매우 저평가"
        elif fpe < 10: s, g = 85, "저평가"
        elif fpe < 15: s, g = 70, "적정 저평가"
        elif fpe < 20: s, g = 55, "적정"
        elif fpe < 30: s, g = 35, "다소 비쌈"
        else:          s, g = 15, "고평가"
        weighted_scores.append((s, 20))
        details.append(f"Forward PER: {fpe:.2f}배 ({g}) ★")

        # Trailing vs Forward 비교
        if pe and pe > 0 and fpe > 0:
            if fpe < pe * 0.9:
                details.append(f"  → 실적 개선 기대 ({pe:.1f}배 → {fpe:.1f}배)")
            elif fpe > pe * 1.1:
                details.append(f"  → 실적 둔화 우려 ({pe:.1f}배 → {fpe:.1f}배)")
    elif fpe and fpe < 0:
        details.append(f"Forward PER: {fpe:.2f}배 (적자 예상)")

    # 3. PEG (15%, Forward PER 없으면 35%)
    eps_growth = None
    eps = data.get("eps")
    feps = data.get("forward_eps")
    if eps and feps and eps > 0 and feps > 0:
        eps_growth = ((feps - eps) / abs(eps)) * 100

    peg_weight = 35 if not has_forward_pe else 15
    if pe and pe > 0 and eps_growth and eps_growth > 0:
        peg = pe / eps_growth
        if peg < 0.5:   s, g = 95, "매우 저평가"
        elif peg < 1.0:  s, g = 80, "저평가"
        elif peg < 1.5:  s, g = 60, "적정"
        elif peg < 2.0:  s, g = 40, "다소 비쌈"
        else:            s, g = 20, "고평가"
        weighted_scores.append((s, peg_weight))
        star = "★★" if peg_weight > 15 else "★"
        details.append(f"PEG: {peg:.2f} ({g}) {star}")
    elif pe and pe > 0 and eps_growth and eps_growth <= 0:
        details.append("PEG: 산출불가 (EPS 감소 중)")

    # 4. PBR (20%)
    pb = data.get("pb_ratio")
    if pb and pb > 0:
        if pb < 0.7:   s, g = 95, "매우 저평가"
        elif pb < 1.0: s, g = 85, "저평가"
        elif pb < 2.0: s, g = 65, "적정"
        elif pb < 3.0: s, g = 45, "다소 비쌈"
        elif pb < 5.0: s, g = 30, "비쌈"
        else:          s, g = 15, "고평가"
        weighted_scores.append((s, 20))
        details.append(f"PBR: {pb:.2f}배 ({g})")

    # 5. EV/EBITDA (10%)
    ev_ebitda = data.get("ev_ebitda")
    if ev_ebitda and ev_ebitda > 0:
        if ev_ebitda < 6:    s, g = 95, "매우 저평가"
        elif ev_ebitda < 10: s, g = 80, "저평가"
        elif ev_ebitda < 15: s, g = 60, "적정"
        elif ev_ebitda < 20: s, g = 40, "다소 비쌈"
        else:                s, g = 20, "고평가"
        weighted_scores.append((s, 10))
        details.append(f"EV/EBITDA: {ev_ebitda:.2f}배 ({g})")

    # 6. EPS 성장률 (10%)
    if eps_growth is not None:
        if eps_growth > 30:    s, g = 95, "고성장"
        elif eps_growth > 15:  s, g = 80, "성장"
        elif eps_growth > 5:   s, g = 65, "완만한 성장"
        elif eps_growth > -5:  s, g = 50, "보합"
        elif eps_growth > -15: s, g = 30, "감익"
        else:                  s, g = 15, "급감익"
        weighted_scores.append((s, 10))
        sign = "+" if eps_growth >= 0 else ""
        details.append(f"EPS 성장률: {sign}{eps_growth:.1f}% ({g}) ★")
    elif eps:
        details.append(f"EPS: {eps:.2f} (Forward EPS 데이터 없음)")

    # 7. PSR (5%)
    ps = data.get("ps_ratio")
    if ps and ps > 0:
        if ps < 1.0:   s = 85
        elif ps < 2.0: s = 65
        elif ps < 5.0: s = 45
        else:          s = 25
        weighted_scores.append((s, 5))
        details.append(f"PSR: {ps:.2f}배")

    if not weighted_scores:
        return 0, ["데이터 부족"]

    # 가중 평균 계산
    total_weight = sum(w for _, w in weighted_scores)
    weighted_sum = sum(s * w for s, w in weighted_scores)
    final_score = int(weighted_sum / total_weight)

    return final_score, details

def score_quality(data):
    """퀄리티 팩터 (가중치 기반).
    ROE 25% / 부채비율 25% / 영업이익률 25% / 이자보상배율 15% / EPS흑자 10%
    """
    weighted_scores = []  # [(score, weight)]
    details = []
    holding = is_holding_company(data)

    # 1. ROE (25%)
    roe = data.get("roe")
    if roe is not None:
        roe_pct = roe * 100 if abs(roe) < 1 else roe
        if roe_pct > 25:    s, g = 95, "매우 우수"
        elif roe_pct > 20:  s, g = 85, "우수"
        elif roe_pct > 15:  s, g = 75, "양호"
        elif roe_pct > 10:  s, g = 60, "적정"
        elif roe_pct > 5:   s, g = 40, "평범"
        elif roe_pct > 0:   s, g = 25, "낮음"
        else:               s, g = 10, "적자"
        weighted_scores.append((s, 25))
        details.append(f"ROE: {roe_pct:.2f}% ({g})")

    # 2. 부채비율 (25%)
    debt = data.get("debt_to_equity")
    if debt is not None:
        if holding:
            if debt < 100:    s, g = 80, "안정"
            elif debt < 200:  s, g = 65, "보통"
            elif debt < 400:  s, g = 45, "높음"
            else:             s, g = 25, "매우 높음"
            details.append(f"부채비율: {debt:.0f}% ({g}, 지주/금융 기준)")
        else:
            if debt < 20:     s, g = 95, "매우 안정"
            elif debt < 50:   s, g = 80, "안정"
            elif debt < 100:  s, g = 60, "보통"
            elif debt < 200:  s, g = 35, "높음"
            elif debt < 300:  s, g = 20, "매우 높음"
            else:             s, g = 10, "위험"
            details.append(f"부채비율: {debt:.0f}% ({g})")
        weighted_scores.append((s, 25))

    # 3. 영업이익률 (25%)
    op = data.get("operating_margin")
    if op is not None:
        op_pct = op * 100 if abs(op) < 1 else op
        if holding:
            details.append(f"영업이익률: {op_pct:.2f}% (지주/금융 특성상 점수 제외)")
        else:
            if op_pct > 25:    s = 95
            elif op_pct > 20:  s = 85
            elif op_pct > 15:  s = 70
            elif op_pct > 10:  s = 55
            elif op_pct > 5:   s = 40
            elif op_pct > 0:   s = 25
            else:              s = 10
            weighted_scores.append((s, 25))
            details.append(f"영업이익률: {op_pct:.2f}%")

    # 4. 이자보상배율 (15%)
    ic = data.get("interest_coverage")
    if ic is not None:
        if ic > 15:    s, g = 95, "매우 안전"
        elif ic > 10:  s, g = 85, "안전"
        elif ic > 5:   s, g = 70, "양호"
        elif ic > 3:   s, g = 55, "보통"
        elif ic > 1:   s, g = 30, "주의"
        else:          s, g = 10, "위험 ⚠️"
        weighted_scores.append((s, 15))
        details.append(f"이자보상배율: {ic:.1f}배 ({g})")

    # 5. EPS 흑자 여부 (10%)
    eps = data.get("eps")
    if eps is not None:
        if eps > 0:
            s, g = 80, "흑자"
            details.append(f"EPS: {eps:.2f} ({g})")
        else:
            s, g = 10, "적자"
            details.append(f"EPS: {eps:.2f} ({g}) ⚠️")
        weighted_scores.append((s, 10))

    if not weighted_scores:
        return 0, ["데이터 부족"]

    # 가중 평균 계산
    total_weight = sum(w for _, w in weighted_scores)
    weighted_sum = sum(s * w for s, w in weighted_scores)
    final_score = int(weighted_sum / total_weight)

    return final_score, details

def score_momentum(data):
    """모멘텀 팩터 (가중치 기반).
    수급 20% / MA정배열 25% / 6M수익률 10% / 3M수익률 10% / 1M수익률 10%
    RSI 10% / 거래량 10% / MACD 5%
    """
    weighted_scores = []  # [(score, weight)]
    details = []
    hist = data.get("history")

    if hist is None or hist.empty or len(hist) < 20:
        return 0, ["데이터 부족"]

    current = hist["Close"].iloc[-1]

    # 1) 수익률 모멘텀 (1M 10% + 3M 10% + 6M 10%)
    details.append("📈 수익률")
    ret_map = {}
    for label, days, weight in [("1M", 21, 10), ("3M", 63, 10), ("6M", 126, 10)]:
        if len(hist) >= days:
            past = hist["Close"].iloc[-days]
            ret = ((current - past) / past) * 100
            ret_map[label] = ret
            sign = "+" if ret >= 0 else ""
            details.append(f"   • {label}: {sign}{ret:.2f}%")
            if ret > 30:    s = 95
            elif ret > 15:  s = 80
            elif ret > 5:   s = 65
            elif ret > -5:  s = 50
            elif ret > -15: s = 35
            elif ret > -30: s = 20
            else:           s = 10
            weighted_scores.append((s, weight))

    # 2) RSI 14일 (10%)
    if len(hist) >= 14:
        delta = hist["Close"].diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, float("inf"))
        rsi = (100 - (100 / (1 + rs))).iloc[-1]
        details.append(f"\n📊 RSI (14일): {rsi:.1f}")
        if rsi < 30:
            s = 85
            details.append("   • 과매도 구간 → 매수 관심 ★")
        elif rsi < 45:
            s = 65
            details.append("   • 약세 구간")
        elif rsi < 55:
            s = 55
            details.append("   • 중립 구간")
        elif rsi < 70:
            s = 70
            details.append("   • 강세 구간 ★")
        else:
            s = 35
            details.append("   • 과매수 구간 → 주의 ⚠️")
        weighted_scores.append((s, 10))

    # 3) 52주 위치 (참고 표시, 점수 미반영)
    high_52 = hist["High"].max()
    low_52 = hist["Low"].min()
    if high_52 > low_52:
        position = ((current - low_52) / (high_52 - low_52)) * 100
        details.append(f"\n📍 52주 위치: {position:.1f}%")
        details.append(f"   • 저점 {low_52:,.0f} ~ 고점 {high_52:,.0f}")
        if position >= 80:
            details.append("   • 52주 고점 근처 (강한 상승 추세)")
        elif position >= 60:
            details.append("   • 상단 영역 (상승 추세)")
        elif position >= 40:
            details.append("   • 중간 영역")
        elif position >= 20:
            details.append("   • 하단 영역 (약세)")
        else:
            details.append("   • 52주 저점 근처 ⚠️")

    # 4) 거래량 모멘텀 (10%)
    if len(hist) >= 20:
        vol_5 = hist["Volume"].iloc[-5:].mean()
        vol_20 = hist["Volume"].iloc[-20:].mean()
        if vol_20 > 0:
            vol_ratio = vol_5 / vol_20
            details.append(f"\n📦 거래량 모멘텀: {vol_ratio:.2f}x (5일/20일 평균)")
            if vol_ratio >= 2.0:
                s = 85
                details.append("   • 거래량 급증 (강한 관심) ★")
            elif vol_ratio >= 1.3:
                s = 70
                details.append("   • 거래량 증가 (관심 상승)")
            elif vol_ratio >= 0.7:
                s = 50
                details.append("   • 거래량 보통")
            else:
                s = 30
                details.append("   • 거래량 감소 (관심 하락)")
            weighted_scores.append((s, 10))

    # 5) MA 정배열 (25%)
    if len(hist) >= 120:
        ma20 = hist["Close"].rolling(20).mean().iloc[-1]
        ma60 = hist["Close"].rolling(60).mean().iloc[-1]
        ma120 = hist["Close"].rolling(120).mean().iloc[-1]
        details.append("\n📊 이동평균선")
        details.append(f"   • MA20: {ma20:,.0f} | MA60: {ma60:,.0f} | MA120: {ma120:,.0f}")

        if ma20 > ma60 > ma120 and current > ma20:
            s = 95
            details.append("   • 완전 정배열 (강한 상승 추세) ★")
        elif ma20 > ma60 > ma120:
            s = 80
            details.append("   • 정배열 (상승 추세)")
        elif ma20 > ma60 and current > ma20:
            s = 65
            details.append("   • 단기 상승 추세")
        elif ma20 < ma60 < ma120 and current < ma20:
            s = 10
            details.append("   • 완전 역배열 (강한 하락 추세) ⚠️")
        elif ma20 < ma60 < ma120:
            s = 25
            details.append("   • 역배열 (하락 추세)")
        elif ma20 < ma60 and current < ma20:
            s = 35
            details.append("   • 단기 하락 추세")
        else:
            s = 50
            details.append("   • 혼조 (방향성 불분명)")
        weighted_scores.append((s, 25))
    elif len(hist) >= 60:
        ma20 = hist["Close"].rolling(20).mean().iloc[-1]
        ma60 = hist["Close"].rolling(60).mean().iloc[-1]
        details.append("\n📊 이동평균선 (단기)")
        details.append(f"   • MA20: {ma20:,.0f} | MA60: {ma60:,.0f}")
        if ma20 > ma60 and current > ma20:
            s = 75
            details.append("   • 단기 정배열 (상승 추세)")
        elif ma20 < ma60 and current < ma20:
            s = 25
            details.append("   • 단기 역배열 (하락 추세)")
        else:
            s = 50
            details.append("   • 혼조")
        weighted_scores.append((s, 25))

    # 6) MACD (5%)
    if len(hist) >= 35:
        ema12 = hist["Close"].ewm(span=12, adjust=False).mean()
        ema26 = hist["Close"].ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        histogram = macd_line - signal_line

        macd_now = macd_line.iloc[-1]
        signal_now = signal_line.iloc[-1]
        hist_now = histogram.iloc[-1]
        hist_prev = histogram.iloc[-2] if len(histogram) >= 2 else 0

        details.append("\n📈 MACD")
        details.append(f"   • MACD: {macd_now:,.1f} | Signal: {signal_now:,.1f}")

        if hist_prev < 0 and hist_now >= 0:
            s = 90
            details.append("   • 골든크로스 발생! (강한 매수 신호) ★")
        elif hist_prev >= 0 and hist_now < 0:
            s = 15
            details.append("   • 데드크로스 발생 (매도 신호) ⚠️")
        elif macd_now > signal_now and hist_now > hist_prev:
            s = 80
            details.append("   • MACD > Signal 상승세 (강세 지속)")
        elif macd_now > signal_now:
            s = 65
            details.append("   • MACD > Signal (상승 추세)")
        elif macd_now < signal_now and hist_now < hist_prev:
            s = 25
            details.append("   • MACD < Signal 하락세 (약세 지속)")
        else:
            s = 40
            details.append("   • MACD < Signal (하락 추세)")
        weighted_scores.append((s, 5))

    # 7) 수급 (20%)
    market = data.get("market", "")

    if market == "KR":
        # 한국: KIS API 외국인/기관 순매수
        foreigner = data.get("foreigner_net")
        institution = data.get("institution_net")
        if foreigner is not None or institution is not None:
            details.append("\n👥 외국인/기관 수급")
            fg_str = ""
            if foreigner is not None:
                fg_sign = "▲" if foreigner > 0 else "▼"
                fg_str = f"외국인 {fg_sign}{abs(foreigner):,}주"
            inst_str = ""
            if institution is not None:
                inst_sign = "▲" if institution > 0 else "▼"
                inst_str = f"기관 {inst_sign}{abs(institution):,}주"
            if fg_str and inst_str:
                details.append(f"   • {fg_str} / {inst_str}")
            elif fg_str:
                details.append(f"   • {fg_str}")
            elif inst_str:
                details.append(f"   • {inst_str}")

            if foreigner is not None and institution is not None:
                if foreigner > 0 and institution > 0:
                    s = 85
                    details.append("   • 외국인+기관 동반 순매수 ★")
                elif foreigner > 0 or institution > 0:
                    s = 65
                    details.append("   • 외국인/기관 순매수")
                elif foreigner < 0 and institution < 0:
                    s = 25
                    details.append("   • 외국인+기관 동반 순매도 ⚠️")
                else:
                    s = 45
                    details.append("   • 수급 혼조")
            elif foreigner is not None:
                s = 65 if foreigner > 0 else 35
            else:
                s = 65 if institution > 0 else 35
            weighted_scores.append((s, 20))

    elif market == "US":
        # 미국: 기관보유 + 공매도 + 내부자 (수급 대체 지표)
        inst_pct = data.get("institutional_pct")
        short_ratio = data.get("short_ratio")
        insider_pct = data.get("insider_pct")

        if inst_pct is not None or short_ratio is not None:
            details.append("\n👥 수급 대체 지표 (기관/공매도)")
            sub_scores = []

            # 기관 보유비율 (40% of 수급)
            if inst_pct is not None:
                inst_pct_val = inst_pct * 100 if inst_pct < 1 else inst_pct
                if inst_pct_val >= 80:    s = 90
                elif inst_pct_val >= 60:  s = 75
                elif inst_pct_val >= 40:  s = 55
                elif inst_pct_val >= 20:  s = 35
                else:                     s = 20
                sub_scores.append((s, 40))
                details.append(f"   • 기관 보유: {inst_pct_val:.1f}%")

            # 공매도 비율 (30% of 수급)
            if short_ratio is not None:
                if short_ratio < 1:      s = 80
                elif short_ratio < 2:    s = 65
                elif short_ratio < 5:    s = 45
                elif short_ratio < 10:   s = 30
                else:                    s = 15
                sub_scores.append((s, 30))
                details.append(f"   • 공매도 비율: {short_ratio:.1f}일")

            # 내부자 보유비율 (30% of 수급)
            if insider_pct is not None:
                insider_val = insider_pct * 100 if insider_pct < 1 else insider_pct
                if insider_val >= 10:    s = 80
                elif insider_val >= 5:   s = 65
                elif insider_val >= 1:   s = 50
                else:                    s = 40
                sub_scores.append((s, 30))
                details.append(f"   • 내부자 보유: {insider_val:.1f}%")

            if sub_scores:
                total_w = sum(w for _, w in sub_scores)
                supply_score = int(sum(s * w for s, w in sub_scores) / total_w)
                weighted_scores.append((supply_score, 20))

                if supply_score >= 70:
                    details.append("   • 수급 양호 (기관 보유 높음)")
                elif supply_score >= 50:
                    details.append("   • 수급 보통")
                else:
                    details.append("   • 수급 부정적 (기관 이탈/숏 많음) ⚠️")

    if not weighted_scores:
        return 0, ["데이터 부족"]

    # 가중 평균 계산
    total_weight = sum(w for _, w in weighted_scores)
    weighted_sum = sum(s * w for s, w in weighted_scores)
    final_score = int(weighted_sum / total_weight)

    return final_score, details

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
        warnings.append("EPS 적자")

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
    msg += "   (밸류40% + 퀄리티40% + 모멘텀20%)\n"

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

    # 업종 상대평가 (캐시 있을 때만)
    sector = data.get("sector", "")
    if sector and SECTOR_CACHE:
        sector_lines = get_sector_comparison(sector, data)
        if sector_lines:
            msg += "\n"
            for line in sector_lines:
                msg += f"{line}\n"

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
