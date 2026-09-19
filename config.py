"""설정/공용 상수 (환경변수, 로깅, HEADERS, STOCK_MAP)"""

import logging
import os
import time
from zoneinfo import ZoneInfo

import requests



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

ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
_last_alert: dict = {}


def notify_admin(key: str, text: str, cooldown: int = 3600) -> None:
    """ADMIN_CHAT_ID가 설정돼 있으면 텔레그램으로 경고 전송 (key별 쿨다운)."""
    if not ADMIN_CHAT_ID or time.time() - _last_alert.get(key, 0) < cooldown:
        return
    _last_alert[key] = time.time()
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_CHAT_ID, "text": text}, timeout=10,
        )
    except Exception as e:
        logger.error(f"관리자 알림 전송 실패: {type(e).__name__}")  # 예외 메시지에 토큰 URL이 있을 수 있어 타입만 기록
