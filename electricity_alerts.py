"""寝室电费低余额提醒：仅 QQ 邮箱。

规则：
- 余额 < 1 元：发送 QQ 邮箱提醒一次
- 余额持续 < 1 元：不重复发送
- 余额恢复到 >= 1 元：重置状态
- 以后再次跌破 1 元：再次发送提醒

敏感信息全部从 Windows 环境变量读取，不要写入 GitHub。

需要的环境变量：
- ELECTRICITY_EMAIL_SENDER
- ELECTRICITY_EMAIL_AUTH_CODE
- ELECTRICITY_EMAIL_RECEIVER
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

LOW_BALANCE_THRESHOLD = 1.0
STATE_FILE = Path(__file__).with_name("alert_state.json")


def _load_state() -> dict:
    """读取提醒状态，并兼容之前的旧版本状态文件。"""
    default = {"email_sent": False}

    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            default["email_sent"] = bool(data.get("email_sent", False))
    except Exception as exc:
        print(f"[提醒] 读取状态文件失败，将按未提醒处理：{exc}")

    return default


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[提醒] 保存状态文件失败：{exc}")


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def send_email_warning(balance: float) -> bool:
    sender = os.getenv("ELECTRICITY_EMAIL_SENDER", "").strip()
    auth_code = os.getenv("ELECTRICITY_EMAIL_AUTH_CODE", "").strip()
    receiver = os.getenv("ELECTRICITY_EMAIL_RECEIVER", "").strip() or sender

    if not sender or not auth_code or not receiver:
        print("[提醒] QQ 邮箱环境变量未配置完整，跳过邮箱提醒。")
        return False

    subject = f"寝室电费不足：余额仅 {balance:.2f} 元"
    body = (
        "寝室电费余额已低于 1 元。\n\n"
        f"当前余额：{balance:.2f} 元\n"
        f"采集时间：{_now_text()}\n\n"
        "请尽快充值。\n\n"
        "DormElectricMonitor 自动提醒"
    )

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = sender
    msg["To"] = receiver

    try:
        with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=20) as server:
            server.login(sender, auth_code)
            server.sendmail(sender, [receiver], msg.as_string())

        print(f"[提醒] QQ 邮箱提醒发送成功：{balance:.2f} 元")
        return True

    except Exception as exc:
        print(f"[提醒] QQ 邮箱提醒发送失败：{exc}")
        return False


def handle_low_balance_alerts(balance: float) -> None:
    """每次成功获取余额并写入 Supabase 后调用。"""
    balance = float(balance)
    state = _load_state()

    # 余额恢复到 1 元及以上，允许下一次跌破 1 元时重新提醒。
    if balance >= LOW_BALANCE_THRESHOLD:
        if state.get("email_sent"):
            print("[提醒] 余额已恢复到 1 元及以上，低余额提醒状态已重置。")

        _save_state({"email_sent": False})
        return

    # 余额低于 1 元，同一次低余额周期只发一次。
    if not state.get("email_sent"):
        if send_email_warning(balance):
            state["email_sent"] = True
            _save_state(state)
    else:
        print(
            f"[提醒] 当前余额 {balance:.2f} 元仍低于 1 元，"
            "本轮已发送过邮箱提醒，不重复发送。"
        )


if __name__ == "__main__":
    # 测试命令：
    # python electricity_alerts.py email
    # python electricity_alerts.py 0.80
    # python electricity_alerts.py 5.00

    arg = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""

    if arg == "email":
        raise SystemExit(0 if send_email_warning(0.80) else 1)

    if arg:
        try:
            handle_low_balance_alerts(float(arg))
        except ValueError:
            print("用法：python electricity_alerts.py [email|余额数字]")
            raise SystemExit(2)
    else:
        print("用法：python electricity_alerts.py [email|余额数字]")
