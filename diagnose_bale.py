#!/usr/bin/env python3
"""Bale connection doctor — run this on the bot server when Bale delivery fails.

A Bale post can be refused by four different machines, and the bot log alone
cannot always tell them apart:

    the bot server  (no route out, TLS blocked)
    the relay host  (the web server in front of bridge/bale_bridge.php refusing
                     the request before PHP runs: a WAF rule, a hit resource
                     limit, a suspended account — the classic "403 Forbidden"
                     HTML page that is not Bale's)
    Bale's edge     (the API refusing the relay host's IP or throttling it)
    Bale's API      (a rejected token, a bad chat id, a malformed album)

This script asks each configured route the simplest possible question — one
``getMe`` per bot — and prints the raw evidence (HTTP status, the ``Server``
header, whether the bridge's own ``X-Bridge`` header came back, and the body),
then says which layer refused and what to do about it.

Usage (from the repo directory, next to .env):

    python diagnose_bale.py
    python diagnose_bale.py --timeout 8       # impatient run
    python diagnose_bale.py --bot 1           # only the primary bot

Exit code 0 when at least one route reached Bale, 1 otherwise — handy from a
monitoring script. The full output is meant to be pasted into the support chat.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# A doctor that refuses to start because of a missing optional import is
# useless exactly when it is needed: install a no-op dotenv shim so config.py
# still imports, and say out loud that .env was not read.
try:
    import dotenv  # noqa: F401
    DOTENV_MISSING = False
except ImportError:
    import types

    _shim = types.ModuleType("dotenv")
    _shim.load_dotenv = lambda *a, **k: None
    sys.modules["dotenv"] = _shim
    DOTENV_MISSING = True

try:
    import bale_client
    import config as cfg
except Exception as exc:  # pragma: no cover - broken environment, said out loud
    print(f"❌ config.py / bale_client.py import failed: {exc}")
    raise SystemExit(2)

# Counters for the summary. ``REACHED_API`` is the one that means "Bale
# answered" — the wrong-key probe only proves the relay host runs PHP, which is
# useful evidence but not a working route.
REACHED_API = 0
FAILURES_TOTAL = 0
PHP_RAN = False
WORKING_ROUTES = []


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def mask_token(token):
    """``123456789:AAH...`` -> ``123456789:…`` (the bot id, never the secret)."""
    if not token:
        return "تنظیم نشده"
    bot_id, _, _ = token.partition(":")
    return f"{bot_id}:…"


def mask_credentials(url):
    """Keep a proxy password out of the printed report."""
    return bale_client._mask_credentials(url)


def opener_for(proxy):
    """An opener with exactly this proxy setting and nothing inherited.

    ``ProxyHandler({})`` means "no proxy at all" — the environment variables
    (http_proxy/https_proxy) must not quietly reroute a probe that is supposed
    to test the direct route.
    """
    handler = urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {})
    return urllib.request.build_opener(handler)


def probe(url, token, method, key=None, opener=None, timeout=15):
    """POST one API call and keep the raw evidence of the answer.

    With ``key`` set the call goes the way the bridge expects it: token and
    method in headers, never in the URL, so they stay out of the relay host's
    access log.
    """
    headers = {}
    if key is not None:
        headers = {"X-Bridge-Key": key, "X-Bale-Token": token,
                   "X-Bale-Method": method}
    request = urllib.request.Request(url, data=b"", method="POST", headers=headers)
    result = {"url": url, "found": False, "status": None, "headers": {},
              "body": b"", "error": None, "elapsed": 0.0, "exc": None}
    started = time.monotonic()
    try:
        response = (opener or urllib.request.build_opener()).open(request, timeout=timeout)
        try:
            result.update(found=True, status=getattr(response, "status", 200),
                          headers=dict(response.headers), body=response.read())
        finally:
            response.close()
    except urllib.error.HTTPError as exc:
        result.update(found=True, status=exc.code, headers=dict(exc.headers or {}),
                      exc=exc, body=bale_client._read_body(exc))
    except Exception as exc:  # no HTTP response at all: DNS, TLS, connect, timeout
        result["error"] = f"{type(exc).__name__}: {exc}".replace("\n", " ")[:300]
    result["elapsed"] = time.monotonic() - started
    result["via_bridge"] = key is not None
    return result


def header(probe_result, name):
    for key, value in probe_result["headers"].items():
        if key.lower() == name.lower():
            return value
    return None


def body_text(probe_result, limit=300):
    text = (probe_result["body"] or b"").decode("utf-8", "replace").strip()
    return " ".join(text.split())[:limit]


def body_json(probe_result):
    try:
        payload = json.loads((probe_result["body"] or b"").decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def bot_message(probe_result):
    """What the bot itself would log for this answer (same code path).

    Proving the doctor and the bot agree matters: the log line the operator
    sees in production is the one printed here.
    """
    if probe_result["exc"] is None:
        return None
    info = bale_client.describe_failure(probe_result["exc"],
                                       via_bridge=probe_result["via_bridge"],
                                       body=probe_result["body"])
    return info.get("description"), info.get("origin")


def classify(probe_result, expected_forbidden=False):
    """(icon, headline, advice) for one probe's raw answer.

    ``advice`` is a key the summary turns into concrete instructions, so the
    same finding is never explained twice in different words.
    """
    global FAILURES_TOTAL, PHP_RAN, REACHED_API

    if probe_result["error"] is not None:
        FAILURES_TOTAL += 1
        return "❌", f"اتصالی برقرار نشد — {probe_result['error']}", "network"

    payload = body_json(probe_result)
    if payload is not None:
        if payload.get("ok"):
            REACHED_API += 1
            name = (payload.get("result") or {}).get("username")
            who = f"@{name}" if name else "بدون نام کاربری"
            return "✅", f"بله پاسخ داد — {who}", None
        description = str(payload.get("description", "")).strip()
        if description.lower() == "forbidden":
            # The bridge script's own auth check. Reaching it proves the host
            # DID run PHP, which is exactly what the wrong-key probe wants.
            if expected_forbidden:
                PHP_RAN = True
                return "✅", "اسکریپت پل اجرا شد (پاسخ JSON خودِ پل، نه صفحهٔ HTML)", None
            FAILURES_TOTAL += 1
            return "❌", "کلید پل پذیرفته نشد (BALE_BRIDGE_KEY ≠ BRIDGE_KEY فایل PHP)", "key"
        FAILURES_TOTAL += 1
        return "❌", f"خود بله درخواست را رد کرد: {description or payload}", "bale"

    # Non-JSON body: a page served by something that is not the Bale API.
    status = probe_result["status"]
    bridge_ran = header(probe_result, "X-Bridge") is not None
    FAILURES_TOTAL += 1
    if probe_result["via_bridge"] and not bridge_ran and status == 404:
        return "❌", ("آدرس پل پیدا نشد (HTTP 404) — فایل bridge/bale_bridge.php حذف"
                      " یا جابه‌جا شده است"), "bridge-host"
    if probe_result["via_bridge"] and not bridge_ran and status == 200:
        return "❌", ("آدرس پل به‌جای پاسخ اسکریپت، یک صفحهٔ HTML برگرداند"
                      f" — {body_text(probe_result, 80)}"), "bridge-host"
    if probe_result["via_bridge"] and not bridge_ran:
        return "❌", (f"هاست پل، درخواست را قبل از اجرای اسکریپت رد کرد "
                      f"(HTTP {status}، بدون هدر X-Bridge)"), "bridge-host"
    if probe_result["via_bridge"]:
        return "❌", (f"اسکریپت پل اجرا شد ولی بله صفحهٔ HTML برگرداند "
                      f"(HTTP {status}) — یعنی لبهٔ بله این IP را رد میکند"), "bale-edge"
    return "❌", (f"لبه/فیلتر جلوی بله پاسخ داد، نه خود API (HTTP {status})"), "bale-edge"


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

LINE = "─" * 58


def print_config():
    transport = bale_client.describe_transport()
    print(LINE)
    print("⚙️  پیکربندی فعلی (همان چیزی که ربات استفاده میکند)")
    print(f"   مسیر ارسال: {transport['fa']}")
    if transport["route"] == "bridge":
        print(f"   آدرس پل: {cfg.BALE_API_BASE}")
        print(f"   کلید پل: {'تنظیم شده' if cfg.BALE_BRIDGE_KEY else '❗️ تنظیم نشده'}"
              f" ({len(cfg.BALE_BRIDGE_KEY or '')} کاراکتر)")
        print(f"   آدرس اصلی بله (tapi.bale.ai) برای تست مستقیم بررسی میشود")
    if cfg.BALE_PROXY:
        print(f"   پروکسی: {mask_credentials(cfg.BALE_PROXY)}")
    else:
        print("   پروکسی: تنظیم نشده")
    print(f"   مهلتها: API {cfg.BALE_TIMEOUT}s / آپلود {cfg.BALE_UPLOAD_TIMEOUT}s"
          f" · همزمانی {cfg.BALE_MAX_CONCURRENT}")
    for label, token in bot_tokens():
        print(f"   {label}: {mask_token(token)}")
    if DOTENV_MISSING:
        print("   ❗️ python-dotenv نصب نیست، پس فایل .env خوانده نشد و فقط"
              " متغیرهای محیطی معتبرند.")
    print()


def bot_tokens():
    return [("ربات بله ۱ (BALE_TOKEN)", cfg.BALE_TOKEN),
            ("ربات بله ۲ (BALE_TOKEN_2)", cfg.BALE_TOKEN_2)]


def print_probe(title, probe_result, expected_forbidden=False):
    icon, headline, advice = classify(probe_result, expected_forbidden)
    if icon == "✅" and not expected_forbidden:
        WORKING_ROUTES.append(title)
    print(f"   {title}")
    print(f"     POST {probe_result['url']}")
    if probe_result["error"] is not None:
        print(f"     → بدون پاسخ HTTP · {probe_result['elapsed']:.1f}s")
    else:
        parts = [f"HTTP {probe_result['status']}"]
        for name in ("Server", "X-Bridge", "Content-Type", "CF-Ray", "X-Powered-By"):
            value = header(probe_result, name)
            if value:
                parts.append(f"{name}: {value}")
        print(f"     → {' · '.join(parts)} · {probe_result['elapsed']:.1f}s")
    snippet = body_text(probe_result, 200)
    if snippet:
        print(f"     پاسخ: {snippet}")
    message = bot_message(probe_result)
    if message:
        print(f"     پیام ربات: {(message[0] or '')[:220]}")
    print(f"     {icon} {headline}")
    print()
    return advice, icon


ADVICE = {
    "bridge-host": [
        "هاست پل، درخواست را قبل از اجرای اسکریپت رد میکند (این صفحه، صفحهٔ خود بله نیست).",
        "۱. فایل bridge/bale_bridge.php را در همان مسیر چک کنید: اگر حذف/قرنطینه شده، دوباره آپلودش کنید.",
        "۲. آدرس پل را در مرورگر (داخل ایران) باز کنید: باید JSON «forbidden» ببینید؛ اگر صفحهٔ ۴۰۳ دیدید،",
        "   هاست/WAF مسیر را بسته است — در cPanel → Metrics → Errors و ModSecurity Tools دنبال دلیلش بگردید.",
        "۳. اگر اکانت/سهمیهٔ هاست پر شده یا سایت معلق شده، محدودیت را بردارید یا پل را به هاست دیگری ببرید.",
        "۴. تا رفع مشکل: اگر پروکسی/مسیر جایگزین دارید، BALE_API_BASE را موقتاً خالی کنید (یا BALE_PROXY بگذارید).",
    ],
    "bale-edge": [
        "اسکریپت پل اجرا میشود ولی خود بله پاسخ HTML میدهد — یعنی IP این مسیر را رد/محدود کرده است.",
        "۱. این معمولاً یعنی IP هاست پل (یا پروکسی) توسط بله بلاک شده؛ IP تمیزتری لازم است (VPS/پروکسی جدید).",
        "۲. BALE_MAX_CONCURRENT را روی ۱ بگذارید تا فشار و احتمال بلاک شدن کمتر شود.",
        "۳. اگر پروکسی تنظیم شده، مقدارش را عوض کنید و همین اسکریپت را دوباره اجرا کنید.",
    ],
    "key": [
        "کلید پل با فایل PHP یکی نیست؛ BALE_BRIDGE_KEY را با BRIDGE_KEY داخل bale_bridge.php همتراز کنید.",
    ],
    "bale": [
        "خود API بله درخواست را رد کرد (پاسخ JSON). متن پاسخ بالا آمده است:",
        "توکن را در @botfather بله بررسی کنید، دسترسی ربات به کانال را چک کنید و ورودی (chat_id/آلبوم) را ببینید.",
    ],
    "network": [
        "یک یا چند مسیر حتی پاسخ HTTP هم ندادند (DNS/TLS/شبکه). از سرور ربات تست کنید:",
        "   curl -sS -o /dev/null -w '%{http_code}\\n' -m 15 <آدرس پل>",
        "   curl -sS -o /dev/null -w '%{http_code}\\n' -m 15 https://tapi.bale.ai",
        "تست مستقیم از سروری بیرون از ایران معمولاً جواب نمیدهد؛ این به‌تنهایی نشانهٔ خرابی نیست.",
        "اگر DNS یا TLS روی هر دو شکست میخورد، مشکل از شبکه/فایروال/سرور است، نه از بله.",
    ],
}


def print_summary(advices):
    """Turn the collected findings into one ordered action list."""
    print(LINE)
    print("📋 خلاصه")
    if REACHED_API and not FAILURES_TOTAL:
        print("   ✅ همهٔ مسیرهای آزمایششده به بله وصل شدند؛ ارسال باید کار کند.")
        print()
        return 0
    if REACHED_API:
        print(f"   ✅ مسیرهای سالم: {', '.join(WORKING_ROUTES)}")
        print("   ⚠️ بقیهٔ مسیرها خطا می‌دهند — تا وقتی مسیر اصلی درست شود،"
              " می‌توانید همان مسیر سالم را در .env تنظیم کنید و ربات را ری‌استارت کنید.")
    else:
        print("   ❌ هیچ مسیری به بله نرسید: هیچ پاسخی با ok=true از بله نیامد.")
        if PHP_RAN:
            print("   ℹ️ ولی هاست پل اسکریپت را اجرا میکند؛ پس مشکل از هاست نیست —"
                  " کلید پل، توکن ربات یا خود بله را بررسی کنید.")
    print()
    # The generic network note only earns space when nothing more specific was
    # found: a probe that reached a web server and got a 403 is not a network
    # problem, even though the *other* route timed out.
    order = [key for key in ("bridge-host", "bale-edge", "key", "bale") if key in advices]
    if not order and "network" in advices and not REACHED_API:
        order = ["network"]
    for key in order:
        for line in ADVICE[key]:
            print(f"   • {line}")
        print()
    print("   راهنمای کامل: bale_connectivity.md")
    print("   این خروجی را کامل بفرستید تا دقیقاً مشخص شود کدام لایه باید درست شود.")
    print()
    return 0 if REACHED_API else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bale connectivity doctor")
    parser.add_argument("--timeout", type=float, default=15,
                        help="seconds per request (default 15)")
    parser.add_argument("--bot", choices=("1", "2", "all"), default="all",
                        help="which Bale bot to test (default all)")
    args = parser.parse_args(argv)

    print()
    print(LINE)
    print("🧪 عیبیابی اتصال بله (Bale) — TisaManager")
    print(LINE)
    print_config()

    tokens = bot_tokens()
    if args.bot == "1":
        tokens = tokens[:1]
    elif args.bot == "2":
        tokens = tokens[1:]

    advices = []

    # 1) Does the relay host run PHP at all? A deliberately wrong key answers
    #    that question without needing the real one: the auth check happens
    #    inside the script and answers with JSON.
    if cfg.BALE_API_BASE:
        print(LINE)
        print("🔌 آزمون ۱ — آیا هاست پل، اسکریپت را اجرا میکند؟")
        print(f"   (کلید عمداً اشتباه فرستاده میشود؛ انتظار: JSON با description=forbidden)")
        result = probe(cfg.BALE_API_BASE, (cfg.BALE_TOKEN or "000:x"), "getMe",
                       key="this-key-is-deliberately-wrong",
                       opener=opener_for(cfg.BALE_PROXY), timeout=args.timeout)
        advice, _ = print_probe("پل ایران — دسترسی خود هاست", result, expected_forbidden=True)
        if advice:
            advices.append(advice)

    # 2) Each configured bot over every route that could carry it: through the
    #    relay if one is configured, through the proxy, and straight to the API.
    print(LINE)
    print("🔌 آزمون ۲ — getMe برای هر ربات روی هر مسیر")
    print()
    for label, token in tokens:
        if not token:
            print(f"   {label}: {mask_token(token)} — رد شد")
            print()
            continue
        if cfg.BALE_API_BASE:
            title = f"{label} — از طریق پل ایران"
            result = probe(cfg.BALE_API_BASE, token, "getMe", key=cfg.BALE_BRIDGE_KEY or "",
                           opener=opener_for(cfg.BALE_PROXY), timeout=args.timeout)
            advice, _ = print_probe(title, result)
            if advice:
                advices.append(advice)
        if cfg.BALE_PROXY:
            title = f"{label} — از طریق پروکسی"
            result = probe(f"https://tapi.bale.ai/bot{token}/getMe", token, "getMe",
                           opener=opener_for(cfg.BALE_PROXY), timeout=args.timeout)
            advice, _ = print_probe(title, result)
            if advice:
                advices.append(advice)
        title = f"{label} — مستقیم (بدون پل و بدون پروکسی)"
        result = probe(f"https://tapi.bale.ai/bot{token}/getMe", token, "getMe",
                       opener=opener_for(None), timeout=args.timeout)
        advice, _ = print_probe(title, result)
        if advice:
            advices.append(advice)

    return print_summary(advices)


if __name__ == "__main__":
    raise SystemExit(main())
