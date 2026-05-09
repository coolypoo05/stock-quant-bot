"""KIS API 응답 상세 확인"""
from kis_api import get_headers, BASE_URL
import requests
import json

headers = get_headers("FHKST01010100")
url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": "005930"}
res = requests.get(url, headers=headers, params=params, timeout=10)
print("=== 현재가 전체 응답 ===")
print(json.dumps(res.json(), indent=2, ensure_ascii=False))