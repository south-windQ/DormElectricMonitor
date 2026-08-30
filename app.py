import calendar
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Flask, jsonify, render_template_string, request


# =========================================================
# 基本配置
# =========================================================

ROOM_ID = "9979"
DATABASE = "electricity.db"
QUERY_TIMEOUT_SECONDS = 15
LOW_BALANCE_THRESHOLD = 20.0
BALANCE_EPSILON = 0.005
CHINA_TIMEZONE = timezone(timedelta(hours=8))

app = Flask(__name__)
query_lock = threading.Lock()

runtime_state = {
    "querying": False,
    "last_error": None,
    "next_query_at": None,
}


def now_china():
    """返回无时区标记的北京时间，便于 SQLite 排序。"""
    return datetime.now(CHINA_TIMEZONE).replace(tzinfo=None)


def format_time(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def parse_time(value):
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


# =========================================================
# 1. 查询寝室电费
# =========================================================

def extract_balance(payload):
    """兼容接口直接返回数字或把余额放在对象字段中的情况。"""
    data = payload.get("data")

    if isinstance(data, (int, float, str)):
        return float(data)

    if isinstance(data, dict):
        for key in ("balance", "remainMoney", "surplus", "money", "value"):
            if key in data:
                return float(data[key])

    raise RuntimeError(f"接口响应中没有找到余额：{payload}")


def get_balance():
    """从西交大电费接口获取当前余额，不依赖 curl.exe。"""
    timestamp = int(time.time() * 1000)
    url = (
        "https://ssn.xjtu.edu.cn/cems/mobile/"
        f"meterAccount/electricity?roomId={ROOM_ID}&_={timestamp}"
    )
    req = Request(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 ElectricityMonitor/2.0",
        },
    )

    try:
        with urlopen(req, timeout=QUERY_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"请求学校服务器失败：{exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"服务器返回内容无法解析：{raw[:200]}") from exc

    code = payload.get("code")
    if code not in (None, 0, "0", 200, "200"):
        raise RuntimeError(f"查询失败：{payload}")

    balance = extract_balance(payload)
    if balance < 0:
        raise RuntimeError(f"接口返回了异常余额：{balance}")
    return round(balance, 2)


# =========================================================
# 2. SQLite 数据库
# =========================================================

def get_db():
    conn = sqlite3.connect(DATABASE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_database():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS electricity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT NOT NULL,
            balance REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS query_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT NOT NULL,
            source TEXT NOT NULL,
            success INTEGER NOT NULL,
            message TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_electricity_time ON electricity(time)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_query_log_time ON query_log(time)"
    )
    conn.commit()
    conn.close()


def save_balance(balance):
    measured_at = format_time(now_china())
    conn = get_db()
    conn.execute(
        "INSERT INTO electricity (time, balance) VALUES (?, ?)",
        (measured_at, balance),
    )
    conn.commit()
    conn.close()
    print(f"[{measured_at}] 当前寝室电费：{balance:.2f} 元")
    return measured_at


def save_query_log(source, success, message):
    conn = get_db()
    conn.execute(
        "INSERT INTO query_log (time, source, success, message) VALUES (?, ?, ?, ?)",
        (format_time(now_china()), source, int(success), message),
    )
    conn.commit()
    conn.close()


def get_latest_balance():
    conn = get_db()
    row = conn.execute(
        "SELECT time, balance FROM electricity ORDER BY time DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return {"time": row["time"], "balance": float(row["balance"])}


def get_records(start_time=None, end_time=None, include_anchor=False):
    """读取时间范围；include_anchor=True 时附带起点前最后一条。"""
    conn = get_db()
    records = []

    if include_anchor and start_time is not None:
        anchor = conn.execute(
            """
            SELECT time, balance FROM electricity
            WHERE time < ? ORDER BY time DESC LIMIT 1
            """,
            (format_time(start_time),),
        ).fetchone()
        if anchor:
            records.append(anchor)

    conditions = []
    params = []
    if start_time is not None:
        conditions.append("time >= ?")
        params.append(format_time(start_time))
    if end_time is not None:
        conditions.append("time <= ?")
        params.append(format_time(end_time))

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = conn.execute(
        f"SELECT time, balance FROM electricity {where} ORDER BY time ASC",
        params,
    ).fetchall()
    conn.close()
    records.extend(rows)

    return [
        {"time": parse_time(row["time"]), "balance": float(row["balance"])}
        for row in records
    ]


# =========================================================
# 3. 查询任务
# =========================================================

def query_once(source="auto"):
    """执行一次查询，防止手动查询与整点任务同时写入。"""
    if not query_lock.acquire(blocking=False):
        return {"success": False, "message": "已有查询正在进行，请稍后再试"}

    runtime_state["querying"] = True
    try:
        balance = get_balance()
        measured_at = save_balance(balance)
        runtime_state["last_error"] = None
        save_query_log(source, True, f"余额 {balance:.2f} 元")
        return {
            "success": True,
            "message": "查询成功",
            "balance": balance,
            "time": measured_at,
        }
    except Exception as exc:
        message = str(exc)
        runtime_state["last_error"] = message
        save_query_log(source, False, message)
        print(f"[{format_time(now_china())}] 查询失败：{message}")
        return {"success": False, "message": message}
    finally:
        runtime_state["querying"] = False
        query_lock.release()


def next_full_hour(value):
    return value.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def hourly_worker():
    query_once("startup")
    while True:
        next_query = next_full_hour(now_china())
        runtime_state["next_query_at"] = format_time(next_query)
        wait_seconds = max(1, (next_query - now_china()).total_seconds())
        print(f"下一次查询时间：{format_time(next_query)}")
        time.sleep(wait_seconds)
        query_once("auto")


# =========================================================
# 4. 用电、充值与时间分摊
# =========================================================

def month_start(value):
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def add_months(value, amount):
    month_index = value.year * 12 + value.month - 1 + amount
    year, month_zero = divmod(month_index, 12)
    return value.replace(year=year, month=month_zero + 1, day=1)


def split_interval_by_day(start, end, amount):
    """把一个区间的金额或小时数按持续时间分摊到各自然日。"""
    if end <= start or amount <= 0:
        return []

    total_seconds = (end - start).total_seconds()
    cursor = start
    result = []

    while cursor < end:
        next_day = datetime.combine(
            cursor.date() + timedelta(days=1),
            datetime.min.time(),
        )
        segment_end = min(end, next_day)
        ratio = (segment_end - cursor).total_seconds() / total_seconds
        result.append((cursor.date().isoformat(), amount * ratio))
        cursor = segment_end

    return result


def analyse_records(records):
    daily_usage = {}
    daily_recharge = {}
    daily_coverage = {}
    activities = []
    long_gap_count = 0

    for previous, current in zip(records, records[1:]):
        start = previous["time"]
        end = current["time"]
        if end <= start:
            continue

        duration_hours = (end - start).total_seconds() / 3600
        if duration_hours > 2.5:
            long_gap_count += 1

        # 只有正常采集间隔才计入覆盖时长，长时间停机不冒充完整数据。
        if duration_hours <= 2.5:
            for day_key, hours in split_interval_by_day(
                start, end, duration_hours
            ):
                daily_coverage[day_key] = (
                    daily_coverage.get(day_key, 0) + hours
                )

        delta = previous["balance"] - current["balance"]
        if abs(delta) < BALANCE_EPSILON:
            continue

        if delta > 0:
            for day_key, value in split_interval_by_day(start, end, delta):
                daily_usage[day_key] = daily_usage.get(day_key, 0) + value
            activity_type = "usage"
            amount = delta
        else:
            # 余额上升说明充值，发生时间近似记为当前采样时刻。
            day_key = end.date().isoformat()
            amount = -delta
            daily_recharge[day_key] = (
                daily_recharge.get(day_key, 0) + amount
            )
            activity_type = "recharge"

        activities.append(
            {
                "time": format_time(end),
                "type": activity_type,
                "amount": round(amount, 2),
                "from_balance": round(previous["balance"], 2),
                "to_balance": round(current["balance"], 2),
                "duration_hours": round(duration_hours, 1),
            }
        )

    return {
        "daily_usage": daily_usage,
        "daily_recharge": daily_recharge,
        "daily_coverage": daily_coverage,
        "activities": activities,
        "long_gap_count": long_gap_count,
    }


def build_dashboard(days=30, months=6):
    now = now_china()
    today = now.date()
    daily_start = datetime.combine(
        today - timedelta(days=days - 1),
        datetime.min.time(),
    )
    monthly_start = add_months(month_start(now), -(months - 1))
    analysis_start = min(daily_start, monthly_start)
    records = get_records(analysis_start, now, include_anchor=True)
    analysis = analyse_records(records)

    daily = []
    for offset in range(days - 1, -1, -1):
        day_value = today - timedelta(days=offset)
        key = day_value.isoformat()
        coverage = min(24.0, analysis["daily_coverage"].get(key, 0))
        daily.append(
            {
                "date": key,
                "usage": round(analysis["daily_usage"].get(key, 0), 2),
                "recharge": round(
                    analysis["daily_recharge"].get(key, 0), 2
                ),
                "coverage_hours": round(coverage, 1),
                "complete": coverage >= 18,
            }
        )

    monthly = []
    current_month = month_start(now)
    for offset in range(-(months - 1), 1):
        start = add_months(current_month, offset)
        end = add_months(start, 1)
        start_key = start.date().isoformat()
        end_key = end.date().isoformat()
        usage = sum(
            value
            for day_key, value in analysis["daily_usage"].items()
            if start_key <= day_key < end_key
        )
        recharge = sum(
            value
            for day_key, value in analysis["daily_recharge"].items()
            if start_key <= day_key < end_key
        )
        monthly.append(
            {
                "month": start.strftime("%Y-%m"),
                "usage": round(usage, 2),
                "recharge": round(recharge, 2),
            }
        )

    latest = get_latest_balance()
    today_item = daily[-1]
    yesterday_item = daily[-2] if len(daily) >= 2 else None
    current_month_item = monthly[-1]

    # 用近 7 个有较完整数据的已结束自然日估算日均。
    completed_days = [
        item for item in daily[:-1] if item["complete"]
    ][-7:]
    if not completed_days:
        completed_days = [
            item for item in daily[:-1]
            if item["coverage_hours"] > 0
        ][-7:]

    average_daily = (
        sum(item["usage"] for item in completed_days) / len(completed_days)
        if completed_days
        else 0
    )
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    projected_month = average_daily * days_in_month if average_daily > 0 else 0
    remaining_days = (
        latest["balance"] / average_daily
        if latest and average_daily > BALANCE_EPSILON
        else None
    )

    history_records = get_records(
        now - timedelta(hours=72),
        now,
        include_anchor=False,
    )
    history = [
        {
            "time": format_time(item["time"]),
            "balance": round(item["balance"], 2),
        }
        for item in history_records
    ]

    latest_age_minutes = None
    if latest:
        latest_age_minutes = max(
            0,
            int(
                (now - parse_time(latest["time"])).total_seconds()
                / 60
            ),
        )

    return {
        "room_id": ROOM_ID,
        "latest": latest,
        "summary": {
            "today_usage": today_item["usage"],
            "yesterday_usage": (
                yesterday_item["usage"] if yesterday_item else 0
            ),
            "month_usage": current_month_item["usage"],
            "month_recharge": current_month_item["recharge"],
            "average_daily": round(average_daily, 2),
            "projected_month": round(projected_month, 2),
            "remaining_days": (
                round(remaining_days, 1)
                if remaining_days is not None
                else None
            ),
        },
        "history": history,
        "daily": daily,
        "monthly": monthly,
        "activities": list(reversed(analysis["activities"][-12:])),
        "monitor": {
            "querying": runtime_state["querying"],
            "last_error": runtime_state["last_error"],
            "next_query_at": runtime_state["next_query_at"],
            "latest_age_minutes": latest_age_minutes,
            "long_gap_count": analysis["long_gap_count"],
            "low_balance": bool(
                latest
                and latest["balance"] < LOW_BALANCE_THRESHOLD
            ),
        },
    }


# =========================================================
# 5. API
# =========================================================

@app.get("/api/dashboard")
def dashboard_api():
    try:
        days = min(60, max(7, int(request.args.get("days", 30))))
    except ValueError:
        days = 30
    return jsonify(build_dashboard(days=days, months=6))


@app.post("/api/query-now")
def query_now_api():
    result = query_once("manual")
    return jsonify(result), (200 if result["success"] else 503)


# =========================================================
# 6. 网页
# =========================================================

HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#07131f">
<title>寝室电费看板</title>
<style>
:root {
    --bg: #07131f;
    --panel: rgba(17, 34, 50, .88);
    --text: #f5f8fb;
    --muted: #8fa6b8;
    --cyan: #4ee1c1;
    --blue: #55a7ff;
    --orange: #ffb45e;
    --red: #ff6b76;
    --border: rgba(255, 255, 255, .08);
    --shadow: 0 20px 50px rgba(0, 0, 0, .24);
}

* { box-sizing: border-box; }
body {
    margin: 0;
    min-height: 100vh;
    font-family: Inter, "Microsoft YaHei", system-ui, sans-serif;
    color: var(--text);
    background:
        radial-gradient(circle at 12% 0%, rgba(31,174,160,.18), transparent 30%),
        radial-gradient(circle at 90% 12%, rgba(57,112,210,.17), transparent 28%),
        var(--bg);
}
button { font: inherit; }
.container {
    width: min(1280px, calc(100% - 32px));
    margin: 0 auto;
    padding: 30px 0 46px;
}
.topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 18px;
    margin-bottom: 26px;
}
.brand { display: flex; align-items: center; gap: 14px; }
.logo {
    width: 50px;
    height: 50px;
    display: grid;
    place-items: center;
    border-radius: 16px;
    background: linear-gradient(145deg, var(--cyan), #24a5cd);
    color: #06201f;
    font-size: 25px;
    box-shadow: 0 12px 30px rgba(78,225,193,.18);
}
h1 {
    margin: 0 0 5px;
    font-size: clamp(22px, 3vw, 30px);
    letter-spacing: -.5px;
}
.subtitle {
    color: var(--muted);
    font-size: 13px;
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
}
.status-pill {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 5px 10px;
    border-radius: 999px;
    background: rgba(78,225,193,.1);
    color: var(--cyan);
}
.status-pill::before {
    content: "";
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: currentColor;
    box-shadow: 0 0 10px currentColor;
}
.status-pill.warning {
    color: var(--orange);
    background: rgba(255,180,94,.1);
}
.status-pill.error {
    color: var(--red);
    background: rgba(255,107,118,.1);
}
.refresh-btn {
    border: 1px solid var(--border);
    color: var(--text);
    background: rgba(255,255,255,.06);
    border-radius: 12px;
    padding: 11px 16px;
    cursor: pointer;
    transition: .2s ease;
}
.refresh-btn:hover {
    background: rgba(255,255,255,.11);
    transform: translateY(-1px);
}
.refresh-btn:disabled { opacity: .55; cursor: wait; transform: none; }

.hero-grid {
    display: grid;
    grid-template-columns: 1.35fr repeat(3, 1fr);
    gap: 16px;
    margin-bottom: 16px;
}
.card, .panel {
    border: 1px solid var(--border);
    background: linear-gradient(145deg, var(--panel), rgba(12,27,41,.9));
    border-radius: 20px;
    box-shadow: var(--shadow);
    backdrop-filter: blur(12px);
}
.metric {
    min-height: 164px;
    padding: 22px;
    position: relative;
    overflow: hidden;
}
.metric::after {
    content: "";
    position: absolute;
    width: 110px;
    height: 110px;
    border-radius: 50%;
    right: -35px;
    top: -38px;
    background: currentColor;
    opacity: .055;
}
.metric-label {
    color: var(--muted);
    font-size: 14px;
    margin-bottom: 20px;
}
.metric-value {
    display: flex;
    align-items: baseline;
    gap: 7px;
    font-size: clamp(29px, 4vw, 42px);
    font-weight: 750;
    letter-spacing: -1.5px;
}
.metric-unit {
    color: var(--muted);
    font-size: 14px;
    font-weight: 500;
    letter-spacing: 0;
}
.metric-note {
    color: var(--muted);
    font-size: 12px;
    margin-top: 15px;
}
.balance-card {
    background: linear-gradient(145deg, rgba(23,70,68,.96), rgba(13,39,47,.94));
    color: var(--cyan);
}
.balance-card .metric-label,
.balance-card .metric-unit,
.balance-card .metric-note { color: rgba(227,255,249,.68); }

.mini-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
    margin-bottom: 16px;
}
.mini { padding: 16px 18px; }
.mini-label {
    color: var(--muted);
    font-size: 12px;
    margin-bottom: 8px;
}
.mini-value { font-size: 21px; font-weight: 700; }
.mini-value span {
    color: var(--muted);
    font-size: 12px;
    font-weight: 500;
    margin-left: 4px;
}
.notice {
    display: none;
    border: 1px solid rgba(255,180,94,.25);
    background: rgba(255,180,94,.08);
    color: #ffd09a;
    border-radius: 14px;
    padding: 12px 15px;
    margin-bottom: 16px;
    font-size: 13px;
}
.notice.show { display: block; }
.content-grid {
    display: grid;
    grid-template-columns: minmax(0,1.65fr) minmax(310px,.85fr);
    gap: 16px;
    margin-bottom: 16px;
}
.panel { padding: 21px; min-width: 0; }
.panel-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 15px;
    margin-bottom: 17px;
}
.panel-title { font-size: 16px; font-weight: 700; }
.panel-desc {
    color: var(--muted);
    font-size: 12px;
    margin-top: 5px;
}
.tabs {
    display: flex;
    gap: 4px;
    background: rgba(255,255,255,.045);
    padding: 4px;
    border-radius: 10px;
}
.tab {
    border: 0;
    background: transparent;
    color: var(--muted);
    padding: 6px 10px;
    border-radius: 7px;
    cursor: pointer;
    font-size: 12px;
}
.tab.active { background: rgba(78,225,193,.14); color: var(--cyan); }
.chart { width: 100%; height: 275px; display: block; }
.activity-list { max-height: 319px; overflow: auto; padding-right: 3px; }
.activity {
    display: grid;
    grid-template-columns: 36px 1fr auto;
    gap: 11px;
    align-items: center;
    padding: 12px 0;
    border-bottom: 1px solid var(--border);
}
.activity:last-child { border-bottom: 0; }
.activity-icon {
    width: 34px;
    height: 34px;
    display: grid;
    place-items: center;
    border-radius: 11px;
    background: rgba(85,167,255,.11);
    color: var(--blue);
}
.activity.recharge .activity-icon {
    color: var(--cyan);
    background: rgba(78,225,193,.11);
}
.activity-title { font-size: 13px; font-weight: 650; }
.activity-time { color: var(--muted); font-size: 11px; margin-top: 3px; }
.activity-amount { font-weight: 700; font-size: 14px; }
.activity.recharge .activity-amount { color: var(--cyan); }
.empty {
    color: var(--muted);
    text-align: center;
    padding: 72px 10px;
    font-size: 13px;
}
.footnote {
    color: var(--muted);
    font-size: 12px;
    line-height: 1.7;
    padding: 2px 3px;
}
.toast {
    position: fixed;
    right: 20px;
    bottom: 20px;
    max-width: min(360px, calc(100% - 40px));
    padding: 12px 16px;
    border-radius: 12px;
    background: #173049;
    box-shadow: var(--shadow);
    font-size: 13px;
    transform: translateY(90px);
    opacity: 0;
    transition: .25s;
    z-index: 10;
}
.toast.show { transform: translateY(0); opacity: 1; }
.toast.error { background: #5a2630; }

@media (max-width: 980px) {
    .hero-grid { grid-template-columns: repeat(2, 1fr); }
    .content-grid { grid-template-columns: 1fr; }
}
@media (max-width: 620px) {
    .container {
        width: min(100% - 20px, 1280px);
        padding-top: 18px;
    }
    .topbar { align-items: flex-start; }
    .logo { width: 43px; height: 43px; border-radius: 13px; }
    .refresh-btn {
        padding: 9px 11px;
        font-size: 12px;
        white-space: nowrap;
    }
    .hero-grid { grid-template-columns: 1fr; gap: 11px; }
    .metric { min-height: 137px; padding: 19px; }
    .metric-label { margin-bottom: 12px; }
    .mini-grid { grid-template-columns: repeat(2, 1fr); }
    .panel { padding: 16px 13px; border-radius: 17px; }
    .chart { height: 235px; }
    .panel-head { flex-direction: column; }
}
</style>
</head>
<body>
<main class="container">
    <header class="topbar">
        <div class="brand">
            <div class="logo">⚡</div>
            <div>
                <h1>寝室电费看板</h1>
                <div class="subtitle">
                    <span>房间 <strong id="room">--</strong></span>
                    <span id="statusPill" class="status-pill">正在读取</span>
                    <span id="nextQuery">下次查询 --</span>
                </div>
            </div>
        </div>
        <button id="refreshBtn" class="refresh-btn" type="button">
            立即查询
        </button>
    </header>

    <div id="notice" class="notice"></div>

    <section class="hero-grid">
        <article class="card metric balance-card">
            <div class="metric-label">当前余额</div>
            <div class="metric-value">
                <span id="balance">--</span>
                <span class="metric-unit">元</span>
            </div>
            <div id="lastUpdate" class="metric-note">尚无查询记录</div>
        </article>
        <article class="card metric">
            <div class="metric-label">今日用电</div>
            <div class="metric-value">
                <span id="todayUsage">--</span>
                <span class="metric-unit">元</span>
            </div>
            <div id="yesterdayCompare" class="metric-note">昨日 -- 元</div>
        </article>
        <article class="card metric">
            <div class="metric-label">本月累计用电</div>
            <div class="metric-value">
                <span id="monthUsage">--</span>
                <span class="metric-unit">元</span>
            </div>
            <div id="monthProjection" class="metric-note">月末预计 -- 元</div>
        </article>
        <article class="card metric">
            <div class="metric-label">预计还可使用</div>
            <div class="metric-value">
                <span id="remainingDays">--</span>
                <span class="metric-unit">天</span>
            </div>
            <div class="metric-note">按近 7 个有效日均值估算</div>
        </article>
    </section>

    <section class="mini-grid">
        <article class="card mini">
            <div class="mini-label">昨日用电</div>
            <div class="mini-value">
                <b id="yesterdayUsage">--</b><span>元</span>
            </div>
        </article>
        <article class="card mini">
            <div class="mini-label">近 7 个有效日日均</div>
            <div class="mini-value">
                <b id="averageDaily">--</b><span>元/天</span>
            </div>
        </article>
        <article class="card mini">
            <div class="mini-label">本月充值</div>
            <div class="mini-value">
                <b id="monthRecharge">--</b><span>元</span>
            </div>
        </article>
        <article class="card mini">
            <div class="mini-label">月末预计</div>
            <div class="mini-value">
                <b id="projectedMonth">--</b><span>元</span>
            </div>
        </article>
    </section>

    <section class="content-grid">
        <article class="panel">
            <div class="panel-head">
                <div>
                    <div class="panel-title">每日用电</div>
                    <div class="panel-desc">
                        按自然日统计；跨午夜区间按时长分摊
                    </div>
                </div>
                <div class="tabs">
                    <button class="tab active" data-days="7">近 7 天</button>
                    <button class="tab" data-days="30">近 30 天</button>
                </div>
            </div>
            <canvas id="dailyChart" class="chart"></canvas>
        </article>
        <article class="panel">
            <div class="panel-head">
                <div>
                    <div class="panel-title">最近余额变动</div>
                    <div class="panel-desc">
                        余额下降为用电，余额上升为充值
                    </div>
                </div>
            </div>
            <div id="activityList" class="activity-list"></div>
        </article>
    </section>

    <section class="content-grid">
        <article class="panel">
            <div class="panel-head">
                <div>
                    <div class="panel-title">最近 72 小时余额</div>
                    <div class="panel-desc">查看用电速度和充值节点</div>
                </div>
            </div>
            <canvas id="balanceChart" class="chart"></canvas>
        </article>
        <article class="panel">
            <div class="panel-head">
                <div>
                    <div class="panel-title">近 6 个月用电</div>
                    <div class="panel-desc">
                        长期运行后月度趋势会逐渐完整
                    </div>
                </div>
            </div>
            <canvas id="monthlyChart" class="chart"></canvas>
        </article>
    </section>

    <div class="footnote">
        统计说明：用电金额是相邻两次查询的余额下降额；余额上升会作为充值记录。
        若两次采样之间既充值又用电，仅凭余额无法精确拆分，因此充值所在区间的实际
        用电可能被低估。程序停机期间的长间隔数据会参与估算，但不计为完整采集时长。
    </div>
</main>
<div id="toast" class="toast"></div>

<script>
let dashboardData = null;
let dailyRange = 7;

const $ = id => document.getElementById(id);
const money = value => Number(value || 0).toFixed(2);
const shortDate = value => (
    value ? value.slice(5, 10).replace("-", "/") : "--"
);

function showToast(message, isError = false) {
    const node = $("toast");
    node.textContent = message;
    node.className = `toast show${isError ? " error" : ""}`;
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => node.className = "toast", 3000);
}

function setupCanvas(canvas) {
    const rect = canvas.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.floor(rect.width * ratio));
    canvas.height = Math.max(1, Math.floor(rect.height * ratio));
    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    return { ctx, width: rect.width, height: rect.height };
}

function emptyChart(ctx, text) {
    ctx.fillStyle = "#8fa6b8";
    ctx.font = '13px "Microsoft YaHei"';
    ctx.textAlign = "center";
    ctx.fillText(
        text,
        ctx.canvas.clientWidth / 2,
        ctx.canvas.clientHeight / 2
    );
}

function drawChart(canvas, labels, values, options = {}) {
    const { ctx, width, height } = setupCanvas(canvas);
    ctx.clearRect(0, 0, width, height);
    if (!values || !values.length) {
        emptyChart(ctx, "暂无数据");
        return;
    }

    const compact = width < 520;
    const pad = { left: 43, right: 12, top: 20, bottom: 36 };
    const chartW = width - pad.left - pad.right;
    const chartH = height - pad.top - pad.bottom;
    let min = options.zeroBased === false ? Math.min(...values) : 0;
    let max = Math.max(...values);

    if (max === min) {
        max += 1;
        min = Math.max(0, min - 1);
    }
    if (options.zeroBased !== false) {
        max = Math.max(1, max * 1.16);
    } else {
        const margin = Math.max((max - min) * .18, .3);
        min -= margin;
        max += margin;
    }

    ctx.lineWidth = 1;
    ctx.font = "11px Inter, Arial";
    for (let i = 0; i <= 4; i++) {
        const y = pad.top + chartH * i / 4;
        ctx.strokeStyle = "rgba(255,255,255,.07)";
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(width - pad.right, y);
        ctx.stroke();
        ctx.fillStyle = "#7890a4";
        ctx.textAlign = "right";
        ctx.fillText(
            (max - (max - min) * i / 4).toFixed(1),
            pad.left - 8,
            y + 4
        );
    }

    const color = options.color || "#4ee1c1";
    const xAt = i => (
        pad.left
        + (
            values.length === 1
                ? chartW / 2
                : i * chartW / (values.length - 1)
        )
    );
    const yAt = value => (
        pad.top + (max - value) / (max - min) * chartH
    );

    if (options.type === "bar") {
        const slot = chartW / values.length;
        const barW = Math.max(3, Math.min(38, slot * .58));
        values.forEach((value, i) => {
            const x = pad.left + i * slot + (slot - barW) / 2;
            const y = yAt(value);
            const gradient = ctx.createLinearGradient(
                0, y, 0, pad.top + chartH
            );
            gradient.addColorStop(0, color);
            gradient.addColorStop(
                1,
                options.endColor || "rgba(78,225,193,.18)"
            );
            ctx.fillStyle = gradient;
            ctx.beginPath();
            if (ctx.roundRect) {
                ctx.roundRect(
                    x,
                    y,
                    barW,
                    Math.max(2, pad.top + chartH - y),
                    [5, 5, 1, 1]
                );
            } else {
                ctx.rect(
                    x,
                    y,
                    barW,
                    Math.max(2, pad.top + chartH - y)
                );
            }
            ctx.fill();
            if (values.length <= 10 && value > 0) {
                ctx.fillStyle = "#cfe0ec";
                ctx.textAlign = "center";
                ctx.font = "10px Inter, Arial";
                ctx.fillText(
                    value.toFixed(1),
                    x + barW / 2,
                    Math.max(12, y - 6)
                );
            }
        });
    } else {
        const gradient = ctx.createLinearGradient(
            0, pad.top, 0, pad.top + chartH
        );
        gradient.addColorStop(0, "rgba(85,167,255,.30)");
        gradient.addColorStop(1, "rgba(85,167,255,0)");
        ctx.beginPath();
        values.forEach((value, i) => {
            if (i) ctx.lineTo(xAt(i), yAt(value));
            else ctx.moveTo(xAt(i), yAt(value));
        });
        ctx.lineTo(xAt(values.length - 1), pad.top + chartH);
        ctx.lineTo(xAt(0), pad.top + chartH);
        ctx.closePath();
        ctx.fillStyle = gradient;
        ctx.fill();

        ctx.beginPath();
        values.forEach((value, i) => {
            if (i) ctx.lineTo(xAt(i), yAt(value));
            else ctx.moveTo(xAt(i), yAt(value));
        });
        ctx.strokeStyle = color;
        ctx.lineWidth = 2.2;
        ctx.lineJoin = "round";
        ctx.stroke();

        values.forEach((value, i) => {
            ctx.beginPath();
            ctx.arc(xAt(i), yAt(value), 2.7, 0, Math.PI * 2);
            ctx.fillStyle = color;
            ctx.fill();
        });
    }

    const labelEvery = Math.max(
        1,
        Math.ceil(labels.length / (compact ? 5 : 8))
    );
    ctx.fillStyle = "#7890a4";
    ctx.font = "10px Inter, Arial";
    ctx.textAlign = "center";
    labels.forEach((label, i) => {
        if (i % labelEvery === 0 || i === labels.length - 1) {
            const x = options.type === "bar"
                ? pad.left + (i + .5) * chartW / labels.length
                : xAt(i);
            ctx.fillText(label, x, height - 12);
        }
    });
}

function renderCharts() {
    if (!dashboardData) return;
    const daily = dashboardData.daily.slice(-dailyRange);
    drawChart(
        $("dailyChart"),
        daily.map(item => shortDate(item.date)),
        daily.map(item => item.usage),
        { type: "bar", color: "#4ee1c1" }
    );
    drawChart(
        $("balanceChart"),
        dashboardData.history.map(item => item.time.slice(5, 16)),
        dashboardData.history.map(item => item.balance),
        { zeroBased: false, color: "#55a7ff" }
    );
    drawChart(
        $("monthlyChart"),
        dashboardData.monthly.map(
            item => item.month.slice(2).replace("-", "/")
        ),
        dashboardData.monthly.map(item => item.usage),
        {
            type: "bar",
            color: "#ffb45e",
            endColor: "rgba(255,180,94,.16)",
        }
    );
}

function renderActivities(items) {
    const container = $("activityList");
    if (!items.length) {
        container.innerHTML = (
            '<div class="empty">积累两次查询记录后显示变动</div>'
        );
        return;
    }

    container.innerHTML = items.map(item => {
        const recharge = item.type === "recharge";
        return `<div class="activity ${recharge ? "recharge" : ""}">
            <div class="activity-icon">${recharge ? "＋" : "↘"}</div>
            <div>
                <div class="activity-title">
                    ${recharge ? "余额充值" : "用电消耗"}
                </div>
                <div class="activity-time">
                    ${item.time.slice(5,16)} ·
                    ${item.from_balance.toFixed(2)} →
                    ${item.to_balance.toFixed(2)} 元
                </div>
            </div>
            <div class="activity-amount">
                ${recharge ? "+" : "−"}${item.amount.toFixed(2)} 元
            </div>
        </div>`;
    }).join("");
}

function render(data) {
    dashboardData = data;
    const summary = data.summary;

    $("room").textContent = data.room_id;
    $("balance").textContent = data.latest
        ? money(data.latest.balance)
        : "--";
    $("lastUpdate").textContent = data.latest
        ? `更新于 ${data.latest.time.slice(5,16)}`
        : "尚无查询记录";
    $("todayUsage").textContent = money(summary.today_usage);
    $("monthUsage").textContent = money(summary.month_usage);
    $("remainingDays").textContent = summary.remaining_days ?? "--";
    $("yesterdayUsage").textContent = money(summary.yesterday_usage);
    $("averageDaily").textContent = money(summary.average_daily);
    $("monthRecharge").textContent = money(summary.month_recharge);
    $("projectedMonth").textContent = money(summary.projected_month);
    $("yesterdayCompare").textContent = (
        `昨日 ${money(summary.yesterday_usage)} 元`
    );
    $("monthProjection").textContent = (
        `月末预计 ${money(summary.projected_month)} 元`
    );
    $("nextQuery").textContent = data.monitor.next_query_at
        ? `下次 ${data.monitor.next_query_at.slice(5,16)}`
        : "等待自动任务";

    const pill = $("statusPill");
    const stale = (
        data.monitor.latest_age_minutes !== null
        && data.monitor.latest_age_minutes > 90
    );
    if (data.monitor.last_error) {
        pill.className = "status-pill error";
        pill.textContent = "查询异常";
    } else if (data.monitor.querying) {
        pill.className = "status-pill warning";
        pill.textContent = "查询中";
    } else if (stale) {
        pill.className = "status-pill warning";
        pill.textContent = "数据较旧";
    } else {
        pill.className = "status-pill";
        pill.textContent = "自动监控中";
    }

    const notice = $("notice");
    if (data.monitor.last_error) {
        notice.textContent = (
            `最近一次查询失败：${data.monitor.last_error}`
        );
        notice.className = "notice show";
    } else if (data.monitor.low_balance) {
        notice.textContent = "余额已低于设定的 20 元，请及时充值。";
        notice.className = "notice show";
    } else if (data.monitor.long_gap_count > 0) {
        notice.textContent = (
            `统计范围内有 ${data.monitor.long_gap_count} 个超过 2.5 小时的`
            + "采集间隔，部分日用电为区间估算值。"
        );
        notice.className = "notice show";
    } else {
        notice.className = "notice";
    }

    renderActivities(data.activities);
    renderCharts();
}

async function loadDashboard(showError = false) {
    try {
        const response = await fetch(
            "/api/dashboard?days=30",
            { cache: "no-store" }
        );
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        render(await response.json());
    } catch (error) {
        if (showError) {
            showToast(`页面数据加载失败：${error.message}`, true);
        }
    }
}

$("refreshBtn").addEventListener("click", async () => {
    const button = $("refreshBtn");
    button.disabled = true;
    button.textContent = "查询中…";
    try {
        const response = await fetch(
            "/api/query-now",
            { method: "POST" }
        );
        const result = await response.json();
        if (!response.ok) {
            throw new Error(result.message || "查询失败");
        }
        showToast(
            `查询成功，当前余额 ${money(result.balance)} 元`
        );
        await loadDashboard(true);
    } catch (error) {
        showToast(error.message, true);
        await loadDashboard(false);
    } finally {
        button.disabled = false;
        button.textContent = "立即查询";
    }
});

document.querySelectorAll(".tab").forEach(tab => {
    tab.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach(
            item => item.classList.remove("active")
        );
        tab.classList.add("active");
        dailyRange = Number(tab.dataset.days);
        renderCharts();
    });
});

let resizeTimer;
window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(renderCharts, 120);
});

loadDashboard(true);
setInterval(() => loadDashboard(false), 60000);
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(HTML)


# =========================================================
# 7. 程序入口
# =========================================================

if __name__ == "__main__":
    init_database()
    monitor_thread = threading.Thread(
        target=hourly_worker,
        daemon=True,
    )
    monitor_thread.start()

    print("\n" + "=" * 52)
    print("寝室电费监控系统已启动")
    print("电脑访问：http://127.0.0.1:5000")
    print("同一局域网手机访问：http://电脑局域网IP:5000")
    print("系统会在启动时和每个整点查询一次")
    print("=" * 52 + "\n")

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
        threaded=True,
    )

