# 📊 주식 퀀트 분석 텔레그램 봇

한국(KOSPI/KOSDAQ) · 미국(S&P500) 주식에 대한 **팩터 기반 퀀트 분석**을 제공하는 텔레그램 봇입니다.
저평가 우량주를 찾기 위한 밸류·퀄리티·모멘텀 스코어링, 스크리닝, 백테스팅, 상관계수 분석 등을 지원합니다.

---

## ✨ 주요 기능

### 📊 팩터 스코어링 (`/factor`)
종목 하나를 밸류 · 퀄리티 · 모멘텀 3개 팩터로 분석하고 가중 종합 점수를 산출합니다.

- **🟢 밸류 (40%)**: PER, Forward PER, PEG, PBR, EV/EBITDA, EPS성장률, PSR
- **🟡 퀄리티 (40%)**: ROE, 부채비율, 영업이익률, 이자보상배율, EPS 흑자여부
- **🔴 모멘텀 (20%)**: 1M/3M/6M 수익률, RSI, 이동평균 정배열, MACD, 거래량, 수급
  - 🇰🇷 한국: 외국인/기관 순매수 (KIS API)
  - 🇺🇸 미국: 기관 보유비율 · 공매도 비율 · 내부자 보유비율로 대체
- **⚠️ 위험 신호 자동 감점**: 고부채, 이자보상배율 위험, 적자 등
- **📊 Piotroski F-Score**: 재무 건전성 9점 만점 평가
- **🏭 업종 상대평가**: 동일 업종 평균 대비 PER/PBR/ROE/영업이익률 비교
- **📌 참고 정보**: 배당 수익률·성장률, 변동성(베타), 매출/EPS 성장률

### 🔍 스크리닝 (`/screen`)
코스피200 + 코스닥150 + S&P500 (총 853개 종목)을 조건에 맞게 필터링합니다.

```
/screen PER<10 ROE>15           # 전체 시장
/screen KR PER<10 ROE>15        # 한국만
/screen US DIV>3 ROE>15         # 미국만
```

`/stop_screen`으로 진행 중인 스크리닝을 중단할 수 있습니다.

### 📊 백테스팅 (`/backtest`)
특정 종목을 과거 특정 시점에 매수했다면 어떤 성과를 거뒀을지 시뮬레이션합니다.

```
/backtest 삼성전자 2020-01-01
/backtest AAPL 2018-06-15 2024-12-31
```

- 총수익률 / CAGR / 최대낙폭(MDD) / 변동성 / 샤프비율
- 벤치마크(KOSPI·KOSDAQ·S&P500) 대비 초과수익
- 가격 추이 + 낙폭 차트 이미지 제공

### 💼 포트폴리오 백테스팅 (`/backtest_portfolio`)
여러 종목을 원하는 비중으로 나눠 투자했을 때의 성과를 시뮬레이션합니다 (리밸런싱 없음, 매수 후 보유).

```
/backtest_portfolio AAPL:4 MSFT:3 GOOGL:3 2020-01-01 10000
/backtest_portfolio 삼성전자:4 SK하이닉스:3 NAVER:3 2020-01-01 10000000
```

- 종목별 수익률 + 포트폴리오 전체 수익률/CAGR/MDD/샤프비율
- 벤치마크 대비 비교, 구성 종목별 + 포트폴리오 + 벤치마크 비교 차트
- ⚠️ 한국/미국 종목 혼용 불가 (동일 통화끼리만)

### 🔗 상관계수 분석 (`/corr`)
종목 간 수익률 상관관계를 분석해 포트폴리오 분산 효과를 점검합니다.

```
/corr 삼성전자 SK하이닉스 1y
/corr AAPL MSFT GOOGL 3y
/corr 삼성전자 AAPL 1y        # 한국+미국 혼용 가능
```

- 기간 선택: `6m` / `1y` / `3y`
- 수익률 비교 차트 + 상관계수 히트맵
- 상관계수 쌍별 해석 및 포트폴리오 분산 효과 평가

### ⚖️ 팩터 비교 (`/compare`)
두 종목의 밸류·퀄리티·모멘텀 지표를 나란히 비교하고 항목별 우위를 표시합니다.

```
/compare 삼성전자 SK하이닉스
/compare AAPL MSFT
/compare 삼성전자 AAPL        # 한국+미국 혼용 가능
```

---

## 🛠️ 기술 스택

- Python 3.13
- python-telegram-bot
- yfinance, pandas, numpy
- matplotlib (차트 생성)
- BeautifulSoup (웹 크롤링)
- 한국투자증권 Open API
- Railway (배포)

---

## ⚠️ 디스클레이머

본 봇이 제공하는 모든 정보는 투자 참고용이며, 투자 결정에 대한 책임은 본인에게 있습니다.
과거 데이터 기반 분석이 미래 수익을 보장하지 않습니다.

---
---

# 📊 Stock Quant Analysis Telegram Bot

A Telegram bot providing **factor-based quant analysis** for Korean (KOSPI/KOSDAQ) and US (S&P 500) stocks.
It supports value · quality · momentum scoring to find undervalued quality stocks, along with screening, backtesting, and correlation analysis.

---

## ✨ Key Features

### 📊 Factor Scoring (`/factor`)
Analyzes a single stock across three factors — value, quality, and momentum — and produces a weighted overall score.

- **🟢 Value (40%)**: PER, Forward PER, PEG, PBR, EV/EBITDA, EPS growth, PSR
- **🟡 Quality (40%)**: ROE, debt-to-equity, operating margin, interest coverage ratio, EPS profitability
- **🔴 Momentum (20%)**: 1M/3M/6M returns, RSI, moving average alignment, MACD, volume, institutional flows
  - 🇰🇷 Korea: Foreign/institutional net buying (KIS API)
  - 🇺🇸 US: Substituted with institutional ownership %, short interest ratio, and insider ownership %
- **⚠️ Automatic risk penalties**: High debt, dangerous interest coverage, net losses, etc.
- **📊 Piotroski F-Score**: 9-point financial health assessment
- **🏭 Sector relative valuation**: Compares PER/PBR/ROE/operating margin against sector averages
- **📌 Additional info**: Dividend yield/growth, volatility (beta), revenue/EPS growth

### 🔍 Screening (`/screen`)
Filters across KOSPI 200 + KOSDAQ 150 + S&P 500 (853 stocks total) based on custom conditions.

```
/screen PER<10 ROE>15           # All markets
/screen KR PER<10 ROE>15        # Korea only
/screen US DIV>3 ROE>15         # US only
```

Use `/stop_screen` to cancel a screening in progress.

### 📊 Backtesting (`/backtest`)
Simulates the performance of a single stock if purchased at a given point in the past.

```
/backtest 삼성전자 2020-01-01
/backtest AAPL 2018-06-15 2024-12-31
```

- Total return / CAGR / Max Drawdown (MDD) / Volatility / Sharpe ratio
- Excess return vs. benchmark (KOSPI, KOSDAQ, S&P 500)
- Price chart + drawdown chart image included

### 💼 Portfolio Backtesting (`/backtest_portfolio`)
Simulates the performance of a multi-stock portfolio with custom weights (buy-and-hold, no rebalancing).

```
/backtest_portfolio AAPL:4 MSFT:3 GOOGL:3 2020-01-01 10000
/backtest_portfolio 삼성전자:4 SK하이닉스:3 NAVER:3 2020-01-01 10000000
```

- Per-stock returns + overall portfolio return/CAGR/MDD/Sharpe ratio
- Comparison vs. benchmark; chart showing individual holdings, portfolio, and benchmark
- ⚠️ Korean and US stocks cannot be mixed (same currency only)

### 🔗 Correlation Analysis (`/corr`)
Analyzes return correlations between stocks to evaluate portfolio diversification.

```
/corr 삼성전자 SK하이닉스 1y
/corr AAPL MSFT GOOGL 3y
/corr 삼성전자 AAPL 1y        # Korea + US mix supported
```

- Period selection: `6m` / `1y` / `3y`
- Return comparison chart + correlation heatmap
- Pairwise correlation interpretation and portfolio diversification assessment

### ⚖️ Factor Comparison (`/compare`)
Compares value/quality/momentum metrics of two stocks side by side, highlighting which one wins each category.

```
/compare 삼성전자 SK하이닉스
/compare AAPL MSFT
/compare 삼성전자 AAPL        # Korea + US mix supported
```

---

## 🛠️ Tech Stack

- Python 3.13
- python-telegram-bot
- yfinance, pandas, numpy
- matplotlib (chart generation)
- BeautifulSoup (web scraping)
- Korea Investment & Securities Open API
- Railway (deployment)

---

## ⚠️ Disclaimer

All information provided by this bot is for reference purposes only. Investment decisions and their outcomes are solely the responsibility of the user.
Past performance does not guarantee future results.
