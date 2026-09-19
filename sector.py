"""업종 평균 캐시 및 업종 상대평가"""

import time
from datetime import datetime
from config import logger
from screening import SCREENING_UNIVERSE, fetch_stock_quick


# ============================================================
# 업종 평균 캐시
# ============================================================

SECTOR_CACHE = {}  # {"Technology": {"per": 18.2, "pbr": 2.8, "roe": 14.5, ...}}

SECTOR_CACHE_DATE = None  # 마지막 갱신 날짜

# 영문 sector → 한글 매핑
SECTOR_KO = {
    "Technology":             "기술",
    "Financial Services":     "금융",
    "Healthcare":             "헬스케어",
    "Consumer Cyclical":      "경기소비재",
    "Consumer Defensive":     "필수소비재",
    "Industrials":            "산업재",
    "Energy":                 "에너지",
    "Basic Materials":        "소재",
    "Communication Services": "커뮤니케이션",
    "Real Estate":            "부동산",
    "Utilities":              "유틸리티",
}

def build_sector_cache():
    """스크리닝 유니버스에서 업종별 평균 지표 계산."""
    global SECTOR_CACHE_DATE

    if not SCREENING_UNIVERSE:
        logger.warning("스크리닝 유니버스가 비어있어 업종 캐시 생성 불가")
        return

    logger.info("업종 평균 캐시 계산 중... (시간 소요)")
    sector_data = {}  # {sector: [{"per", "pbr", "roe", ...}]}

    for item in SCREENING_UNIVERSE:
        try:
            data = fetch_stock_quick(item)
            if not data or not data.get("sector"):
                continue
            sector = data["sector"]
            if sector not in sector_data:
                sector_data[sector] = []
            sector_data[sector].append(data)
            time.sleep(0.2)
        except Exception:
            continue

    # 업종별 평균 계산
    cache = {}
    for sector, items in sector_data.items():
        def avg(key):
            vals = [d[key] for d in items if d.get(key) and d[key] > 0]
            return round(sum(vals) / len(vals), 2) if vals else None

        cache[sector] = {
            "per":              avg("pe_ratio"),
            "pbr":              avg("pb_ratio"),
            "roe":              avg("roe"),
            "roa":              avg("roa"),
            "operating_margin": avg("operating_margin"),
            "debt_to_equity":   avg("debt_to_equity"),
            "dividend_yield":   avg("dividend_yield"),
            "count":            len(items),
            "ko_name":          SECTOR_KO.get(sector, sector),
        }

    SECTOR_CACHE.clear()
    SECTOR_CACHE.update(cache)
    SECTOR_CACHE_DATE = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"업종 캐시 완료: {len(cache)}개 업종, {sum(v['count'] for v in cache.values())}개 종목")

def get_sector_comparison(sector: str, data: dict) -> list[str]:
    """업종 평균 대비 비교 텍스트 반환."""
    if not SECTOR_CACHE or sector not in SECTOR_CACHE:
        return []

    avg = SECTOR_CACHE[sector]
    ko_name = avg.get("ko_name", sector)
    lines = [f"📊 업종 상대평가 ({ko_name} 업종 {avg['count']}개사 평균)"]

    def compare(label, val, avg_val, lower_is_better=False):
        if val is None or avg_val is None or avg_val == 0:
            return None
        diff = ((val - avg_val) / avg_val) * 100
        if lower_is_better:
            if diff < -20:
                grade = "★★★ 매우 저평가"
            elif diff < -5:
                grade = "★★ 저평가"
            elif diff < 5:
                grade = "★ 적정"
            elif diff < 20:
                grade = "고평가"
            else:
                grade = "⚠️ 매우 고평가"
        else:
            if diff > 20:
                grade = "★★★ 우수"
            elif diff > 5:
                grade = "★★ 양호"
            elif diff > -5:
                grade = "★ 적정"
            elif diff > -20:
                grade = "하회"
            else:
                grade = "⚠️ 크게 하회"

        sign = "+" if diff >= 0 else ""
        return f"   • {label}: {val:.1f} vs 평균 {avg_val:.1f} ({sign}{diff:.0f}%) → {grade}"

    # PER (낮을수록 저평가)
    line = compare("PER", data.get("pe_ratio"), avg.get("per"), lower_is_better=True)
    if line:
        lines.append(line)

    # PBR (낮을수록 저평가)
    line = compare("PBR", data.get("pb_ratio"), avg.get("pbr"), lower_is_better=True)
    if line:
        lines.append(line)

    # ROE (높을수록 우수)
    roe = data.get("roe")
    if roe and roe < 1:
        roe = roe * 100
    line = compare("ROE", roe, avg.get("roe"), lower_is_better=False)
    if line:
        lines.append(line)

    # 영업이익률 (높을수록 우수)
    op = data.get("operating_margin")
    if op and op < 1:
        op = op * 100
    line = compare("영업이익률", op, avg.get("operating_margin"), lower_is_better=False)
    if line:
        lines.append(line)

    return lines if len(lines) > 1 else []
