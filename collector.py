import json
import os
import subprocess
import sys
import time


# ============================================================
# 1. 基本配置
# ============================================================

# 你的寝室 roomId
ROOM_ID = "9979"

# Windows 环境变量
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv(
    "SUPABASE_SERVICE_ROLE_KEY", ""
).strip()


# ============================================================
# 2. 导入低余额邮件提醒
# ============================================================

try:
    from electricity_alerts import handle_low_balance_alerts
except ImportError:
    handle_low_balance_alerts = None


# ============================================================
# 3. 检查环境变量
# ============================================================

def check_config():
    """
    检查 Supabase 环境变量是否已经配置。
    """

    if not SUPABASE_URL:
        raise RuntimeError(
            "没有检测到环境变量 SUPABASE_URL"
        )

    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "没有检测到环境变量 SUPABASE_SERVICE_ROLE_KEY"
        )

    if not SUPABASE_URL.startswith("https://"):
        raise RuntimeError(
            f"SUPABASE_URL 格式不正确：{SUPABASE_URL}"
        )


# ============================================================
# 4. 查询西安交通大学寝室电费
# ============================================================

def get_balance():
    """
    使用 Windows curl.exe 查询学校电费接口。

    之前 Python requests / urllib 可能出现 HTTP 403，
    因此这里直接使用 Windows 自带的 curl.exe。
    """

    timestamp = int(time.time() * 1000)

    url = (
        "https://ssn.xjtu.edu.cn/cems/mobile/"
        "meterAccount/electricity"
        f"?roomId={ROOM_ID}&_={timestamp}"
    )

    command = [
        "curl.exe",

        # 安静模式，但保留错误信息
        "-sS",

        # HTTP 4xx / 5xx 时让 curl 返回非 0
        "--fail-with-body",

        # 建立连接最多等待 10 秒
        "--connect-timeout",
        "10",

        # 整个请求最多运行 20 秒
        "--max-time",
        "20",

        # 请求头
        "-H",
        "Accept: application/json, text/plain, */*",

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
        raise RuntimeError(
            "学校服务器返回了空数据"
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "学校服务器返回的内容不是合法 JSON：\n"
            f"{raw[:500]}"
        ) from exc

    # 正常响应通常类似：
    #
    # {
    #     "code": 0,
    #     "data": 33.48,
    #     "msg": "操作成功"
    # }

    code = payload.get("code")

    if code not in (0, "0", 200, "200", None):
        raise RuntimeError(
            f"学校接口返回查询失败：{payload}"
        )

    data = payload.get("data")

    # 兼容 data 直接就是余额数字
    if isinstance(data, (int, float, str)):
        try:
            balance = float(data)
        except ValueError as exc:
            raise RuntimeError(
                f"无法解析余额：{data}"
            ) from exc

    # 兼容以后接口可能改成对象
    elif isinstance(data, dict):

        balance = None

        possible_keys = [
            "balance",
            "remainMoney",
            "surplus",
            "money",
            "value",
        ]

        for key in possible_keys:
            if key in data:
                try:
                    balance = float(data[key])
                    break
                except (ValueError, TypeError):
                    pass

        if balance is None:
            raise RuntimeError(
                f"接口响应中没有找到余额：{payload}"
            )

    else:
        raise RuntimeError(
            f"接口响应中没有找到余额：{payload}"
        )

    if balance < 0:
        raise RuntimeError(
            f"检测到异常余额：{balance}"
        )

    return round(balance, 2)


# ============================================================
# 5. 写入 Supabase
# ============================================================

def save_to_supabase(balance):
    """
    把当前余额写入 Supabase electricity 表。

    electricity 表只需要：
        id
        created_at
        balance

    created_at 由 Supabase 自动生成。
    """

    url = (
        SUPABASE_URL.rstrip("/")
        + "/rest/v1/electricity"
    )

    payload = json.dumps(
        {
            "balance": balance
        },
        ensure_ascii=False,
    )

    command = [
        "curl.exe",

        "-sS",

        "--fail-with-body",

        "--connect-timeout",
        "10",

        "--max-time",
        "20",

        "-X",
        "POST",

        url,

        "-H",
        f"apikey: {SUPABASE_SERVICE_ROLE_KEY}",

        "-H",
        (
            "Authorization: Bearer "
            f"{SUPABASE_SERVICE_ROLE_KEY}"
        ),

        "-H",
        "Content-Type: application/json",

        "-H",
        "Prefer: return=minimal",

        "--data",
        payload,
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


# ============================================================
# 6. 低余额提醒
# ============================================================

def check_low_balance_alert(balance):
    """
    调用 electricity_alerts.py。

    当前逻辑：

        balance >= 1 元
            不发送

        balance < 1 元
            QQ 邮箱提醒一次

        持续低于 1 元
            不重复提醒

        充值恢复 >= 1 元
            重置提醒状态
    """

    if handle_low_balance_alerts is None:
        print(
            "[提醒] 没有找到 electricity_alerts.py，"
            "跳过低余额提醒。"
        )
        return

    try:
        handle_low_balance_alerts(balance)

    except Exception as exc:

        # 提醒失败不能影响电费采集和 Supabase 数据保存
        print(
            f"[提醒] 检查低余额提醒时发生错误：{exc}"
        )


# ============================================================
# 7. 主程序
# ============================================================

def main():

    print("=" * 50)
    print("DormElectricMonitor 电费自动采集")
    print("=" * 50)

    # --------------------------------------------------------
    # 检查配置
    # --------------------------------------------------------

    print("正在检查配置……")

    check_config()

    print("配置检查正常")


    # --------------------------------------------------------
    # 查询学校电费
    # --------------------------------------------------------

    print()
    print("开始查询寝室电费……")

    balance = get_balance()

    print(
        f"当前寝室电费：{balance:.2f} 元"
    )


    # --------------------------------------------------------
    # 写入 Supabase
    # --------------------------------------------------------

    print()
    print("正在写入 Supabase……")

    save_to_supabase(balance)

    print("Supabase 写入成功")


    # --------------------------------------------------------
    # 判断低余额
    # --------------------------------------------------------

    print()
    print("正在检查低余额提醒……")

    check_low_balance_alert(balance)


    # --------------------------------------------------------
    # 完成
    # --------------------------------------------------------

    print()
    print(
        f"本次采集完成，当前余额：{balance:.2f} 元"
    )

    print("=" * 50)


# ============================================================
# 8. 程序入口
# ============================================================

if __name__ == "__main__":

    try:
        main()

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