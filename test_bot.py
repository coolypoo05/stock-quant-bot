"""순수 함수 스모크 테스트: python test_bot.py (또는 pytest)."""
import os

os.environ.setdefault("BOT_TOKEN", "test")

import data, scoring, screening  # noqa: E402


def test_weighted_overall():
    assert scoring.calc_weighted_overall(80, 60, 40) == 64  # 32+24+8
    assert scoring.calc_weighted_overall(80, 60, 0) == 70   # 모멘텀 없으면 가중치 정규화
    assert scoring.calc_weighted_overall(0, 0, 0) == 0


def test_grade_score():
    assert scoring.grade_score(80)[0] == "🟢 A"
    assert scoring.grade_score(29)[0] == "🔴 F"


def test_parse_and_check_conditions():
    conds = screening.parse_screen_conditions("PER<10 ROE>=15 PROFITABLE")
    assert [c["raw"] for c in conds] == ["PER", "ROE", "PROFITABLE"]
    assert all(screening.check_condition({"pe_ratio": 8, "roe": 15, "eps": 3}, c) for c in conds)
    assert not screening.check_condition({"pe_ratio": 12}, conds[0])
    assert not screening.check_condition({}, conds[0])  # 값 없으면 탈락


def test_record_scrape():
    data.SCRAPE_STATS.clear()
    data.record_scrape("x", True)
    data.record_scrape("x", False)
    assert data.SCRAPE_STATS["x"] == [1, 1]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("OK")
