import json
import os
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


ROOM_ID = "9979"

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]


def get_balance():
    timestamp = int(time.time() * 1000)

    url = (
        "https://ssn.xjtu.edu.cn/cems/mobile/"
        f"meterAccount/electricity?roomId={ROOM_ID}&_={timestamp}"
    )

    request = Request(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 ElectricityMonitor/1.0",
        },
    )

    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"查询电费失败：{exc}") from exc

    data = json.loads(raw)

    if data.get("code") not in (0, "0"):
        raise RuntimeError(f"学校接口返回异常：{data}")

    balance = float(data["data"])

    return round(balance, 2)


def save_to_supabase(balance):
    url = f"{SUPABASE_URL}/rest/v1/electricity"

    payload = json.dumps({
        "balance": balance
    }).encode("utf-8")

    request = Request(
        url,
        data=payload,
        method="POST",
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )

    try:
        with urlopen(request, timeout=15) as response:
            status = response.status
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Supabase 写入失败：HTTP {exc.code}，{body}"
        ) from exc

    print(f"Supabase 状态码：{status}")


if __name__ == "__main__":

    print("开始查询寝室电费……")

    balance = get_balance()

    print(f"当前寝室电费：{balance:.2f} 元")

    save_to_supabase(balance)

    print("电费记录已成功写入 Supabase")