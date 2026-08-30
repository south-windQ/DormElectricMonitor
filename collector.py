import subprocess
import json
import os
import time
import urllib.request


ROOM_ID = "9979"

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]


def get_balance():
    timestamp = int(time.time() * 1000)

    url = (
        "https://ssn.xjtu.edu.cn/cems/mobile/"
        f"meterAccount/electricity?roomId={ROOM_ID}&_={timestamp}"
    )

    result = subprocess.run(
        [
            "curl.exe",
            "-s",
            "--max-time",
            "15",
            url
        ],
        capture_output=True,
        text=True,
        encoding="utf-8"
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"curl 查询失败：{result.stderr}"
        )

    if not result.stdout.strip():
        raise RuntimeError("学校接口返回空内容")

    data = json.loads(result.stdout)

    if data.get("code") not in (0, "0"):
        raise RuntimeError(
            f"学校接口返回异常：{data}"
        )

    return round(float(data["data"]), 2)


def save_to_supabase(balance):
    url = f"{SUPABASE_URL}/rest/v1/electricity"

    payload = json.dumps({
        "balance": balance
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        method="POST"
    )

    request.add_header(
        "apikey",
        SUPABASE_SERVICE_ROLE_KEY
    )

    request.add_header(
        "Authorization",
        f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"
    )

    request.add_header(
        "Content-Type",
        "application/json"
    )

    request.add_header(
        "Prefer",
        "return=minimal"
    )

    with urllib.request.urlopen(
        request,
        timeout=15
    ) as response:

        print(
            f"Supabase 状态码：{response.status}"
        )


if __name__ == "__main__":

    print("开始查询寝室电费……")

    balance = get_balance()

    print(
        f"当前寝室电费：{balance:.2f} 元"
    )

    save_to_supabase(balance)

    print("电费记录已成功写入 Supabase")