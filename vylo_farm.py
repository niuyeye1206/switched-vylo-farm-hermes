#!/usr/bin/env python3
"""Farm switched.cloud Cloud Tokens AFTER the v2.0 panel redesign (Vylo route removed).

New panel (v2.0.2) earn routes:
  * Daily streak  - no button: server credits 0.25 (->1 on day 7) just for opening /tokens.
                    One claim/day, so a single daily visit is all this script needs for it.
  * Cuty          - 1 Cloud Token / link, 2 links/day. This is the automatable route:
      POST /tokens/earn (provider=cuty) -> 302 to cuty.io/<short>
      -> cuttty.com: click "Continue" -> Cloudflare Turnstile checkbox
      -> solve via CDP coordinate clicks (same shadow-root trick as the old Vylo script)
      -> click "I am not a robot" -> "preparing" -> click the "Go ..." button
      -> the page redirects back to panel.switched.cloud and the postback credits +1.
  * Linkvertise   - 1.5/link, 10/day, but the free path hides behind hcaptcha + a
                    subscription/3-ad wall. Not automatable on a datacenter runner; SKIPPED.

A residential exit is still required: the Turnstile on cuttty.com rejects datacenter IPs
exactly like Vylo did. Keep using secret HOME_VLESS_URL / the local sing-box relay.

usage:
  SW_PROXY=socks5://127.0.0.1:10900 xvfb-run -a python3 vylo_farm.py --count 25
env:
  NOPROFIT_COOKIES   base64 or raw JSON cookie list (CI); else ~/noprofit_cookies.json
"""
import argparse
import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

from panelcookies import load_cookies, save_cookies

PANEL = os.environ.get("SW_PANEL", "https://panel.switched.cloud/tokens")
# Daily hard caps enforced by the panel; --count only bounds how many we *attempt*.
CUTY_CAP = 2
ADFOC_CAP = 4  # AdFoc.us pays 1 Cloud Token per link, 4/day

RECT_JS = r"""() => {const e=document.querySelector('.cf-turnstile,[data-sitekey]');
    if(!e) return null; const b=e.getBoundingClientRect();
    return {x:b.x,y:b.y,w:b.width,h:b.height};}"""

SKIP_JS = r"""() => {
  // AdFoc.us "skip" is not a <button>/<a> (button dump only found empty
  // anchors), so match any visible leaf element whose text is exactly "skip".
  for (const e of document.querySelectorAll('body *')) {
    const r = e.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    if (e.children.length) continue;
    const t = (e.innerText || '').trim().toLowerCase();
    if (t === 'skip') {
      e.scrollIntoView({block:'center'}); e.click();
      return true;
    }
  }
  return false;
}"""
HIT_JS = """([x,y]) => {const e=document.elementFromPoint(x,y);
    return e ? (e.tagName + '.' + String(e.className).slice(0,40)) : 'none';}"""

# Loose substring button clicker for the Cuty ad chain. Never touches paid paths.
CLICK_BTN_JS = r"""(want) => {
  const w = want.toLowerCase();
  const BAD = /premium|trial|subscribe|charge|9\.99|59\.99|skip all ads|become a/i;
  for (const e of document.querySelectorAll('button,[role=button],input[type=submit],a[href]')) {
    const r = e.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || e.disabled) continue;
    const t = (e.innerText || e.value || '').trim().replace(/\s+/g,' ').toLowerCase();
    if (!t || t.length > 40 || BAD.test(t)) continue;
    if (t.includes(w)) {
      e.scrollIntoView({block:'center'}); e.click();
      return t.slice(0, 40);
    }
  }
  return null;
}"""

ADVANCE_WORDS = ["skip", "go", "get link", "continue", "unlock", "next", "proceed",
                 "click here", "visit", "i am not a robot", "verify", "submit"]


def proxy_cfg(url):
    if not url:
        return None
    from urllib.parse import urlparse, unquote
    u = urlparse(url)
    cfg = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        cfg["username"] = unquote(u.username)
        cfg["password"] = unquote(u.password or "")
    return cfg


def body(p):
    try:
        return " ".join(p.evaluate("document.body.innerText").split())
    except Exception:
        return ""


def toklen(p):
    try:
        return p.evaluate(
            "()=>{const e=document.querySelector('[name=\"cf-turnstile-response\"]');"
            "return e?(e.value||'').length:-1}")
    except Exception:
        return -1


def click_at(cdp, x, y):
    for t, ex in (("mouseMoved", {"x": x, "y": y}),
                  ("mousePressed", {"x": x, "y": y, "button": "left", "clickCount": 1}),
                  ("mouseReleased", {"x": x, "y": y, "button": "left", "clickCount": 1})):
        cdp.send("Input.dispatchMouseEvent", dict({"type": t}, **ex))
        time.sleep(0.18)


def goto_retry(page, url, tries=4):
    """home residential exits throw transient net::ERR_CONNECTION_CLOSED - just retry."""
    for i in range(tries):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return True
        except Exception as e:
            print(f"    goto retry {i}: {type(e).__name__}", flush=True)
            time.sleep(5)
    return False


def read_state(page):
    """Parse the v2 panel: integer balance, Cuty/Linkvertise daily usage, streak."""
    t = body(page)
    bal = re.search(r"(\d+) cloud tokens", t)
    cuty = re.search(r"(\d+) of 2 today", t)
    adfoc = re.search(r"(\d+) of 4 today", t)
    lv = re.search(r"(\d+) of 10 today", t)
    claimed = "Claimed today" in t
    return {
        "bal": int(bal.group(1)) if bal else None,
        "cuty": int(cuty.group(1)) if cuty else None,
        "adfoc": int(adfoc.group(1)) if adfoc else None,
        "lv": int(lv.group(1)) if lv else None,
        "claimed": claimed,
    }


def solve_turnstile(page, cdp, log):
    """Click the checkbox; if that does not take, click the challenge iframe directly."""
    for rnd in range(8):
        r = page.evaluate(RECT_JS)
        if not (r and r["w"] > 5 and r["h"] > 5):
            log(f"  widget not rendered yet: {r}")
            time.sleep(2)
            continue
        x, y = r["x"] + 28, r["y"] + r["h"] / 2
        hit = page.evaluate(HIT_JS, [x, y])
        log(f"  round {rnd}: rect={r} click {x:.0f},{y:.0f} top={hit}")
        click_at(cdp, x, y)
        for _ in range(8):
            time.sleep(2)
            if toklen(page) > 10:
                log(f"  token len {toklen(page)}")
                return True
        if rnd == 2:
            for fr in page.frames:
                if "challenges.cloudflare.com" not in (fr.url or ""):
                    continue
                try:
                    fr.frame_element().click(force=True, timeout=8000)
                    log("  fallback: clicked challenge iframe element")
                except Exception as e:
                    log(f"  fallback click failed: {type(e).__name__}: {str(e)[:80]}")
                break
    return False


def claim_streak(page, log):
    """Click the daily-streak Claim button if it is present.
    Opening the panel does NOT claim it - the button must be clicked
    (2026-11-21: Day 4, 0.5 tokens, button still there after a run
    that only opened the page)."""
    try:
        btn = page.evaluate("""()=>{
          const els=[...document.querySelectorAll('button, a')];
          const b=els.find(x=>/claim/i.test(x.innerText||'') && /cloud token/i.test(x.innerText||''));
          if(b){b.click(); return b.innerText.trim();}
          return null;
        }""")
    except Exception as e:
        log(f"  claim: lookup failed: {type(e).__name__}")
        return False
    if not btn:
        log("  claim: no Claim button found")
        return False
    log(f"  claim: clicked '{btn}'")
    time.sleep(5)
    return True


def earn_one(ctx, panel_page, log, provider):
    """Complete ONE sponsored link for `provider` (cuty | adfoc).
    Returns True if the panel redirects back (credit postback fired)."""
    # 1. get a fresh short link from the panel (POST /tokens/earn).
    # Each provider has its OWN form, and the server-side provider
    # value is NOT the display name: Cuty -> "cuty", AdFoc.us -> "adfocus"
    # (2026-11-21: POSTing provider=adfoc returned no redirect at all).
    forms = panel_page.evaluate("""()=>{
      return [...document.querySelectorAll('form')].filter(x=>/tokens\/earn/.test(x.action)).map(f=>{
        const g=n=>f.querySelector(`input[name=${n}]`)?.value;
        const btn=(f.querySelector('button')||{}).innerText||'';
        return {action:f.action, token:g('_token'), provider:g('provider'), button:btn.trim()};
      });
    }""")
    want = {"cuty": "cuty", "adfoc": "adfocus"}.get(provider, provider)
    form = None
    for f in forms or []:
        pv = (f.get("provider") or "").lower()
        btn = (f.get("button") or "").lower()
        if pv == want or provider in btn.replace(".", ""):
            form = f
            break
    if form is None and forms:
        form = forms[0]
    if not form or not form.get("token"):
        log("  no /tokens/earn form (quota exhausted / markup changed?)")
        return False
    server_provider = form.get("provider") or want
    log(f"  earn form: provider={server_provider} button='{form.get('button')}'")
    try:
        r = ctx.request.post(form["action"],
                             form={"_token": form["token"], "provider": server_provider},
                             max_redirects=0, timeout=30000)
        cu = r.headers.get("location")
    except Exception as e:
        log(f"  earn POST failed: {type(e).__name__}: {str(e)[:100]}")
        return False
    if not cu or provider not in cu:
        log(f"  unexpected earn redirect: {cu}")
        return False
    log(f"  {provider} link: {cu}")

    # 2. walk the short-link chain in its own page (panel page stays for verification)
    q = ctx.new_page()
    try:
        if not goto_retry(q, cu):
            log(f"  {provider} page unreachable")
            return False
        time.sleep(5)
        cdp = ctx.new_cdp_session(q)
        try:
            cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        except Exception:
            pass

        # step A: first "Continue" -> captcha page
        q.evaluate("""()=>{const b=document.getElementById('submit-button')||
            [...document.querySelectorAll('button')].find(x=>/continue/i.test(x.innerText||''));
            if(b)b.click()}""")
        time.sleep(6)

        # step B: solve the Cloudflare Turnstile checkbox, IF one is
        # shown on this step. AdFoc.us does not render a Turnstile here
        # (widget never appears), so a missing widget is not a failure -
        # the advance loop below re-solves one if it shows up later.
        has_ts = q.evaluate("!!document.querySelector('.cf-turnstile,[data-sitekey]')")
        if has_ts:
            if not solve_turnstile(q, cdp, log):
                log("  turnstile: FAILED")
                try:
                    q.screenshot(path=f"/tmp/{provider}_fail_{int(time.time())}.png", full_page=True)
                except Exception:
                    pass
                return False
            log("  turnstile: solved")
        else:
            log("  turnstile: none on this page, proceeding")

        # step C: submit + advance through "preparing" until it redirects back to the panel
        for i in range(20):
            if "panel.switched.cloud" in q.url:
                log("  back on panel")
                break
            txt = body(q)
            # re-solve a fresh turnstile if the ad chain raises another one
            if q.evaluate("!!document.querySelector('.cf-turnstile,[data-sitekey]')") \
                    and toklen(q) < 10 and i % 4 == 0:
                solve_turnstile(q, cdp, log)
            # AdFoc.us return path: top-right "skip" (any element type)
            if q.evaluate(SKIP_JS):
                if i % 2 == 0:
                    log(f"  step{i}: clicked 'skip'")
                time.sleep(2)
                continue

            for w in ADVANCE_WORDS:
                hit = q.evaluate(CLICK_BTN_JS, w)
                if hit:
                    if i % 2 == 0:
                        log(f"  step{i}: clicked '{hit}'")
                    break
            if i % 4 == 0:
                try:
                    btns = q.evaluate("""()=>{
                      return [...document.querySelectorAll('button, a, input[type=submit]')].filter(e=>e.offsetParent!==null).slice(0,12).map(e=>((e.tagName)+':'+(e.innerText||e.value||'').trim().slice(0,30)+(e.disabled?'[disabled]':'')));
                    }""")
                    log(f"  step{i} buttons: {btns}")
                except Exception as e:
                    log(f"  step{i} buttons dump failed: {type(e).__name__}")
            if i % 4 == 0:
                log(f"  step{i}: {q.url.split('/')[-1][:20]} :: {txt[:60]}")
            time.sleep(4)

        back = "panel.switched.cloud" in q.url
        log(f"  {'redirected back' if back else 'NO redirect'} (last url {q.url[:70]})")
        return back
    finally:
        try:
            q.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=25,
                    help="max links to attempt (Cuty 2/day + AdFoc.us 4/day)")
    ap.add_argument("--proxy", default=os.environ.get("SW_PROXY", "socks5://127.0.0.1:10900"))
    ap.add_argument("--max-fail", type=int, default=3, help="abort after N consecutive failures")
    ap.add_argument("--headless", action="store_true",
                    help="run headless; default is headed (xvfb), which Turnstile likes")
    args = ap.parse_args()

    def log(msg):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    cookies = load_cookies()
    done = 0
    fails = 0
    st = {}
    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=args.headless, args=[
            "--no-sandbox", "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage"])
        ctx = br.new_context(viewport={"width": 1366, "height": 900}, locale="en-US",
                             proxy=proxy_cfg(args.proxy))
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        try:
            log("open panel (also claims the daily streak)")
            if not goto_retry(page, PANEL):
                log("panel unreachable")
                return 1
            time.sleep(4)
            st = read_state(page)
            if not st.get("claimed"):
                # opening the page does NOT claim the streak - the
                # Claim button must actually be clicked
                if claim_streak(page, log):
                    goto_retry(page, PANEL)
                    time.sleep(4)
                    st = read_state(page)
            log(f"state: {st}")

            # how many links are still available today, Cuty then AdFoc.us
            jobs = []
            cuty_left = CUTY_CAP - (st["cuty"] if st["cuty"] is not None else CUTY_CAP)
            adfoc_left = ADFOC_CAP - (st["adfoc"] if st["adfoc"] is not None else ADFOC_CAP)
            jobs += ["cuty"] * max(0, cuty_left)
            jobs += ["adfoc"] * max(0, adfoc_left)
            jobs = jobs[:args.count]
            log(f"queue: cuty {max(0, cuty_left)}/{CUTY_CAP}, adfoc {max(0, adfoc_left)}/{ADFOC_CAP} -> {len(jobs)} job(s)")
            if not jobs:
                log("daily caps already used - nothing to farm")

            skip_providers = set()
            for provider in jobs:
                if provider in skip_providers:
                    continue
                if fails >= args.max_fail:
                    break
                qkey = provider  # "cuty" or "adfoc"
                cap = CUTY_CAP if provider == "cuty" else ADFOC_CAP
                log(f"--- {provider} {done + 1}/{len(jobs)} (fails={fails}) ---")
                try:
                    ok = earn_one(ctx, page, log, provider)
                except Exception as e:
                    log(f"error: {type(e).__name__}: {str(e)[:120]}")
                    ok = False
                # refresh panel to read the authoritative quota/balance
                # (quota alone is not proof: the server can consume the
                # daily slot without the coin postback landing, so require
                # the balance to actually move too)
                goto_retry(page, PANEL)
                time.sleep(8)
                after = read_state(page)
                if (after[qkey] is not None and st[qkey] is not None
                        and after[qkey] > st[qkey]
                        and after["bal"] == st["bal"]):
                    # quota moved but balance didn't: the postback may be
                    # delayed (2026-11-20/21: 2nd Cuty link twice consumed
                    # the quota without crediting). Re-read at 1/2/3 min
                    # before concluding it didn't land.
                    for wait in (60, 120, 180):
                        if after["bal"] is not None and st["bal"] is not None \
                                and after["bal"] > st["bal"]:
                            break
                        log(f"  balance not moved yet, wait {wait}s and re-read")
                        time.sleep(wait)
                        goto_retry(page, PANEL)
                        time.sleep(5)
                        after = read_state(page)
                credited = (after[qkey] is not None and st[qkey] is not None
                            and after[qkey] > st[qkey]
                            and after["bal"] is not None and st["bal"] is not None
                            and after["bal"] > st["bal"])
                log(f"  {provider} quota {st[qkey]}->{after[qkey]} balance {st['bal']}->{after['bal']} "
                    f"credited={credited}")
                st = after
                if ok and credited:
                    done += 1
                    fails = 0
                else:
                    fails += 1
                    time.sleep(8)
                save_cookies(ctx.cookies())
                if after.get(qkey) is not None and after[qkey] >= cap:
                    log(f"  {provider} quota full - skip remaining {provider} jobs")
                    skip_providers.add(provider)
        finally:
            log(f"SUMMARY: farmed {done} link(s), {fails} consecutive failure(s), "
                f"final {st}")
            br.close()
    # 正常收尾一律算成功：当天配额已满、刷 0 条、刷到 N 条，都是正常日。
    # 只有「面板连不上」(上面直接 return 1) 或「连续失败到 max-fail」才算失败，
    # 否则 GA 会标红并短路跳过后面的 spend 换时长步骤，保活反而断在这里。
    return 1 if fails >= args.max_fail else 0


if __name__ == "__main__":
    sys.exit(main())
