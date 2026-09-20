"""팩터 입력값 스냅샷: 매일 유니버스의 경량 팩터 입력값과 한국 수급 강도를 SQLite에 저장 (검증용 시점별 데이터셋)."""

import math
import os
import sqlite3
from datetime import datetime

from config import KST, logger, notify_admin

SNAPSHOT_DB_PATH = os.environ.get("SNAPSHOT_DB_PATH", "factor_snapshots.db")
MIN_COVERAGE = 0.6  # 저장 행 수가 기대치의 이 비율 미만이면 경고

# (테이블 컬럼, fetch_stock_quick 결과의 키). 값의 단위는 fetch_stock_quick 그대로 (ROE·마진·성장률·배당은 %)
FACTOR_COLUMNS = [
    ("market", "market"), ("name", "name"), ("sector", "sector"), ("industry", "industry"),
    ("price", "price"), ("market_cap_bil", "market_cap_bil"),
    ("pe", "pe_ratio"), ("forward_pe", "forward_pe"), ("pb", "pb_ratio"), ("ps", "ps_ratio"), ("peg", "peg_ratio"),
    ("roe", "roe"), ("roa", "roa"), ("op_margin", "operating_margin"), ("net_margin", "net_margin"),
    ("gross_margin", "gross_margin"), ("debt_to_equity", "debt_to_equity"), ("current_ratio", "current_ratio"),
    ("div_yield", "dividend_yield"), ("payout", "payout_ratio"),
    ("revenue_growth", "revenue_growth"), ("earnings_growth", "earnings_growth"),
]
FLOW_COLUMNS = ["ratio", "adv_eok", "foreigner_amt_20d", "institution_amt_20d"]
TEXT_COLUMNS = {"market", "name", "sector", "industry"}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(SNAPSHOT_DB_PATH, timeout=30)
    factor_cols = ", ".join(f"{c} {'TEXT' if c in TEXT_COLUMNS else 'REAL'}" for c, _ in FACTOR_COLUMNS)
    flow_cols = ", ".join(f"{c} REAL" for c in FLOW_COLUMNS)
    conn.execute(f"CREATE TABLE IF NOT EXISTS factor_snapshot (date TEXT, code TEXT, {factor_cols}, PRIMARY KEY (date, code))")
    conn.execute(f"CREATE TABLE IF NOT EXISTS flow_snapshot (date TEXT, code TEXT, {flow_cols}, PRIMARY KEY (date, code))")
    conn.execute("PRAGMA user_version = 1")
    return conn


def _clean(value, is_text: bool):
    """None/NaN은 NULL, 텍스트는 문자열, 숫자는 float로 정규화 (numpy 타입 등 sqlite가 못 받는 값 방지)."""
    if value is None:
        return None
    if is_text:
        return str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _check_coverage(kind: str, saved: int, expected: int | None) -> None:
    if expected and saved < MIN_COVERAGE * expected:
        msg = f"스냅샷 저장 행 부족 [{kind}]: {saved}/{expected}"
        logger.warning(msg)
        notify_admin(f"snapshot:{kind}", f"⚠️ {msg}")


def _save(kind: str, table: str, columns: list[str], values: list[tuple], expected: int | None) -> int:
    marks = ", ".join("?" * (len(columns) + 2))
    try:
        conn = _connect()
        try:
            with conn:
                conn.executemany(f"INSERT OR REPLACE INTO {table} (date, code, {', '.join(columns)}) VALUES ({marks})", values)
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"스냅샷 저장 실패 [{kind}]: {e}")
        notify_admin("snapshot:error", f"⚠️ 스냅샷 저장 실패 [{kind}]: {type(e).__name__}")
        return 0
    _check_coverage(kind, len(values), expected)
    return len(values)


def save_factor_rows(rows: list[dict], date: str | None = None, expected: int | None = None) -> int:
    """fetch_stock_quick 결과 목록을 저장하고 저장 행 수를 반환. 같은 (날짜, 종목)은 덮어쓴다."""
    date = date or _today()
    values = [(date, r["code"], *[_clean(r.get(src), col in TEXT_COLUMNS) for col, src in FACTOR_COLUMNS]) for r in rows]
    return _save("factor", "factor_snapshot", [c for c, _ in FACTOR_COLUMNS], values, expected)


def save_flow_rows(rows: list[dict], date: str | None = None, expected: int | None = None) -> int:
    """한국 종목별 수급 강도 행({code, ratio, adv_eok, foreigner_amt_20d, institution_amt_20d}) 저장."""
    date = date or _today()
    values = [(date, r["code"], *[_clean(r.get(c), False) for c in FLOW_COLUMNS]) for r in rows]
    return _save("flow", "flow_snapshot", FLOW_COLUMNS, values, expected)


def summary() -> dict:
    """마지막 날짜와 누적 일수, 마지막 날짜의 행 수."""
    empty = {"days": 0, "last_date": None, "factor_rows": 0, "flow_rows": 0}
    try:
        conn = _connect()
        try:
            days, last = conn.execute("SELECT COUNT(DISTINCT date), MAX(date) FROM factor_snapshot").fetchone()
            if not last:
                return empty
            factor = conn.execute("SELECT COUNT(*) FROM factor_snapshot WHERE date = ?", (last,)).fetchone()[0]
            flow = conn.execute("SELECT COUNT(*) FROM flow_snapshot WHERE date = ?", (last,)).fetchone()[0]
        finally:
            conn.close()
        return {"days": days, "last_date": last, "factor_rows": factor, "flow_rows": flow}
    except Exception as e:
        logger.error(f"스냅샷 요약 실패: {e}")
        return empty


def describe(info: dict) -> str:
    if not info["last_date"]:
        return "📦 스냅샷: 아직 저장된 데이터가 없어요."
    return (f"📦 스냅샷: 마지막 {info['last_date']}, 누적 {info['days']}일, "
            f"factor {info['factor_rows']}행 / flow {info['flow_rows']}행")
