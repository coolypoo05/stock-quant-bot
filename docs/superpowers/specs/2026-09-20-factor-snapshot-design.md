# 팩터 입력값 스냅샷 저장 (Factor Snapshot) 설계

작성일: 2026-09-20 · 상태: 설계 승인됨, 구현 전

## 목적

채점 방법론을 "개별 종목 진단 + 검증 가능한 전략"으로 개선하려면, 점수가 이후 수익률을 예측하는지 검증해야 한다.
그러나 yfinance의 PER/ROE 등은 **현재 값만** 제공해 과거 시점 값을 복원할 수 없다(look-ahead 없는 백테스트 불가).
따라서 매일 유니버스의 팩터 입력값을 저장해 시점별 데이터셋을 쌓는다. 데이터는 시간이 지나야 쌓이므로 착수가 시급하다.

모멘텀은 과거 가격으로 언제든 재계산할 수 있어 저장 대상이 아니다.

## 범위

- 포함: 종목별 경량 팩터 입력값 일별 저장(SQLite), 한국 종목 수급 강도 저장, 저장 실패·행 수 부족 알림, `/health` 표시.
- 제외(후속): 모멘텀 재설계와 과거 가격 검증, F-Score·EV/EBITDA 등 풀 분석 입력 저장(접근법 C 확장), 데이터 내보내기·보관 기간 정리 기능.

## 접근법: 경량 스냅샷 (기존 새벽 작업에 편승)

기존 새벽 6시(KST) 작업 `build_sector_cache()`가 유니버스 853종목에 대해 `fetch_stock_quick`을 이미 호출한다(종목당 약 0.3초).
현재는 업종 평균만 남기고 종목별 결과를 버리므로, 결과 전체를 반환받아 저장한다. 추가 네트워크 호출은 없다.

```
새벽 6시 작업
  build_sector_cache()   → 종목별 결과 rows 반환 (업종 없는 종목 포함, None 제외)
  snapshot.save_factor_rows(rows)
  run_flow_snapshot()    → 한국 종목별 수급 강도 rows
  snapshot.save_flow_rows(rows)
```

## 컴포넌트

### snapshot.py (신규)

SQLite 저장·조회만 담당한다. 채점 로직과 네트워크를 모른다.

- `save_factor_rows(rows, date=None) -> int`: `fetch_stock_quick` 결과 dict 목록을 저장하고 저장 행 수를 반환.
- `save_flow_rows(rows, date=None) -> int`: 수급 강도 행 저장.
- `summary() -> dict`: `{"days": 저장된 날짜 수, "last_date": 마지막 날짜 또는 None, "factor_rows": 마지막 날짜의 행 수, "flow_rows": 같음}`.
- `date` 기본값은 실행 시점의 KST 날짜(`YYYY-MM-DD`).
- 연결은 호출마다 열고 닫는다(`timeout=30`). 저장은 한 트랜잭션에서 `INSERT OR REPLACE`.
- 테이블은 `CREATE TABLE IF NOT EXISTS`로 만들고 `PRAGMA user_version = 1`을 기록한다.

### 스키마

`factor_snapshot` — 기본키 `(date, code)`. 값의 단위는 `fetch_stock_quick`이 반환하는 그대로다.

| 컬럼 | 원본 키 | 비고 |
|---|---|---|
| date, code | | 날짜는 실행 시점 KST |
| market, name, sector, industry | market, name, sector, industry | 업종은 yfinance 영문 |
| price | price | 통화는 종목 통화 |
| market_cap_bil | market_cap_bil | 한국 억원, 미국 백만달러 |
| pe, forward_pe, pb, ps, peg | pe_ratio, forward_pe, pb_ratio, ps_ratio, peg_ratio | |
| roe, roa, op_margin, net_margin, gross_margin | roe, roa, operating_margin, net_margin, gross_margin | % 단위 |
| debt_to_equity, current_ratio | debt_to_equity, current_ratio | 부채비율 % |
| div_yield, payout | dividend_yield, payout_ratio | % 단위 |
| revenue_growth, earnings_growth | revenue_growth, earnings_growth | % 단위 |

값이 없으면 `NULL`로 저장한다. `ev_ebitda`와 `interest_coverage`는 경량 조회가 항상 `None`을 주므로 컬럼을 만들지 않는다.

`flow_snapshot` — 기본키 `(date, code)`, 한국 종목만.

| 컬럼 | 의미 |
|---|---|
| ratio | (외국인+기관 20일 순매수 수량) ÷ 20일 거래량, % |
| adv_eok | 일평균 거래대금(억원) |
| foreigner_amt_20d, institution_amt_20d | 20일 누적 순매수 금액(백만원) |

### sector.py 변경

`build_sector_cache()`가 `None` 대신 종목별 결과 dict 목록을 반환한다(유니버스가 비어 있으면 빈 목록). 업종 평균 계산과 캐시 파일 저장 동작은 그대로 두고, `sector.py`는 DB를 모른다.

### flow.py 변경

`collect_flow_ratios`를 종목별 dict 목록(`code, ratio, adv_eok, foreigner_amt_20d, institution_amt_20d`) 반환으로 바꾼다. 유동성 부족 종목도 포함한다. 분포 요약과 밴드 이탈 감시는 그중 `adv_eok >= FLOW_FULL_ADV_EOK`인 행의 `ratio`만 써서 기존 동작을 유지한다(`flow_snapshots.jsonl`도 그대로).

### bot.py 변경

- 새벽 스케줄러와 시작 시 빌드, `/sector_update` 세 경로가 `build_sector_cache()` 반환값을 `snapshot.save_factor_rows`에 넘긴다. 같은 날 여러 번 돌려도 기본키로 덮어쓰기만 된다.
- `run_flow_snapshot()`은 종목별 행을 `snapshot.save_flow_rows`에 저장한다.
- `/health` 응답에 스냅샷 요약(마지막 날짜, 누적 일수, 마지막 날짜 행 수)을 덧붙인다.

## 오류 처리·모니터링

- 저장 실패는 예외를 삼키고 로그와 `notify_admin`만 남긴다. 업종 캐시와 봇 동작에는 영향을 주지 않는다.
- 저장된 factor 행 수가 유니버스 크기의 60% 미만이면 경고한다(`notify_admin` 키 `snapshot:factor`). flow 행 수는 한국 유니버스 크기의 60% 미만이면 같은 방식으로 경고한다.
- 경고는 기존 `notify_admin` 쿨다운 규칙을 따른다.

## 설정

- `SNAPSHOT_DB_PATH`: 기본 `factor_snapshots.db`(`.gitignore`의 `*.db`에 포함). Railway에서는 볼륨 경로(`/data/factor_snapshots.db`) 지정 필요.
- README 환경변수 표에 추가한다.

## 데이터 의미와 한계

- `date`는 새벽 작업 실행 날짜이며 값은 전일 종가 기준이다. 나중에 다음 거래일부터의 수익률을 볼 때 이 시차를 반영해야 한다.
- 값은 yfinance가 그 시각에 준 값이라 이후 정정은 반영되지 않는다.
- 그날 새벽 작업이 실패하거나 재시작 시 "오늘 캐시 있음"으로 재계산을 건너뛰면 그날 스냅샷은 빈다.
- 크기는 하루 약 850행, 연 40~50MB 수준으로 예상한다(추정).

## 테스트

오프라인 단위 테스트(임시 DB, CI에서 실행):

- 경량 조회 dict → 행 변환(컬럼 매핑, `None`은 `NULL`, 업종이 없는 종목도 저장).
- 같은 날 두 번 저장해도 행 수가 늘지 않고 최신 값으로 덮어써진다.
- `summary()` 반환값(빈 DB와 데이터가 있는 DB).
- 행 수 부족 시 알림 발생, 정상 시 미발생.
- `build_sector_cache()`가 업종이 없는 종목까지 포함한 목록을 반환한다(가짜 조회 함수 사용).
- `collect_flow_ratios`가 종목별 행을 반환하고 분포 요약은 유동성 충분 종목만 사용한다.

로컬 실측: 5종목을 실제 조회해 임시 DB에 저장하고 행을 직접 확인한다. 853종목 전체는 시간이 걸려 로컬에서 돌리지 않는다.

## 성공 기준

배포 후 다음 새벽 작업에서 factor 약 850행, flow 약 300행이 저장되고 `/health`에 표시된다. 7일 뒤 7개 날짜가 쌓여 있다.
