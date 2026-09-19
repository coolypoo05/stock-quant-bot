"""설정/공용 상수 (환경변수, 로깅, HEADERS, STOCK_MAP)"""

import logging
import os
from zoneinfo import ZoneInfo



# .env 파일 로딩 (로컬 테스트용, Railway 환경변수는 덮어쓰지 않음)
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("bot")

# httpx가 요청 URL(텔레그램 봇 토큰 포함)을 INFO로 남기는 것을 차단
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN 환경변수가 설정되지 않았습니다.")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

STOCK_MAP: dict = {}  # {종목명: {"code": "005930", "suffix": ".KS"}}

KST = ZoneInfo("Asia/Seoul")
