"""
한국투자증권 KIS API 모듈 (모의투자)
- 국내 주식 현재가/기본 정보
- 재무 지표 (PER, PBR, EPS, ROE 등)
- 외국인/기관 수급
- 일봉 데이터
"""

import os
import time
import logging
import requests

# .env 파일 로딩 (로컬 테스트용)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

# ============================================================
# 설정
# ============================================================

APPKEY    = os.environ.get("KIS_APPKEY", "")
APPSECRET = os.environ.get("KIS_APPSECRET", "")
ACCOUNT   = os.environ.get("KIS_ACCOUNT", "")

if not APPKEY:
    logger.warning("KIS_APPKEY 환경변수가 비어있습니다.")

# 모의투자 도메인
BASE_URL = "https://openapivts.koreainvestment.com:29443"

_token_cache = {"token": None, "expires_at": 0}


# ============================================================
# 인증 (토큰 발급)
# ============================================================

def get_access_token() -> str | None:
    """액세스 토큰 발급 (캐싱 적용)."""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    if not APPKEY or not APPSECRET:
        logger.warning("KIS API 키가 설정되지 않았습니다.")
        return None

    url = f"{BASE_URL}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": APPKEY,
        "appsecret": APPSECRET,
    }
    try:
        res = requests.post(url, json=body, timeout=10)
        res.raise_for_status()
        data = res.json()
        token = data.get("access_token")
        expires_in = int(data.get("expires_in", 86400))
        if token:
            _token_cache["token"] = token
            _token_cache["expires_at"] = now + expires_in - 60
            logger.info("KIS API 토큰 발급 완료")
            time.sleep(0.5)  # 초당 거래건수 초과 방지
            return token
    except Exception as e:
        logger.error(f"KIS API 토큰 발급 실패: {e}")
    return None


def get_headers(tr_id: str) -> dict | None:
    """공통 헤더 생성."""
    token = get_access_token()
    if not token:
        return None
    return {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appKey": APPKEY,
        "appSecret": APPSECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }


# ============================================================
# 국내 주식 현재가 시세
# ============================================================

def get_price(code: str) -> dict | None:
    """주식 현재가 시세 조회 (FHKST01010100)."""
    try:
        data = _kis_get("FHKST01010100", "/uapi/domestic-stock/v1/quotations/inquire-price",
                        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
        if not data:
            return None
        output = data.get("output", {})
        return {
            "code": code,
            "price": int(output.get("stck_prpr", 0)),           # 현재가
            "open": int(output.get("stck_oprc", 0)),             # 시가
            "high": int(output.get("stck_hgpr", 0)),             # 고가
            "low": int(output.get("stck_lwpr", 0)),              # 저가
            "prev_close": int(output.get("stck_sdpr", 0)),       # 전일 종가
            "change": int(output.get("prdy_vrss", 0)),           # 전일 대비
            "change_rate": float(output.get("prdy_ctrt", 0)),    # 등락률
            "volume": int(output.get("acml_vol", 0)),            # 누적 거래량
            "volume_money": int(output.get("acml_tr_pbmn", 0)),  # 누적 거래대금
            "market_cap": int(output.get("hts_avls", 0)),        # 시가총액 (억원)
            "per": float(output.get("per", 0) or 0),             # PER
            "pbr": float(output.get("pbr", 0) or 0),             # PBR
            "eps": float(output.get("eps", 0) or 0),             # EPS
            "bps": float(output.get("bps", 0) or 0),             # BPS
            "week52_high": int(output.get("w52_hgpr", 0)),       # 52주 최고가
            "week52_low": int(output.get("w52_lwpr", 0)),        # 52주 최저가
            "dividend_rate": float(output.get("dvdn_yield", 0) or 0),  # 배당수익률
        }
    except Exception as e:
        logger.error(f"KIS 현재가 조회 오류 ({code}): {e}")
        return None


# ============================================================
# 국내 주식 재무 비율
# ============================================================

def get_financial_ratio(code: str) -> dict | None:
    """재무 비율 조회 (FHKST66430300) - ROE, 영업이익률 등."""
    try:
        data = _kis_get("FHKST66430300", "/uapi/domestic-stock/v1/finance/financial-ratio",
                        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                         "FID_DIV_CLS_CODE": "0"})  # 0: 연간
        if not data:
            return None
        output_list = data.get("output", [])
        if not output_list:
            return None
        # 가장 최근 연도 데이터
        output = output_list[0]
        return {
            "stac_yymm": output.get("stac_yymm", ""),           # 결산 연월
            "roe": float(output.get("roe_val", 0) or 0),         # ROE (%)
            "roa": None,                                          # KIS 미제공 → yfinance fallback
            "operating_margin": None,                            # KIS 미제공 → yfinance fallback
            "net_margin": None,                                   # KIS 미제공 → yfinance fallback
            "debt_ratio": float(output.get("lblt_rate", 0) or 0),  # 부채비율 (%)
            "current_ratio": None,                               # KIS 미제공 → yfinance fallback
            "interest_coverage": None,                           # KIS 미제공 → yfinance fallback
            "revenue_growth": float(output.get("grs", 0) or 0), # 매출성장률 (%)
            "op_income_growth": float(output.get("bsop_prfi_inrt", 0) or 0),  # 영업이익성장률
            "eps": float(output.get("eps", 0) or 0),             # EPS
            "bps": float(output.get("bps", 0) or 0),             # BPS
        }
    except Exception as e:
        logger.error(f"KIS 재무비율 조회 오류 ({code}): {e}")
        return None


# ============================================================
# 외국인/기관 수급
# ============================================================

def _kis_get(tr_id: str, path: str, params: dict, retries: int = 2) -> dict | None:
    """KIS GET 호출. 초당 거래건수 초과(EGW00201)면 잠시 후 재시도, 실패 시 None."""
    for attempt in range(retries + 1):
        headers = get_headers(tr_id)
        if not headers:
            return None
        res = requests.get(f"{BASE_URL}{path}", headers=headers, params=params, timeout=10)
        try:
            data = res.json()
        except ValueError:
            return None
        if data.get("msg_cd") == "EGW00201" and attempt < retries:
            time.sleep(1.0)
            continue
        if res.status_code != 200 or data.get("rt_cd") != "0":
            logger.warning(f"KIS 조회 실패 ({tr_id} {params.get('FID_INPUT_ISCD')}): {data.get('msg1')}")
            return None
        return data
    return None


def get_investor_trend(code: str) -> dict | None:
    """외국인/기관 순매수 동향 조회 (최근 30거래일 일별 행)."""
    try:
        # 시세 조회 계열은 모의/실전 모두 동일 TR_ID 사용
        data = _kis_get("FHKST01010900", "/uapi/domestic-stock/v1/quotations/inquire-investor",
                        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
        output = (data or {}).get("output") or []
        if not output:
            return None
        today = output[0]

        def total(key, days):  # 최신순 정렬된 일별 행을 days일 합산
            return sum(int(r.get(key, 0) or 0) for r in output[:days])

        return {
            "foreigner_net": int(today.get("frgn_ntby_qty", 0) or 0),
            "institution_net": int(today.get("orgn_ntby_qty", 0) or 0),
            "individual_net": int(today.get("prsn_ntby_qty", 0) or 0),
            # 누적 순매수 금액 (백만원)
            "foreigner_amt_5d": total("frgn_ntby_tr_pbmn", 5),
            "institution_amt_5d": total("orgn_ntby_tr_pbmn", 5),
            "foreigner_amt_20d": total("frgn_ntby_tr_pbmn", 20),
            "institution_amt_20d": total("orgn_ntby_tr_pbmn", 20),
            # 일별 (날짜, 외국인 순매수 수량, 기관 순매수 수량) - 거래량 대비 강도 계산용
            "flows": [(r["stck_bsop_date"], int(r.get("frgn_ntby_qty", 0) or 0), int(r.get("orgn_ntby_qty", 0) or 0))
                      for r in output[:20]],
        }
    except Exception as e:
        logger.error(f"KIS 수급 조회 오류 ({code}): {e}")
        return None


# ============================================================
# 일별 거래량
# ============================================================

def get_daily_volume(code: str) -> list | None:
    """최근 30거래일 일별 종가/거래량 (FHKST01010400)."""
    try:
        data = _kis_get("FHKST01010400", "/uapi/domestic-stock/v1/quotations/inquire-daily-price",
                        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                         "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
        rows = (data or {}).get("output") or []
        return [{"date": r["stck_bsop_date"], "close": int(r.get("stck_clpr", 0) or 0),
                 "volume": int(r.get("acml_vol", 0) or 0)} for r in rows] or None
    except Exception as e:
        logger.error(f"KIS 일별 거래량 조회 오류 ({code}): {e}")
        return None


def calc_flow_intensity(flows, daily, min_days: int = 15) -> tuple[float | None, float | None]:
    """(외국인+기관 순매수 수량 ÷ 거래량 %, 일평균 거래대금 억원). 날짜가 겹치는 일수가 부족하면 (None, None)."""
    if not flows or not daily:
        return None, None
    vols = {r["date"]: r for r in daily if r["volume"] > 0}
    matched = [(f + o, vols[d]) for d, f, o in flows if d in vols]
    if len(matched) < min_days:
        return None, None
    net = sum(n for n, _ in matched)
    volume = sum(v["volume"] for _, v in matched)
    adv_eok = sum(v["volume"] * v["close"] for _, v in matched) / len(matched) / 1e8
    return net / volume * 100, adv_eok


# ============================================================
# 통합 조회 (팩터 분석용)
# ============================================================

def get_full_stock_data(code: str) -> dict | None:
    """팩터 분석에 필요한 전체 데이터 통합 조회."""
    tasks = {
        "price":    lambda: get_price(code),
        "ratio":    lambda: get_financial_ratio(code),
        "investor": lambda: get_investor_trend(code),
        "daily":    lambda: get_daily_volume(code),
    }

    # KIS API 초당 호출 제한으로 순차 호출 (병렬 불가)
    results = {}
    for key, fn in tasks.items():
        try:
            results[key] = fn()
            time.sleep(0.2)  # 초당 5건 제한 대응
        except Exception as e:
            logger.debug(f"KIS 통합 조회 실패 ({key}): {e}")
            results[key] = None

    price = results.get("price")
    if not price:
        return None

    ratio = results.get("ratio") or {}
    investor = results.get("investor") or {}
    flow_ratio, adv_eok = calc_flow_intensity(investor.get("flows"), results.get("daily"))

    return {
        # 기본 정보
        "code": code,
        "name": code,  # 종목명은 STOCK_MAP에서 가져옴
        "sector": "",
        "market": "KR",
        "currency": "KRW",
        # 가격
        "price": price["price"],
        "previous_close": price["prev_close"],
        "open": price["open"],
        "high": price["high"],
        "low": price["low"],
        "volume": price["volume"],
        "market_cap": price["market_cap"],  # 억원
        "week52_high": price["week52_high"],
        "week52_low": price["week52_low"],
        # 밸류 지표 (현재가 API에서)
        "pe_ratio": price["per"] if price["per"] > 0 else None,
        "pb_ratio": price["pbr"] if price["pbr"] > 0 else None,
        "eps": price["eps"] if price["eps"] != 0 else None,
        "bps": price["bps"] if price["bps"] != 0 else None,
        "dividend_yield": price["dividend_rate"] if price["dividend_rate"] > 0 else None,
        # 재무 비율 (재무비율 API에서)
        "roe": ratio.get("roe"),
        "roa": ratio.get("roa"),
        "operating_margin": ratio.get("operating_margin"),
        "net_margin": ratio.get("net_margin"),
        "debt_to_equity": ratio.get("debt_ratio"),
        "current_ratio": ratio.get("current_ratio"),
        "interest_coverage": ratio.get("interest_coverage"),
        "revenue_growth": ratio.get("revenue_growth"),
        # 수급
        "foreigner_net": investor.get("foreigner_net"),
        "institution_net": investor.get("institution_net"),
        "individual_net": investor.get("individual_net"),
        "foreigner_amt_5d": investor.get("foreigner_amt_5d"),
        "institution_amt_5d": investor.get("institution_amt_5d"),
        "foreigner_amt_20d": investor.get("foreigner_amt_20d"),
        "institution_amt_20d": investor.get("institution_amt_20d"),
        "flow_ratio_20d": flow_ratio,  # (외국인+기관 순매수 수량 / 거래량) %, 20거래일
        "adv_eok_20d": adv_eok,        # 일평균 거래대금 (억원)
        # yfinance 호환용 (None으로 채움 → fallback)
        "forward_pe": None,
        "forward_eps": None,
        "ps_ratio": None,
        "ev_ebitda": None,
        "payout_ratio": None,
        "beta": None,
        "history": None,  # 필요 시 get_daily_prices로 별도 조회
        "industry": "",
        "fscore_info": None,
        "dividend_growth": {},
    }


# ============================================================
# 연결 테스트
# ============================================================

def test_connection() -> bool:
    """KIS API 연결 테스트."""
    token = get_access_token()
    if token:
        logger.info("KIS API 연결 성공!")
        return True
    logger.error("KIS API 연결 실패!")
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if test_connection():
        # 삼성전자 테스트
        data = get_full_stock_data("005930")
        if data:
            print(f"삼성전자 현재가: {data['price']:,}원")
            print(f"PER: {data['pe_ratio']}")
            print(f"ROE: {data['roe']}%")
            print(f"부채비율: {data['debt_to_equity']}%")
            if data['foreigner_net'] is not None:
                print(f"외국인 순매수: {data['foreigner_net']:,}주")
            else:
                print("외국인 순매수: 모의투자 미지원")
        else:
            print("데이터 조회 실패")
