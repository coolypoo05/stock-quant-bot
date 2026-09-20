"""수급 분포 스냅샷: 유니버스의 (외국인+기관 순매수 수량 / 거래량) 분포를 매일 저장하고 밴드 이탈을 감시."""

import json
import os
import time
from datetime import datetime

import numpy as np

import kis_api
import snapshot
from config import KST, logger, notify_admin
from scoring import FLOW_BANDS, FLOW_FULL_ADV_EOK
from screening import SCREENING_UNIVERSE

FLOW_SNAPSHOT_PATH = os.environ.get("FLOW_SNAPSHOT_PATH", "flow_snapshots.jsonl")
MIN_SAMPLE = 50  # 이보다 적으면 분포로 쓰지 않음 (KIS 장애 등)


def collect_flow_rows(universe=None, pause: float = 0.2) -> list[dict]:
    """한국 유니버스 종목별 수급 행 (유동성 낮은 종목 포함, 거래량 조회에 실패한 종목은 제외)."""
    universe = SCREENING_UNIVERSE if universe is None else universe
    rows = []
    for item in universe:
        if item["market"] == "SP500":
            continue
        inv = kis_api.get_investor_trend(item["code"])
        time.sleep(pause)
        daily = kis_api.get_daily_volume(item["code"])
        time.sleep(pause)
        ratio, adv = kis_api.calc_flow_intensity(inv and inv["flows"], daily)
        if ratio is not None:
            rows.append({"code": item["code"], "ratio": ratio, "adv_eok": adv,
                         "foreigner_amt_20d": inv["foreigner_amt_20d"], "institution_amt_20d": inv["institution_amt_20d"]})
    return rows


def summarize(ratios: list[float]) -> dict:
    p10, p25, p50, p75, p90 = np.percentile(ratios, [10, 25, 50, 75, 90])
    return {"date": datetime.now(KST).strftime("%Y-%m-%d"), "n": len(ratios),
            "p10": round(p10, 2), "p25": round(p25, 2), "p50": round(p50, 2),
            "p75": round(p75, 2), "p90": round(p90, 2)}


def check_drift(summary: dict) -> str | None:
    """분포가 밴드 설계 가정(±강한/뚜렷한 임계값이 각각 상·하위 10%/25% 부근)에서 크게 벗어나면 설명 문자열 반환."""
    strong, distinct = FLOW_BANDS[0][0], FLOW_BANDS[1][0]
    problems = []
    for key, target, sign in (("p90", strong, 1), ("p10", strong, -1), ("p75", distinct, 1), ("p25", distinct, -1)):
        value = abs(summary[key]) if summary[key] * sign > 0 else 0.0
        if not 0.5 * target <= value <= 2 * target:
            problems.append(f"{key}={summary[key]:+.1f} (기대 약 {sign * target:+.1f})")
    if abs(summary["p50"]) > distinct:
        problems.append(f"중앙값 {summary['p50']:+.1f}% (시장 전체가 한쪽으로 쏠림)")
    return ", ".join(problems) or None


def save_snapshot(summary: dict, path: str | None = None) -> None:
    with open(path or FLOW_SNAPSHOT_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")


def run_flow_snapshot() -> dict | None:
    """수집 → 저장 → 이탈 시 관리자 알림. 하루 1회 실행 (약 10분 소요)."""
    rows = collect_flow_rows()
    kr_count = sum(1 for item in SCREENING_UNIVERSE if item["market"] != "SP500")
    snapshot.save_flow_rows(rows, expected=kr_count)
    ratios = [r["ratio"] for r in rows if r["adv_eok"] >= FLOW_FULL_ADV_EOK]  # 분포는 유동성 충분 종목만
    if len(ratios) < MIN_SAMPLE:
        logger.warning(f"수급 분포 스냅샷 생략: 표본 {len(ratios)}개 (최소 {MIN_SAMPLE})")
        return None
    summary = summarize(ratios)
    save_snapshot(summary)
    logger.info(f"수급 분포 스냅샷 저장: {summary}")
    drift = check_drift(summary)
    if drift:
        msg = f"수급 밴드 이탈 감지 ({summary['date']}, n={summary['n']}): {drift} — FLOW_BANDS 재보정 필요"
        logger.warning(msg)
        notify_admin("flow_drift", f"⚠️ {msg}", cooldown=6 * 3600)
    return summary
