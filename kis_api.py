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
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ============================================================
# 설정
# ============================================================

APPKEY    = os.environ.get("KIS_APPKEY", "")
APPSECRET = os.environ.get("KIS_APPSECRET", "")
ACCOUNT   = os.environ.get("KIS_ACCOUNT", "")

# 환경변수 로딩 확인
all_kis_keys = [k for k in os.environ.keys() if "KIS" in k.upper()]
logger.info(f"KIS 관련 환경변수 목록: {all_kis_keys}")
if APPKEY:
    logger.info(f"KIS API 키 로딩 성공 (appkey 앞 4자리: {APPKEY[:4]}...)")
else:
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
    headers = get_headers("FHKST01010100")
    if not headers:
        return None
    try:
        url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
        }
        res = requests.get(url, headers=headers, params=params, timeout=10)
        logger.info(f"KIS 응답 코드: {res.status_code}")
        if res.status_code != 200:
            logger.error(f"KIS 응답 내용: {res.text[:500]}")
        res.raise_for_status()
        data = res.json()
        if data.get("rt_cd") != "0":
            logger.warning(f"KIS 현재가 조회 실패 ({code}): {data.get('msg1')}")
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
# 국내 주식 기본 정보 (재무 지표)
# ============================================================

def get_stock_info(code: str) -> dict | None:
    """주식 기본 정보 조회 (CTPF1002R) - ROE, 부채비율 등."""
    headers = get_headers("CTPF1002R")
    if not headers:
        return None
    try:
        url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/search-stock-info"
        params = {
            "PRDT_TYPE_CD": "300",
            "PDNO": code,
        }
        res = requests.get(url, headers=headers, params=params, timeout=10)
        res.raise_for_status()
        data = res.json()
        if data.get("rt_cd") != "0":
            logger.warning(f"KIS 기본정보 조회 실패 ({code}): {data.get('msg1')}")
            return None
        output = data.get("output", {})
        return {
            "name": output.get("prdt_abrv_name", ""),       # 종목명
            "sector": output.get("std_idst_clsf_cd_name", ""),  # 업종
            "market": output.get("mket_id_cd", ""),          # 시장 구분
            "listed_shares": int(output.get("lstg_stqt", 0) or 0),  # 상장주수
        }
    except Exception as e:
        logger.error(f"KIS 기본정보 조회 오류 ({code}): {e}")
        return None


# ============================================================
# 국내 주식 재무 비율
# ============================================================

def get_financial_ratio(code: str) -> dict | None:
    """재무 비율 조회 (FHKST66430300) - ROE, 영업이익률 등."""
    headers = get_headers("FHKST66430300")
    if not headers:
        return None
    try:
        url = f"{BASE_URL}/uapi/domestic-stock/v1/finance/financial-ratio"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_DIV_CLS_CODE": "0",  # 0: 연간
        }
        res = requests.get(url, headers=headers, params=params, timeout=10)
        res.raise_for_status()
        data = res.json()
        if data.get("rt_cd") != "0":
            logger.warning(f"KIS 재무비율 조회 실패 ({code}): {data.get('msg1')}")
            return None
        output_list = data.get("output", [])
        if not output_list:
            return None
        # 가장 최근 연도 데이터
        output = output_list[0]
        return {
            "stac_yymm": output.get("stac_yymm", ""),          # 결산 연월
            "roe": float(output.get("roe_val", 0) or 0),        # ROE (%)
            "roa": float(output.get("roa_val", 0) or 0),        # ROA (%)
            "operating_margin": float(output.get("bstp_enpn_rate", 0) or 0),  # 영업이익률 (%)
            "net_margin": float(output.get("net_prft_rate", 0) or 0),         # 순이익률 (%)
            "debt_ratio": float(output.get("lblt_rate", 0) or 0),             # 부채비율 (%)
            "current_ratio": float(output.get("crnt_rate", 0) or 0),          # 유동비율 (%)
            "interest_coverage": float(output.get("int_covr_rate", 0) or 0),  # 이자보상배율
            "revenue_growth": float(output.get("sles_icrs_rate", 0) or 0),    # 매출성장률 (%)
            "op_income_growth": float(output.get("bstp_enpn_icrs_rate", 0) or 0),  # 영업이익성장률
        }
    except Exception as e:
        logger.error(f"KIS 재무비율 조회 오류 ({code}): {e}")
        return None


# ============================================================
# 외국인/기관 수급
# ============================================================

def get_investor_trend(code: str) -> dict | None:
    """외국인/기관 순매수 동향 조회."""
    # 시세 조회 계열은 모의/실전 모두 동일 TR_ID 사용
    headers = get_headers("FHKST01010900")
    if not headers:
        return None
    try:
        url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-investor"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
        }
        res = requests.get(url, headers=headers, params=params, timeout=10)
        logger.info(f"KIS 수급 응답: {res.status_code} / {res.text[:200]}")
        if res.status_code != 200:
            return None
        data = res.json()
        if data.get("rt_cd") != "0":
            logger.warning(f"KIS 수급 조회 실패 ({code}): {data.get('msg1')}")
            return None
        output = data.get("output", [])
        if not output:
            return None
        today = output[0] if output else {}
        return {
            "foreigner_net": int(today.get("frgn_ntby_qty", 0) or 0),
            "institution_net": int(today.get("orgn_ntby_qty", 0) or 0),
            "individual_net": int(today.get("indvd_ntby_qty", 0) or 0),
        }
    except Exception as e:
        logger.error(f"KIS 수급 조회 오류 ({code}): {e}")
        return None


# ============================================================
# 일봉 데이터
# ============================================================

def get_daily_prices(code: str, start_date: str, end_date: str = None) -> list | None:
    """일봉 OHLCV 조회 (FHKST01010400).
    start_date, end_date: 'YYYYMMDD' 형식
    """
    headers = get_headers("FHKST01010400")
    if not headers:
        return None
    if not end_date:
        end_date = datetime.now().strftime("%Y%m%d")
    try:
        url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-price"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date,
            "FID_PERIOD_DIV_CODE": "D",  # D: 일봉
            "FID_ORG_ADJ_PRC": "0",      # 0: 수정주가
        }
        res = requests.get(url, headers=headers, params=params, timeout=10)
        res.raise_for_status()
        data = res.json()
        if data.get("rt_cd") != "0":
            return None
        output = data.get("output2", [])
        result = []
        for row in output:
            result.append({
                "date": row.get("stck_bsop_date", ""),
                "open": int(row.get("stck_oprc", 0) or 0),
                "high": int(row.get("stck_hgpr", 0) or 0),
                "low": int(row.get("stck_lwpr", 0) or 0),
                "close": int(row.get("stck_clpr", 0) or 0),
                "volume": int(row.get("acml_vol", 0) or 0),
            })
        return result
    except Exception as e:
        logger.error(f"KIS 일봉 조회 오류 ({code}): {e}")
        return None


# ============================================================
# 통합 조회 (팩터 분석용)
# ============================================================

def get_full_stock_data(code: str) -> dict | None:
    """팩터 분석에 필요한 전체 데이터 통합 조회."""
    tasks = {
        "price":    lambda: get_price(code),
        "ratio":    lambda: get_financial_ratio(code),
        "investor": lambda: get_investor_trend(code),
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
