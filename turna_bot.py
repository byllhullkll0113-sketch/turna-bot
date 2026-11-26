import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SUIUSDT", "SOLUSDT"]
STATE_FILE = "state.json"

ST_LEN = 14
ST_15M_FAC = 3.0
ST_1H_FAC = 3.0
ST_4H_FAC = 5.5
ADX_MIN = 20.0
COOL_BARS = 16
STOP_PCT = 0.20

INTERVAL_MS = {
    "15": 15 * 60 * 1000,
    "60": 60 * 60 * 1000,
    "240": 240 * 60 * 1000,
    "D": 24 * 60 * 60 * 1000,
}

OKX_BAR = {"15": "15m", "60": "1H", "240": "4H", "D": "1Dutc"}
GATE_BAR = {"15": "15m", "60": "1h", "240": "4h", "D": "1d"}


def http_get_json(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 turna-bot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_klines(symbol: str, interval: str, limit: int = 300) -> tuple[list[dict], str]:
    errors = []
    for host in ("api.bybit.com", "api.bytick.com"):
        try:
            params = urllib.parse.urlencode(
                {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
            )
            data = http_get_json(f"https://{host}/v5/market/kline?{params}")
            if data.get("retCode") == 0 and data["result"]["list"]:
                candles = [
                    {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])}
                    for r in data["result"]["list"]
                ]
                candles.sort(key=lambda x: x["t"])
                return candles, "Bybit"
            errors.append(f"{host}: {data.get('retMsg')}")
        except Exception as e:
            errors.append(f"{host}: {e}")

    try:
        base = symbol.replace("USDT", "")
        params = urllib.parse.urlencode(
            {"instId": f"{base}-USDT-SWAP", "bar": OKX_BAR[interval], "limit": min(limit, 300)}
        )
        data = http_get_json(f"https://www.okx.com/api/v5/market/candles?{params}")
        if data.get("code") == "0" and data.get("data"):
            candles = [
                {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])}
                for r in data["data"]
            ]
            candles.sort(key=lambda x: x["t"])
            return candles, "OKX"
        errors.append(f"okx: {data.get('msg')}")
    except Exception as e:
        errors.append(f"okx: {e}")

    try:
        base = symbol.replace("USDT", "")
        params = urllib.parse.urlencode(
            {"contract": f"{base}_USDT", "interval": GATE_BAR[interval], "limit": min(limit, 300)}
        )
        rows = http_get_json(f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks?{params}")
        if isinstance(rows, list) and rows:
            candles = [
                {"t": int(r["t"]) * 1000, "o": float(r["o"]), "h": float(r["h"]), "l": float(r["l"]), "c": float(r["c"])}
                for r in rows
            ]
            candles.sort(key=lambda x: x["t"])
            return candles, "Gate"
    except Exception as e:
        errors.append(f"gate: {e}")

    raise RuntimeError(f"Veri alinamadi ({symbol} {interval}): " + " | ".join(errors))


def closed_candles(candles: list[dict], interval: str, ref_ms: int) -> list[dict]:
    step = INTERVAL_MS[interval]
    return [c for c in candles if c["t"] + step <= ref_ms]


def calc_ema(values: list[float], length: int) -> list[float]:
    alpha = 2.0 / (length + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1.0 - alpha) * out[-1])
    return out


def calc_rma(values: list[float], length: int) -> list[float]:
    out = [0.0] * len(values)
    if len(values) < length:
        return out
    out[length - 1] = sum(values[:length]) / length
    alpha = 1.0 / length
    for i in range(length, len(values)):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def calc_tr(candles: list[dict]) -> list[float]:
    tr = [candles[0]["h"] - candles[0]["l"]]
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    return tr


def calc_supertrend_dir(candles: list[dict], factor: float, length: int) -> list[int]:
    n = len(candles)
    atr = calc_rma(calc_tr(candles), length)
    upper = [0.0] * n
    lower = [0.0] * n
    st = [0.0] * n
    direction = [1] * n

    for i in range(n):
        hl2 = (candles[i]["h"] + candles[i]["l"]) / 2.0
        ub = hl2 + factor * atr[i]
        lb = hl2 - factor * atr[i]
        if i == 0:
            upper[i], lower[i], st[i], direction[i] = ub, lb, ub, 1
            continue
        prev_lb, prev_ub, prev_c = lower[i - 1], upper[i - 1], candles[i - 1]["c"]
        lower[i] = lb if (lb > prev_lb or prev_c < prev_lb) else prev_lb
        upper[i] = ub if (ub < prev_ub or prev_c > prev_ub) else prev_ub
        if st[i - 1] == prev_ub:
            direction[i] = -1 if candles[i]["c"] > upper[i] else 1
        else:
            direction[i] = 1 if candles[i]["c"] < lower[i] else -1
        st[i] = lower[i] if direction[i] == -1 else upper[i]
    return direction


def calc_adx(candles: list[dict], length: int) -> list[float]:
    n = len(candles)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = candles[i]["h"] - candles[i - 1]["h"]
        down = candles[i - 1]["l"] - candles[i]["l"]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
    trur = calc_rma(calc_tr(candles), length)
    p_rma = calc_rma(plus_dm, length)
    m_rma = calc_rma(minus_dm, length)
    dx = [0.0] * n
    for i in range(n):
        if trur[i] > 0:
            plus = 100.0 * p_rma[i] / trur[i]
            minus = 100.0 * m_rma[i] / trur[i]
            denom = plus + minus
            dx[i] = 100.0 * abs(plus - minus) / denom if denom > 0 else 0.0
    return calc_rma(dx, length)


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram secret eksik, mesaj konsola yazildi:\n" + text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def fmt_price(p: float) -> str:
    return f"{p:.4f}" if p < 10 else f"{p:.2f}"


def process_symbol(symbol: str, sym_state: dict, now_ms: int) -> tuple[list[str], str]:
    raw15, src = fetch_klines(symbol, "15", 300)
    c15 = closed_candles(raw15, "15", now_ms)
    if len(c15) < 60:
        raise RuntimeError(f"{symbol}: yetersiz 15m veri")

    bar = c15[-1]
    bar_t = bar["t"]
    ref_close_ms = bar_t + INTERVAL_MS["15"]

    c1h = closed_candles(fetch_klines(symbol, "60", 300)[0], "60", ref_close_ms)
    c4h = closed_candles(fetch_klines(symbol, "240", 300)[0], "240", ref_close_ms)
    c1d = closed_candles(fetch_klines(symbol, "D", 200)[0], "D", ref_close_ms)

    dir15 = calc_supertrend_dir(c15, ST_15M_FAC, ST_LEN)
    ema50 = calc_ema([c["c"] for c in c15], 50)
    h1_dir3 = calc_supertrend_dir(c1h, ST_1H_FAC, ST_LEN)[-1]
    h4_dir55 = calc_supertrend_dir(c4h, ST_4H_FAC, ST_LEN)[-1]
    h4_adx = calc_adx(c4h, ST_LEN)[-1]
    d1_ema50 = calc_ema([c["c"] for c in c1d], 50)[-1]

    d15_now, d15_prev = dir15[-1], dir15[-2]
    e50_now, e50_prev = ema50[-1], ema50[-2]
    close = bar["c"]

    pos = sym_state.get("position")
    status_line = (
        f"• <b>{symbol}</b> ({src}): {fmt_price(close)} | "
        f"15m:{'🟢' if d15_now < 0 else '🔴'} "
        f"1H:{'🟢' if h1_dir3 < 0 else '🔴'} "
        f"4H:{'🟢' if h4_dir55 < 0 else '🔴'} "
        f"ADX:{h4_adx:.1f} | Poz:{pos or 'YOK'}"
    )

    if sym_state.get("last_processed_bar_time") == bar_t:
        return [], status_line

    alerts = []
    exited_this_bar = False
    bar_str = datetime.fromtimestamp(ref_close_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if pos == "LONG":
        sl = sym_state["stop_price"]
        if bar["l"] <= sl:
            alerts.append(
                f"🛑 <b>TURNA | %20 SL ÇIKIŞ (LONG)</b>\n"
                f"Parite: <b>{symbol}</b> ({src})\n"
                f"Stop: {fmt_price(sl)} | Bar Düşük: {fmt_price(bar['l'])}\n"
                f"Zaman: {bar_str}"
            )
            sym_state.update(position=None, entry_price=None, stop_price=None, last_exit_bar_time=bar_t)
            exited_this_bar = True
        elif h4_dir55 > 0:
            alerts.append(
                f"🔄 <b>TURNA | 4H TREND ÇIKIŞ (LONG)</b>\n"
                f"Parite: <b>{symbol}</b> ({src})\n"
                f"Fiyat: {fmt_price(close)} | Giriş: {fmt_price(sym_state['entry_price'])}\n"
                f"Zaman: {bar_str}"
            )
            sym_state.update(position=None, entry_price=None, stop_price=None, last_exit_bar_time=bar_t)
            exited_this_bar = True

    elif pos == "SHORT":
        sl = sym_state["stop_price"]
        if bar["h"] >= sl:
            alerts.append(
                f"🛑 <b>TURNA | %20 SL ÇIKIŞ (SHORT)</b>\n"
                f"Parite: <b>{symbol}</b> ({src})\n"
                f"Stop: {fmt_price(sl)} | Bar Yüksek: {fmt_price(bar['h'])}\n"
                f"Zaman: {bar_str}"
            )
            sym_state.update(position=None, entry_price=None, stop_price=None, last_exit_bar_time=bar_t)
            exited_this_bar = True
        elif h4_dir55 < 0:
            alerts.append(
                f"🔄 <b>TURNA | 4H TREND ÇIKIŞ (SHORT)</b>\n"
                f"Parite: <b>{symbol}</b> ({src})\n"
                f"Fiyat: {fmt_price(close)} | Giriş: {fmt_price(sym_state['entry_price'])}\n"
                f"Zaman: {bar_str}"
            )
            sym_state.update(position=None, entry_price=None, stop_price=None, last_exit_bar_time=bar_t)
            exited_this_bar = True

    last_exit = sym_state.get("last_exit_bar_time")
    cool_ok = True if last_exit is None else ((bar_t - last_exit) // INTERVAL_MS["15"]) >= COOL_BARS

    if sym_state.get("position") is None and not exited_this_bar and cool_ok:
        al = (
            d15_now < 0
            and d15_prev > 0
            and h1_dir3 < 0
            and h4_dir55 < 0
            and close > d1_ema50
            and close > e50_now
            and e50_now > e50_prev
            and h4_adx > ADX_MIN
        )
        sat = (
            d15_now > 0
            and d15_prev < 0
            and h1_dir3 > 0
            and h4_dir55 > 0
            and close < d1_ema50
            and close < e50_now
            and e50_now < e50_prev
            and h4_adx > ADX_MIN
        )
        if al:
            sl = close * (1.0 - STOP_PCT)
            sym_state.update(position="LONG", entry_price=close, stop_price=sl)
            alerts.append(
                f"🟢 <b>TURNA | AL (LONG)</b>\n"
                f"Parite: <b>{symbol}</b> ({src})\n"
                f"Giriş: {fmt_price(close)} | %20 SL: {fmt_price(sl)}\n"
                f"4H ADX: {h4_adx:.1f} | 1D EMA50: {fmt_price(d1_ema50)}\n"
                f"Zaman: {bar_str}"
            )
        elif sat:
            sl = close * (1.0 + STOP_PCT)
            sym_state.update(position="SHORT", entry_price=close, stop_price=sl)
            alerts.append(
                f"🔴 <b>TURNA | SAT (SHORT)</b>\n"
                f"Parite: <b>{symbol}</b> ({src})\n"
                f"Giriş: {fmt_price(close)} | %20 SL: {fmt_price(sl)}\n"
                f"4H ADX: {h4_adx:.1f} | 1D EMA50: {fmt_price(d1_ema50)}\n"
                f"Zaman: {bar_str}"
            )

    sym_state["last_processed_bar_time"] = bar_t
    return alerts, status_line


def main() -> None:
    now_ms = int(time.time() * 1000)
    state = load_state()
    all_alerts = []
    status_lines = []
    errors = []

    for sym in SYMBOLS:
        sym_state = state.setdefault(sym, {})
        try:
            alerts, status = process_symbol(sym, sym_state, now_ms)
            all_alerts.extend(alerts)
            status_lines.append(status)
        except Exception as exc:
            errors.append(f"{sym}: {exc}")

    save_state(state)

    for msg in all_alerts:
        send_telegram(msg)

    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if is_manual:
        header = "✅ <b>TURNA (F1-v2.1) Tarayıcı Testi Başarılı</b>\n\n"
        body = "\n".join(status_lines)
        if errors:
            body += "\n\n⚠️ Hatalar:\n" + "\n".join(errors)
        send_telegram(header + body)
    elif errors:
        send_telegram("⚠️ <b>TURNA Tarayıcı Hatası</b>\n" + "\n".join(errors))

    print("\n".join(status_lines))
    if errors:
        print("ERRORS:", errors, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
