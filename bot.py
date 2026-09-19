"""주식 퀀트 분석 텔레그램 봇 — 핸들러와 엔트리포인트"""

import asyncio
import time
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram import Update
from analysis import PERIOD_MAP, create_correlation_chart, format_compare_result, format_correlation_message, run_compare, run_correlation
from screening import ACTIVE_SCREENINGS, SCREENING_UNIVERSE, check_condition, fetch_stock_quick, load_screening_universe, parse_screen_conditions
from backtest import create_portfolio_chart, format_portfolio_message, parse_backtest_args, parse_portfolio_args, process_backtest, run_portfolio_backtest
from data import SCRAPE_STATS, load_stock_map
from scoring import process_factor
from config import BOT_TOKEN, KST, logger
import sector
from sector import SECTOR_CACHE, build_sector_cache, load_sector_cache


SCREEN_WORKERS = 5  # /screen 동시 조회 수


async def compare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/compare 삼성전자 SK하이닉스"""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "📊 팩터 비교 사용법:\n"
            "/compare <종목1> <종목2>\n\n"
            "예시:\n"
            "  /compare 삼성전자 SK하이닉스\n"
            "  /compare AAPL MSFT\n"
            "  /compare 삼성전자 AAPL\n\n"
            "밸류/퀄리티/모멘텀 항목별 비교 + 종합 우위 표시"
        )
        return

    q1 = context.args[0].strip()
    q2 = context.args[1].strip()
    await update.message.reply_text(f"⏳ {q1} vs {q2} 비교 분석 중...")
    chat_id = update.effective_chat.id

    async def run():
        try:
            loop = asyncio.get_event_loop()
            data1, data2 = await loop.run_in_executor(None, run_compare, q1, q2)
            if not data1:
                await context.bot.send_message(chat_id=chat_id, text=f"❌ '{q1}' 종목을 찾을 수 없어요.")
                return
            if not data2:
                await context.bot.send_message(chat_id=chat_id, text=f"❌ '{q2}' 종목을 찾을 수 없어요.")
                return
            msg = format_compare_result(data1, data2)
            await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            logger.exception("팩터 비교 실패")
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ 오류: {e}")

    asyncio.create_task(run())

async def sector_update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """업종 캐시 수동 갱신."""
    await update.message.reply_text(
        "⏳ 업종 평균 캐시 갱신 시작!\n"
        f"종목 수: {len(SCREENING_UNIVERSE)}개\n"
        "약 15~30분 소요됩니다. 완료 시 알림드려요."
    )
    chat_id = update.effective_chat.id
    loop = asyncio.get_running_loop()  # 워커 스레드에는 루프가 없어 미리 잡아서 전달

    def _build():
        try:
            build_sector_cache()
            asyncio.run_coroutine_threadsafe(
                context.bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ 업종 캐시 갱신 완료!\n"
                         f"업종 수: {len(SECTOR_CACHE)}개\n"
                         f"갱신일: {sector.SECTOR_CACHE_DATE}"
                ),
                loop
            )
        except Exception as e:
            logger.error(f"업종 캐시 갱신 실패: {e}")

    import threading
    threading.Thread(target=_build, daemon=True).start()

async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """스크래핑 소스별 성공/실패 현황."""
    if not SCRAPE_STATS:
        await update.message.reply_text("아직 스크래핑 기록이 없어요.")
        return
    lines = ["🩺 스크래핑 상태 (재시작 이후)"]
    for src, (ok, fail) in SCRAPE_STATS.items():
        lines.append(f"• {src}: 성공 {ok} / 실패 {fail} ({fail / (ok + fail):.0%} 실패)")
    await update.message.reply_text("\n".join(lines))

async def corr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/corr 삼성전자 SK하이닉스 AAPL [6m/1y/3y]"""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "📊 상관계수 분석 사용법:\n"
            "/corr <종목1> <종목2> ... [기간]\n\n"
            "기간 옵션:\n"
            "  6m  → 6개월\n"
            "  1y  → 1년 (기본값)\n"
            "  3y  → 3년\n\n"
            "예시:\n"
            "  /corr 삼성전자 SK하이닉스\n"
            "  /corr 삼성전자 SK하이닉스 NAVER 1y\n"
            "  /corr AAPL MSFT GOOGL 3y\n"
            "  /corr 삼성전자 AAPL 1y\n\n"
            "⚠️ 최대 5개 종목까지 가능"
        )
        return

    args = context.args

    # 기간 파싱 (마지막 인자가 6m/1y/3y이면 기간으로 처리)
    period = "1y"
    if args[-1].lower() in PERIOD_MAP:
        period = args[-1].lower()
        args = args[:-1]

    if len(args) < 2:
        await update.message.reply_text("❌ 종목을 2개 이상 입력해주세요.")
        return

    if len(args) > 5:
        await update.message.reply_text("❌ 최대 5개 종목까지 가능해요.")
        return

    _, period_label = PERIOD_MAP[period]
    await update.message.reply_text(
        f"⏳ 상관계수 분석 중...\n"
        f"종목: {', '.join(args)}\n"
        f"기간: {period_label}"
    )

    chat_id = update.effective_chat.id

    async def run_corr():
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, run_correlation, list(args), period
            )

            if not result:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="❌ 데이터를 가져올 수 없어요.\n2개 이상 종목의 데이터가 필요해요."
                )
                return

            # 차트 전송
            chart_bytes = create_correlation_chart(result)
            if chart_bytes:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=chart_bytes,
                    caption=f"📊 상관계수 분석 ({period_label})"
                )

            # 텍스트 결과
            msg = format_correlation_message(result)
            await context.bot.send_message(
                chat_id=chat_id, text=msg, disable_web_page_preview=True
            )
        except Exception as e:
            logger.exception("상관계수 분석 실패")
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ 오류: {e}")

    asyncio.create_task(run_corr())

# ============================================================
# 텔레그램 핸들러
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📊 퀀트 분석 봇입니다.\n\n"
        "🎯 팩터 스코어링\n"
        "/factor 삼성전자 - 종목 팩터 분석\n"
        "/factor AAPL - 미국 주식도 가능\n\n"
        "또는 그냥 종목명/티커만 입력해도 돼요!\n\n"
        "⚙️ 점수화 항목:\n"
        "🟢 밸류 (PER, PBR, PSR)\n"
        "🟡 퀄리티 (ROE, 영업이익률, 부채비율)\n"
        "🔴 모멘텀 (1M/3M/6M/12M 수익률)\n\n"
        "📌 참고 정보:\n"
        "💰 배당 (배당수익률, 배당성향)\n"
        "📉 변동성 (베타, 연환산 변동성)"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 사용법\n\n"
        "🎯 팩터 분석:\n"
        "  /factor <종목> - 팩터 스코어링\n"
        "  예) /factor 삼성전자\n"
        "      /factor 005930\n"
        "      /factor AAPL\n\n"
        "🔍 스크리닝:\n"
        "  /screen PER<10 ROE>15       (전체)\n"
        "  /screen KR PER<10 ROE>15    (한국만)\n"
        "  /screen US DIV>3 ROE>15     (미국만)\n"
        "  /stop_screen                (진행 중단)\n"
        "  /screen 만 입력하면 도움말\n\n"
        "📊 백테스팅:\n"
        "  /backtest <종목> <시작일> [종료일]\n"
        "  예) /backtest 삼성전자 2020-01-01\n\n"
        "💼 포트폴리오 백테스팅:\n"
        "  /backtest_portfolio <종목:비율> ... <시작일>\n"
        "  예) /backtest_portfolio AAPL:4 MSFT:3 GOOGL:3 2020-01-01\n\n"
        "🔗 상관계수 분석:\n"
        "  /corr <종목1> <종목2> ... [6m/1y/3y]\n"
        "  예) /corr 삼성전자 SK하이닉스 1y\n"
        "      /corr AAPL MSFT GOOGL 3y\n\n"
        "🏭 업종 상대평가:\n"
        "  /sector_update  → 업종 캐시 수동 갱신\n"
        "  (팩터 분석 시 자동 표시)\n\n"
        "또는 종목명/티커만 입력해도 자동 분석합니다.\n\n"
        "⚙️ 점수 산출 방식:\n"
        "  • 밸류40 + 퀄리티40 + 모멘텀20\n"
        "  • 위험 신호 자동 감점\n"
        "  • Piotroski F-Score 별도 표시\n\n"
        "📌 참고 정보 (점수 X):\n"
        "  • 배당 / 변동성 / 성장성"
    )

async def factor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("사용법: /factor 삼성전자 또는 /factor AAPL")
        return
    query = " ".join(context.args).strip()
    await update.message.reply_text("⏳ 팩터 분석 중...")
    try:
        result = await asyncio.to_thread(process_factor, query)
        if result:
            await update.message.reply_text(result, disable_web_page_preview=True)
        else:
            await update.message.reply_text(f"❌ '{query}' 종목을 찾을 수 없어요.")
    except Exception as e:
        logger.exception("팩터 분석 실패")
        await update.message.reply_text(f"⚠️ 오류: {e}")

async def backtest_portfolio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """포트폴리오 백테스팅 명령어.
    /backtest_portfolio AAPL:4 MSFT:3 GOOGL:3 2020-01-01 10000
    /backtest_portfolio 삼성전자:4 SK하이닉스:3 NAVER:3 2020-01-01 10000000
    """
    if not context.args or len(context.args) < 3:
        await update.message.reply_text(
            "📊 포트폴리오 백테스팅 사용법:\n"
            "/backtest_portfolio <종목:비율> ... <시작일> [초기금액]\n\n"
            "예시:\n"
            "  🇺🇸 미국:\n"
            "  /backtest_portfolio AAPL:4 MSFT:3 GOOGL:3 2020-01-01 10000\n\n"
            "  🇰🇷 한국:\n"
            "  /backtest_portfolio 삼성전자:4 SK하이닉스:3 NAVER:3 2020-01-01 10000000\n\n"
            "⚠️ 주의:\n"
            "  • 한국/미국 종목 혼용 불가\n"
            "  • 비율은 상대값 (4:3:3 → 40%, 30%, 30%)\n"
            "  • 초기금액 생략 시: 한국 1천만원 / 미국 $10,000\n"
            "  • 리밸런싱 없음 (매수 후 방치)"
        )
        return

    holdings, start_date, end_date, initial = parse_portfolio_args(context.args)

    if not holdings:
        await update.message.reply_text(
            "❌ 입력 형식이 잘못됐어요.\n"
            "예) /backtest_portfolio AAPL:4 MSFT:3 GOOGL:3 2020-01-01"
        )
        return

    names = [h["ticker"] for h in holdings]
    await update.message.reply_text(
        f"⏳ 포트폴리오 백테스팅 중...\n"
        f"종목: {', '.join(names)}\n"
        f"기간: {start_date} ~\n"
        f"잠시만 기다려주세요!"
    )

    chat_id = update.effective_chat.id

    async def run_port_bt():
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, run_portfolio_backtest, holdings, start_date, end_date, initial
            )

            if not result:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="❌ 데이터를 가져올 수 없어요.\n종목명/티커와 날짜를 확인해주세요."
                )
                return

            # 통화 혼용 에러
            if result.get("error") == "mixed_currency":
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "❌ 한국과 미국 종목을 함께 사용할 수 없어요.\n"
                        "같은 통화의 종목끼리만 가능해요.\n"
                        "🇰🇷 한국끼리 또는 🇺🇸 미국끼리 입력해주세요."
                    )
                )
                return

            # 차트 전송
            chart_bytes = create_portfolio_chart(result)
            if chart_bytes:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=chart_bytes,
                    caption="📊 포트폴리오 백테스팅 차트"
                )

            # 텍스트 결과
            message = format_portfolio_message(result)
            await context.bot.send_message(
                chat_id=chat_id, text=message, disable_web_page_preview=True
            )
        except Exception as e:
            logger.exception("포트폴리오 백테스팅 실패")
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ 오류: {e}")

    asyncio.create_task(run_port_bt())

async def backtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """백테스팅 명령어. /backtest 005930 2020-01-01 [2024-12-31]"""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "📊 백테스팅 사용법:\n"
            "/backtest <종목> <시작일> [종료일]\n\n"
            "예시:\n"
            "  /backtest 삼성전자 2020-01-01\n"
            "  /backtest 005930 2020-01-01\n"
            "  /backtest AAPL 2018-06-15 2024-12-31\n\n"
            "📋 분석 항목:\n"
            "  • 수익률 (총/CAGR)\n"
            "  • 벤치마크 대비 초과수익\n"
            "    🇰🇷 한국: KOSPI/KOSDAQ\n"
            "    🇺🇸 미국: S&P 500\n"
            "  • 최대 낙폭 (MDD)\n"
            "  • 변동성, 샤프 비율\n"
            "  • 차트 (가격 추이 + 낙폭)"
        )
        return

    query, start_date, end_date = parse_backtest_args(context.args)
    if not query:
        await update.message.reply_text(
            "❌ 입력 형식이 잘못됐어요.\n"
            "날짜는 YYYY-MM-DD 형식으로 입력하세요.\n"
            "예) /backtest 삼성전자 2020-01-01"
        )
        return

    await update.message.reply_text("⏳ 백테스팅 분석 중... (1~2분 소요)")
    chat_id = update.effective_chat.id

    async def run_bt():
        try:
            # 동기 함수를 별도 스레드에서 실행 (yfinance 호출이 동기)
            loop = asyncio.get_event_loop()
            result_tuple = await loop.run_in_executor(
                None, process_backtest, query, start_date, end_date
            )
            result, message, chart_bytes, bench_name = result_tuple

            if not result:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ '{query}' 종목 데이터를 가져올 수 없어요.\n종목명/티커와 날짜를 확인해주세요."
                )
                return

            # 차트 먼저 전송
            if chart_bytes:
                try:
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=chart_bytes,
                        caption=f"📊 {result['name']} ({result['ticker']})"
                    )
                except Exception as e:
                    logger.warning(f"차트 전송 실패: {e}")

            # 텍스트 결과
            await context.bot.send_message(chat_id=chat_id, text=message, disable_web_page_preview=True)
        except Exception as e:
            logger.exception("백테스팅 실패")
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ 오류: {e}")

    asyncio.create_task(run_bt())

async def screen_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """스크리닝 명령어.
    /screen PER<10 ROE>15       → 전체 (한국+미국)
    /screen KR PER<10 ROE>15    → 한국만
    /screen US PER<10 ROE>15    → 미국만
    """
    if not context.args:
        await update.message.reply_text(
            "📋 스크리닝 사용법:\n"
            "/screen PER<10 ROE>15\n"
            "/screen KR PER<10 ROE>15  (한국만)\n"
            "/screen US PER<10 ROE>15  (미국만)\n\n"
            "💰 밸류:\n"
            "  PER, FORWARDPER, PBR, PSR, PEG\n\n"
            "⚙️ 퀄리티:\n"
            "  ROE, ROA (%), OPMARGIN (영업이익률 %)\n"
            "  NETMARGIN (순이익률 %), GROSSMARGIN (매출총이익률 %)\n"
            "  DEBT (부채비율 %), CURRENTRATIO (유동비율)\n\n"
            "💰 배당:\n"
            "  DIV (배당수익률 %), PAYOUT (배당성향 %)\n\n"
            "📈 성장:\n"
            "  REVGROWTH (매출성장률 %), EPSGROWTH (EPS성장률 %)\n\n"
            "📊 규모:\n"
            "  MARKETCAP\n"
            "    🇰🇷 한국: 억원  예) MARKETCAP>10000 (1조 이상)\n"
            "    🇺🇸 미국: 백만달러  예) MARKETCAP>10000 (100억달러 이상)\n"
            "  PRICE\n"
            "    🇰🇷 한국: 원  예) PRICE<50000\n"
            "    🇺🇸 미국: 달러  예) PRICE<100\n\n"
            "🔑 특수 키워드:\n"
            "  IMPROVING     → PER > Forward PER (실적 개선 기대)\n"
            "  DETERIORATING → PER < Forward PER (실적 둔화 우려)\n"
            "  PROFITABLE    → EPS 흑자 종목만\n"
            "  DIVIDEND      → 배당 지급 종목만\n\n"
            "연산자: <, <=, >, >=, =\n\n"
            "예시:\n"
            "  /screen KR IMPROVING ROE>10\n"
            "  /screen KR PER<10 ROE>15\n"
            "  /screen US DIVIDEND GROSSMARGIN>40\n"
            "  /screen PROFITABLE PBR<1 ROE>10"
        )
        return

    args_text = " ".join(context.args).strip()

    # 시장 필터 파싱 (첫 번째 단어가 KR/US인지 확인)
    market_filter = "ALL"
    first_word = context.args[0].upper()
    if first_word == "KR":
        market_filter = "KR"
        args_text = " ".join(context.args[1:]).strip()
    elif first_word == "US":
        market_filter = "US"
        args_text = " ".join(context.args[1:]).strip()

    conditions = parse_screen_conditions(args_text)

    if not conditions:
        await update.message.reply_text(
            "❌ 조건을 인식할 수 없어요.\n"
            "예) /screen PER<10 ROE>15\n"
            "/screen 만 입력하면 도움말이 나와요."
        )
        return

    # 시장 필터 적용
    if market_filter == "KR":
        universe = [s for s in SCREENING_UNIVERSE if s["market"] in ("KOSPI200", "KOSDAQ150")]
        market_label = "🇰🇷 한국 (코스피200+코스닥150)"
    elif market_filter == "US":
        universe = [s for s in SCREENING_UNIVERSE if s["market"] == "SP500"]
        market_label = "🇺🇸 미국 (S&P500)"
    else:
        universe = SCREENING_UNIVERSE
        market_label = "🇰🇷 한국 + 🇺🇸 미국 (전체)"

    if not universe:
        await update.message.reply_text("⚠️ 종목 리스트가 아직 로딩되지 않았어요. 잠시 후 다시 시도하세요.")
        return

    cond_summary = ", ".join([
        c["raw"] if c.get("type") in ("compare", "positive")
        else f"{c['raw']}{c['op']}{c['val']}"
        for c in conditions
    ])
    total = len(universe)

    chat_id = update.effective_chat.id
    if chat_id in ACTIVE_SCREENINGS:
        await update.message.reply_text(
            "⚠️ 이미 진행 중인 스크리닝이 있어요.\n"
            "/stop_screen 으로 중단 후 다시 시도하세요."
        )
        return

    # 예상 시간 (실측: 5종목 배치당 약 1초)
    est_min = max(1, int(total / SCREEN_WORKERS // 60))
    await update.message.reply_text(
        f"🔍 스크리닝 시작!\n"
        f"범위: {market_label}\n"
        f"조건: {cond_summary}\n"
        f"종목 수: {total}개\n"
        f"⏳ 약 {est_min}~{est_min+3}분 소요. 완료 시 자동 알림드려요."
    )

    # 진행 상태 등록
    ACTIVE_SCREENINGS[chat_id] = {"cancel": False}

    # 비동기 백그라운드 실행
    async def run_screening():
        try:
            import gc
            matches = []
            processed = 0
            last_progress = 0

            for i in range(0, total, SCREEN_WORKERS):
                # 중단 체크
                if ACTIVE_SCREENINGS.get(chat_id, {}).get("cancel"):
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"🛑 스크리닝 중단됨 ({processed}/{total})"
                    )
                    return

                batch = universe[i:i + SCREEN_WORKERS]
                results = await asyncio.gather(
                    *(asyncio.to_thread(fetch_stock_quick, item) for item in batch)
                )
                processed += len(batch)
                for data in results:
                    if data and all(check_condition(data, c) for c in conditions):
                        matches.append(data)

                await asyncio.sleep(0.3)

                if processed % 50 < SCREEN_WORKERS:
                    gc.collect()

                if processed - last_progress >= 100:
                    last_progress = processed
                    try:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"⏳ 진행 중... {processed}/{total} ({len(matches)}개 매칭)"
                        )
                    except Exception:
                        pass

            if not matches:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"❌ 조건에 맞는 종목이 없어요.\n"
                        f"조건: {cond_summary}\n\n"
                        f"💡 가능한 원인:\n"
                        f"• 조건이 너무 엄격해요 (완화 시도)\n"
                        f"• FORWARDPER 등 일부 지표는 한국 주식에 데이터 없을 수 있어요\n"
                        f"• PER/PBR 등 기본 지표로 먼저 시도해보세요"
                    )
                )
                return

            # 시가총액 큰 순으로 정렬
            matches.sort(key=lambda x: -(x.get("market_cap_bil") or 0))

            flag_map = {"KOSPI200": "🇰🇷", "KOSDAQ150": "🇰🇷", "SP500": "🇺🇸"}
            msg = f"✅ 스크리닝 완료!\n범위: {market_label}\n조건: {cond_summary}\n매칭: {len(matches)}개\n"
            msg += "━━━━━━━━━━━━━━━\n\n"

            for i, m in enumerate(matches[:30], 1):
                flag = flag_map.get(m["market"], "🌐")
                msg += f"{i}. {flag} {m['name']} ({m['code']})\n"
                parts = []
                pe = m.get("pe_ratio")
                pb = m.get("pb_ratio")
                roe = m.get("roe")
                div = m.get("dividend_yield")
                cap = m.get("market_cap_bil")

                parts.append(f"PER {pe:.2f}" if pe and pe > 0 else "PER N/A")
                parts.append(f"PBR {pb:.3f}" if pb and pb > 0 else "PBR N/A")
                parts.append(f"ROE {roe:.1f}%" if roe is not None else "ROE N/A")
                parts.append(f"DIV {div:.2f}%" if div else "무배당")
                if cap:
                    if m["market"] != "SP500":
                        parts.append(f"시총 {cap:.0f}억")
                    else:
                        parts.append(f"시총 ${cap:.0f}M")
                if parts:
                    msg += "   " + " | ".join(parts) + "\n"

            if len(matches) > 30:
                msg += f"\n... 외 {len(matches)-30}개 더\n"

            msg += "\n💡 자세한 분석은 /factor <종목명>"

            if len(msg) > 4000:
                msg = msg[:3950] + "\n\n... (메시지 길이 초과)"

            await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            logger.exception("스크리닝 실패")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ 스크리닝 중 오류: {e}"
            )
        finally:
            # 진행 상태 정리
            ACTIVE_SCREENINGS.pop(chat_id, None)

    asyncio.create_task(run_screening())

async def stop_screen_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """진행 중인 스크리닝을 중단."""
    chat_id = update.effective_chat.id
    if chat_id in ACTIVE_SCREENINGS:
        ACTIVE_SCREENINGS[chat_id]["cancel"] = True
        await update.message.reply_text("🛑 스크리닝 중단 요청됨. 잠시 후 멈춰요.")
    else:
        await update.message.reply_text("⚠️ 진행 중인 스크리닝이 없어요.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.message.text.strip()
    logger.info(f"조회 요청: {query}")
    try:
        result = await asyncio.to_thread(process_factor, query)
        if result:
            await update.message.reply_text(result, disable_web_page_preview=True)
        else:
            await update.message.reply_text(
                f"❌ '{query}' 종목을 찾을 수 없어요.\n/help 로 사용법을 확인하세요."
            )
    except Exception as e:
        logger.exception("조회 실패")
        await update.message.reply_text(f"⚠️ 오류: {e}")

def main() -> None:
    load_stock_map()
    load_screening_universe()

    # 업종 캐시 백그라운드 빌드 (봇 시작 직후 비동기로)
    import threading
    def _build_cache():
        try:
            build_sector_cache()
        except Exception as e:
            logger.error(f"업종 캐시 빌드 실패: {e}")
    if not (load_sector_cache() and sector.SECTOR_CACHE_DATE == datetime.now(KST).strftime("%Y-%m-%d")):
        threading.Thread(target=_build_cache, daemon=True).start()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("factor", factor_cmd))
    app.add_handler(CommandHandler("screen", screen_cmd))
    app.add_handler(CommandHandler("stop_screen", stop_screen_cmd))
    app.add_handler(CommandHandler("backtest", backtest_cmd))
    app.add_handler(CommandHandler("backtest_portfolio", backtest_portfolio_cmd))
    app.add_handler(CommandHandler("corr", corr_cmd))
    app.add_handler(CommandHandler("compare", compare_cmd))
    app.add_handler(CommandHandler("sector_update", sector_update_cmd))
    app.add_handler(CommandHandler("health", health_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # 매일 새벽 6시 업종 캐시 갱신 (threading 스케줄러)
    def _daily_cache_scheduler():
        while True:
            try:
                now = datetime.now(ZoneInfo("Asia/Seoul"))
                next_run = now.replace(hour=6, minute=0, second=0, microsecond=0)
                if now >= next_run:
                    next_run = next_run + timedelta(days=1)
                wait_sec = (next_run - now).total_seconds()
                time.sleep(wait_sec)
                _build_cache()
            except Exception as e:
                logger.error(f"업종 캐시 스케줄러 오류: {e}")
                time.sleep(3600)

    threading.Thread(target=_daily_cache_scheduler, daemon=True).start()

    logger.info("퀀트 봇 시작...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
