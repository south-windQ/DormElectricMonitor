import json
import os
import subprocess
import sys
import time

ROOMS = [
    {"id": "9979", "name": "LJH(B1 0680203)", "enable_alert": True},
    {"id": "9468", "name": "ZYW(B1 0370202)", "enable_alert": False},
]

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

try:
    from electricity_alerts import handle_low_balance_alerts
except ImportError:
    handle_low_balance_alerts = None


def check_config():
    if not SUPABASE_URL:
        raise RuntimeError("没有检测到环境变量 SUPABASE_URL")
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("没有检测到环境变量 SUPABASE_SERVICE_ROLE_KEY")
    if not SUPABASE_URL.startswith("https://"):
        raise RuntimeError(f"SUPABASE_URL 格式不正确：{SUPABASE_URL}")


def get_balance(room_id):
    timestamp = int(time.time() * 1000)
    url = (
        "https://ssn.xjtu.edu.cn/cems/mobile/"
        "meterAccount/electricity"
        f"?roomId={room_id}&_={timestamp}"
    )

    command = [
        "curl.exe",
        "-sS",
        "--fail-with-body",
        "--connect-timeout", "10",
        "--max-time", "20",
        "-H", "Accept: application/json, text/plain, */*",
        "-H",
        (
            "User-Agent: Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/152.0 Safari/537.36"
        ),
        url,
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if result.returncode != 0:
        raise RuntimeError(
            "学校电费查询失败。\n"
            f"curl 退出码：{result.returncode}\n"
            f"错误信息：{result.stderr.strip()}\n"
            f"服务器响应：{result.stdout.strip()}"
        )

    raw = result.stdout.strip()
    if not raw:
        raise RuntimeError("学校服务器返回了空数据")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "学校服务器返回的内容不是合法 JSON：\n"
            f"{raw[:500]}"
        ) from exc

    code_value = payload.get("code")
    if code_value not in (0, "0", 200, "200", None):
        raise RuntimeError(f"学校接口返回查询失败：{payload}")

    data = payload.get("data")

    if isinstance(data, (int, float, str)):
        try:
            balance = float(data)
        except ValueError as exc:
            raise RuntimeError(f"无法解析余额：{data}") from exc
    elif isinstance(data, dict):
        balance = None
        for key in ["balance", "remainMoney", "surplus", "money", "value"]:
            if key in data:
                try:
                    balance = float(data[key])
                    break
                except (ValueError, TypeError):
                    pass
        if balance is None:
            raise RuntimeError(f"接口响应中没有找到余额：{payload}")
    else:
        raise RuntimeError(f"接口响应中没有找到余额：{payload}")

    if balance < 0:
        raise RuntimeError(f"检测到异常余额：{balance}")

    return round(balance, 2)


def save_to_supabase(room_id, room_name, balance):
    url = SUPABASE_URL.rstrip("/") + "/rest/v1/electricity"

    payload = json.dumps(
        {
            "balance": balance,
            "room_id": room_id,
            "room_name": room_name,
        },
        ensure_ascii=False,
    )

    command = [
        "curl.exe",
        "-sS",
        "--fail-with-body",
        "--connect-timeout", "10",
        "--max-time", "20",
        "-X", "POST",
        url,
        "-H", f"apikey: {SUPABASE_SERVICE_ROLE_KEY}",
        "-H", f"Authorization: Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "-H", "Content-Type: application/json",
        "-H", "Prefer: return=minimal",
        "--data", payload,
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Supabase 写入失败。\n"
            f"curl 退出码：{result.returncode}\n"
            f"错误信息：{result.stderr.strip()}\n"
            f"服务器响应：{result.stdout.strip()}"
        )


def check_low_balance_alert(balance):
    if handle_low_balance_alerts is None:
        print("[提醒] 没有找到 electricity_alerts.py，跳过低余额提醒。")
        return

    try:
        handle_low_balance_alerts(balance)
    except Exception as exc:
        print(f"[提醒] 检查低余额提醒时发生错误：{exc}")


def collect_one_room(room):
    room_id = room["id"]
    room_name = room["name"]

    print("-" * 50)
    print(f"正在采集：{room_name}")
    print(f"Room ID：{room_id}")

    balance = get_balance(room_id)
    print(f"当前余额：{balance:.2f} 元")

    print("正在写入 Supabase……")
    save_to_supabase(room_id, room_name, balance)
    print("Supabase 写入成功")

    if room.get("enable_alert", False):
        print("正在检查低余额提醒……")
        check_low_balance_alert(balance)
    else:
        print("该寝室未启用低余额提醒，跳过。")

    print(f"采集完成：{room_name} / {balance:.2f} 元")


def main():
    print("=" * 50)
    print("DormElectricMonitor 多寝室电费自动采集")
    print("=" * 50)

    print("正在检查配置……")
    check_config()
    print("配置检查正常")
    print()

    failed_rooms = []

    for index, room in enumerate(ROOMS):
        try:
            collect_one_room(room)
        except Exception as exc:
            failed_rooms.append(room["name"])
            print(f"[失败] {room['name']} 采集失败：{exc}")

        if index < len(ROOMS) - 1:
            time.sleep(1)

        print()

    print("=" * 50)

    if failed_rooms:
        print("本次存在采集失败的寝室：" + "、".join(failed_rooms))
        print("=" * 50)
        return 1

    print("全部寝室采集完成")
    print("=" * 50)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        print("程序已被手动终止")
        sys.exit(130)
    except Exception as exc:
        print()
        print("=" * 50)
        print("本次电费采集失败")
        print("=" * 50)
        print(exc)
        print("=" * 50)
        sys.exit(1)
