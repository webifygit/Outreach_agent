#!/usr/bin/env python3
"""The AI-hiring forms the agent could not finish, opened in an ordinary Chrome with a Fill button.

ai_hiring_assist.py fills forms in a window the agent drives, and reCAPTCHA
refuses those ("Failed to validate Captcha" on federato.ai, 2026-10-30); some
other forms it cannot read at all. Here nothing is driven: the CAPTCHA tool's
ordinary-Chrome profile opens the contact pages ten at a time, and its
bookmarks bar carries "Webify Fill", a bookmarklet holding every company's
details and personalised message. One click on a form fills what is true of
the sender - name, email, phone, company, website, job title, country, the
message - and leaves every other question for the person (no first-option
guessing; see ai_hiring_assist.TRUE_ROLES). The user will not copy and paste
(2026-10-30); a click is all it asks.

A click per tab was still too much (2026-10-30), so the same logic also ships as
the "Webify Fill" extension (ai_hiring_targets/webify_fill_extension, copied to
the Desktop to load unpacked once - Chrome 156 ignores --load-extension). It
fills every listed page by itself, iframes too, and reports Submit presses and
thank-you pages to --serve, so --record-events can write what went to
output/state.json without anyone listing it. --record takes names instead.

    python ai_hiring_fill.py --batch 1                  # build button + extension data, open forms 1-10
    python ai_hiring_fill.py --serve                    # receive the extension's reports
    python ai_hiring_fill.py --record-events            # record what the reports say went
    python ai_hiring_fill.py --record "Hebbia,Everlaw"   # or: these were sent
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import pandas as pd

import ai_hiring_assist as a
from agent.state import State, norm_site
from agent.templating import context_for, make_env, render_file, render_string

PROFILE = Path("london_hotels_output/captcha_chrome_normal_profile").resolve()
QUEUE = Path("ai_hiring_targets/fill_queue.json")
START = Path("ai_hiring_targets/fill_start.html").resolve()
EXT = Path("ai_hiring_targets/webify_fill_extension")
EXT_DESKTOP = Path.home() / "Desktop" / "Webify Fill extension"
EVENTS = Path("ai_hiring_targets/fill_events.csv")
EVENT_FIELDS = ["timestamp", "type", "host", "n", "left", "page"]
PORT = 5057
BATCH = 10

FILL_JS = r"""(function(){
var D=__DATA__;
var h=location.hostname.replace(/^www\./,''), e=null;
for(var i=0;i<D.length&&!e;i++)for(var j=0;j<D[i].h.length;j++)if(h===D[i].h[j]||h.endsWith('.'+D[i].h[j])){e=D[i];break;}
function toast(t){var b=document.getElementById('__webify_fill');if(!b){b=document.createElement('div');b.id='__webify_fill';
b.style.cssText='position:fixed;top:12px;right:12px;z-index:2147483647;max-width:380px;background:#1e3a8a;color:#fff;font:600 14px/1.4 system-ui,sans-serif;padding:10px 14px;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.3)';
document.documentElement.appendChild(b);}b.textContent=t;setTimeout(function(){b.remove();},10000);}
if(!e){toast('Webify Fill: this site is not on the list.');return;}
var LV=new RegExp(e.lv,'i');
var SKIP=/size|employees|headcount|revenue|budget|volume|how many|timeline|\bdate\b|password|search|promo|coupon|captcha|zip|postal|\bstate\b|province/;
function desc(el){var s=[el.name,el.id,el.placeholder,el.getAttribute('aria-label'),el.getAttribute('autocomplete'),el.getAttribute('data-name')];
try{if(el.id){var l=el.ownerDocument.querySelector('label[for="'+CSS.escape(el.id)+'"]');if(l)s.push(l.innerText);}}catch(x){}
var p=el.closest('label');if(p)s.push(p.innerText);
var w=el.parentElement;for(var k=0;k<3&&w;k++,w=w.parentElement){var lb=w.querySelector('label,legend');if(lb){s.push(lb.innerText);break;}}
return s.filter(Boolean).join(' ').toLowerCase();}
function vis(el){var r=el.getBoundingClientRect(),cs=getComputedStyle(el);return r.width>0&&r.height>0&&cs.visibility!=='hidden'&&cs.display!=='none';}
function setv(el,v){var P=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
el.focus();Object.getOwnPropertyDescriptor(P,'value').set.call(el,v);
el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));el.blur();}
function covers(t){if(t.indexOf('+')>=0)return false;var n=(t.match(/\d[\d,]*/g)||[]).map(function(x){return parseInt(x.replace(/,/g,''),10);});
if(n.length===2)return n[0]<=11&&n[1]>=49;if(n.length===1&&/under|less than|fewer than|<|up to/i.test(t))return n[0]>=50;return false;}
function pick(el,d){var o=[].slice.call(el.options).filter(function(x){return x.value&&!/^\s*(select|choose|please|--)/i.test(x.text)&&!/\bother\b|please specify/i.test(x.text);});
function f(rx){for(var i=0;i<o.length;i++)if(rx.test(o[i].text))return o[i].value;return null;}
if(/country/.test(d))return f(/^\s*india\s*$/i);
if(/industry|company type|business type|sector|vertical/.test(d))return f(/software|information technology|\bit\b|technology|saas|computer|internet|consult/i);
if(/size|employees|headcount/.test(d)){for(var i=0;i<o.length;i++)if(covers(o[i].text))return o[i].value;return null;}
if(/hear about|find us|how did you/.test(d))return f(/search|online|internet|\bweb\b/i);
if(/job level|seniority|job title|\btitle\b|function|department/.test(d))return f(LV);
if(/inquiry|enquiry|reason|topic|interest|help|best describes|request/.test(d))return f(/partner|vendor|supplier|business development|service provider/i)||f(/general|something else/i);
return null;}
var docs=[document];[].forEach.call(document.querySelectorAll('iframe'),function(fr){try{if(fr.contentDocument&&fr.contentDocument.body)docs.push(fr.contentDocument);}catch(x){}});
var n=0,msg=false,left=0;
docs.forEach(function(doc){[].forEach.call(doc.querySelectorAll('input,textarea,select'),function(el){
if(!vis(el)||el.disabled||el.readOnly)return;
var t=(el.type||'').toLowerCase();if(/hidden|checkbox|radio|submit|button|file|password|search|image|reset/.test(t))return;
var d=desc(el),v=null;
if(el.tagName==='SELECT'){if(el.selectedIndex>0)return;v=pick(el,d);
if(v){el.value=v;el.dispatchEvent(new Event('change',{bubbles:true}));n++;}else if(el.required||/\*/.test(d))left++;return;}
if(el.value)return;
if(el.tagName==='TEXTAREA'){if(!msg){v=(el.maxLength>0&&el.maxLength<e.msg.length)?e.sh:e.msg;msg=true;}else if(el.required||/\*/.test(d))v=e.msg;}
else if(SKIP.test(d)&&!/e-?mail|phone|name|compan/.test(d))v=null;
else if(t==='email'||/e-?mail/.test(d))v=e.m;
else if(t==='tel'||/phone|mobile|\btel\b/.test(d))v=e.p;
else if(/first|fname|given/.test(d))v=e.f;
else if(/last|lname|surname|family/.test(d))v=e.l;
else if(t==='url'||/website|url|domain/.test(d))v=e.w;
else if(/compan|organi[sz]ation|business name|employer/.test(d))v=e.co;
else if(/job ?title|\btitle\b|position|designation|\brole\b/.test(d))v=e.t;
else if(/country/.test(d))v='India';
else if(/city/.test(d))v='Ahmedabad';
else if(/hear about|find us|how did you/.test(d))v='Online search';
else if(/subject/.test(d))v=e.s;
else if(/name/.test(d))v=e.n;
else if(!msg&&/message|comment|detail|help|anything|tell us|inquiry|enquiry|question/.test(d)){v=e.msg;msg=true;}
if(v){setv(el,v);n++;}else if(el.required||/\*/.test(d))left++;});});
toast('Webify Fill - '+e.c+': '+n+' fields filled'+(left?', '+left+' still need you':'')+'. Solve any CAPTCHA, then press Submit.');
})();"""


def remaining() -> list[dict]:
    """What is left on the list, best chance first: a form behind a CAPTCHA or a
    buyer question a person can answer, then the two set aside, then pages where
    the agent found no form at all."""
    why = {norm_site(r["website"]): r["detail"] for r in a.read_log()}
    sites = []
    for s in a.pending_sites():
        reason = why.get(norm_site(s["website"]), "")
        rank = 0 if "CAPTCHA" in reason else 1 if "truthfully" in reason else 3
        sites.append(dict(s, reason=reason, rank=rank))
    state = State("output/state.json")
    hand = pd.read_excel(a.SHEET, sheet_name="By hand").fillna("")
    for i, r in enumerate(hand.to_dict("records")):
        if state.contacted_site(r["website"]):
            continue
        sites.append(dict(r, company_name=r["name"], contact_page=r["contact_page_url"],
                          row_index=1000 + i, reason=r["why"], rank=2))
    return sorted(sites, key=lambda s: s["rank"])


def entry(cfg, env, s: dict) -> dict:
    sender = a.sender_for(cfg, s)
    ctx = context_for(cfg, s, sender)
    tiers = cfg.path("form", "message_tiers")
    hosts = sorted({norm_site(s["website"]), norm_site(s["contact_page"])} - {""})
    return {"c": s["company_name"], "h": hosts, "f": sender["first_name"], "l": sender["last_name"],
            "n": sender["name"], "m": sender["email"], "p": sender["phone"], "co": sender["company"],
            "w": sender["website"], "t": sender["designation"],
            "s": render_string(env, str(cfg.path("form", "subject_line")), ctx),
            "msg": render_file(env, cfg, tiers["full"], ctx),
            "sh": render_file(env, cfg, tiers["shortest"], ctx),
            "lv": a.LEVEL_ANSWER[sender["key"]].pattern}


def install_button(bookmarklet: str) -> None:
    """Put Webify Fill on the profile's bookmarks bar. Chrome must not be running on it."""
    stamp = str(int((time.time() + 11644473600) * 1_000_000))   # Chrome counts from 1601
    folder = lambda i, name, kids: {"children": kids, "date_added": stamp, "date_modified": stamp,
                                    "id": i, "name": name, "type": "folder"}
    marks = {"roots": {"bookmark_bar": folder("1", "Bookmarks bar", [
                 {"date_added": stamp, "id": "4", "name": "Webify Fill", "type": "url", "url": bookmarklet}]),
                       "other": folder("2", "Other bookmarks", []),
                       "synced": folder("3", "Mobile bookmarks", [])},
             "version": 1}
    default = PROFILE / "Default"
    default.mkdir(parents=True, exist_ok=True)
    (default / "Bookmarks").write_text(json.dumps(marks), encoding="utf-8")
    prefs_path = default / "Preferences"
    prefs = json.loads(prefs_path.read_text(encoding="utf-8")) if prefs_path.exists() else {}
    prefs.setdefault("bookmark_bar", {})["show_on_all_tabs"] = True
    prefs_path.write_text(json.dumps(prefs), encoding="utf-8")


def start_page(sites: list[dict], bookmarklet: str, batch: int) -> str:
    rows = "".join(f"<tr><td>{n}</td><td>{html.escape(s['company_name'])}</td>"
                   f"<td>{'this window' if (n - 1) // BATCH + 1 == batch else 'batch ' + str((n - 1) // BATCH + 1)}</td></tr>"
                   for n, s in enumerate(sites, 1))
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>Webify Fill - batch {batch}</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;margin:28px 44px;color:#1f2937;background:#f8fafc}}
a.btn{{display:inline-block;background:#1e3a8a;color:#fff;padding:8px 16px;border-radius:8px;text-decoration:none;font-weight:600}}
td{{border-bottom:1px solid #d1d5db;padding:5px 14px}}</style></head><body>
<h2>Webify AI hiring - batch {batch}</h2>
<ol><li>Go to each tab to the right and click <b>Webify Fill</b> in the bookmarks bar. The details and message go in.</li>
<li>Answer anything it leaves empty only if the answer is true; otherwise close the tab.</li>
<li>Solve the CAPTCHA and press <b>Submit</b>.</li></ol>
<p>No Webify Fill in the bookmarks bar? Press Cmd+Shift+B, or drag this button up there:
<a class='btn' href="{html.escape(bookmarklet, quote=True)}">Webify Fill</a></p>
<table>{rows}</table></body></html>"""


def open_batch(batch: int) -> None:
    if subprocess.run(["pgrep", "-f", str(PROFILE)], capture_output=True).returncode == 0:
        sys.exit("the Webify Fill Chrome window is still open - close it (Cmd+Q) first")
    cfg, env = a.cfg_for_script(), make_env(Path("."))
    sites = remaining() if batch == 1 or not QUEUE.exists() else json.loads(QUEUE.read_text())["sites"]
    if batch == 1 or not QUEUE.exists():
        QUEUE.write_text(json.dumps({"built": datetime.now().isoformat(timespec="seconds"),
                                     "sites": sites}, default=str, indent=1))
    data = json.dumps([entry(cfg, env, s) for s in sites], ensure_ascii=False)
    bookmarklet = "javascript:" + quote(FILL_JS.replace("__DATA__", data), safe="")
    install_button(bookmarklet)
    write_extension(data)
    START.write_text(start_page(sites, bookmarklet, batch), encoding="utf-8")
    chunk = sites[(batch - 1) * BATCH: batch * BATCH]
    if not chunk:
        sys.exit(f"there is no batch {batch} - {len(sites)} sites in all")
    subprocess.run(["open", "-na", "Google Chrome", "--args", f"--user-data-dir={PROFILE}", "--new-window",
                    START.as_uri(), *[s["contact_page"] for s in chunk]], check=True)
    print(f"batch {batch}: opened {len(chunk)} of {len(sites)}: " + ", ".join(s["company_name"] for s in chunk))


def write_extension(data: str) -> None:
    """The extension's company data, and its Desktop copy to load unpacked."""
    (EXT / "data.js").write_text(f"var WEBIFY_DATA = {data};\n", encoding="utf-8")
    if EXT_DESKTOP.exists():
        shutil.rmtree(EXT_DESKTOP)
    shutil.copytree(EXT, EXT_DESKTOP)


class EventHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/event":
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            new = not EVENTS.exists()
            with open(EVENTS, "a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=EVENT_FIELDS, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow({"timestamp": datetime.now().isoformat(timespec="seconds"), **q})
            print(datetime.now().strftime("%H:%M:%S"), q.get("type"), q.get("host"), q.get("n", ""), flush=True)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def log_message(self, *args):
        pass


def serve() -> None:
    print(f"listening on 127.0.0.1:{PORT} for Webify Fill reports", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), EventHandler).serve_forever()


def record_events() -> None:
    """Record every listed site the extension saw a Submit press on."""
    if a.run_active():
        sys.exit("a run is writing state.json right now - record again when it has finished")
    if not EVENTS.exists():
        sys.exit("no reports from the extension yet")
    with open(EVENTS, newline="", encoding="utf-8") as fh:
        events = list(csv.DictReader(fh))
    done = {norm_site(r["website"]) for r in a.read_log() if r["status"] in ("success", "uncertain")}
    cfg = a.cfg_for_script()
    for s in json.loads(QUEUE.read_text())["sites"]:
        hosts = {norm_site(s["website"]), norm_site(s["contact_page"])} - {""}
        mine = {ev["type"] for ev in events if norm_site(ev["host"]) in hosts}
        if "submit" not in mine or norm_site(s["website"]) in done:
            continue
        sender = a.sender_for(cfg, s)
        thanked = "thanks" in mine
        a.append_log({"timestamp": datetime.now().isoformat(timespec="seconds"), "website": s["website"],
                      "company_name": s["company_name"], "contact_page": s["contact_page"],
                      "status": "success" if thanked else "uncertain",
                      "detail": "sent by hand in the Webify Fill window; " +
                                ("the site thanked us" if thanked else "Submit pressed, no thank-you seen"),
                      "sender": sender["name"], "sender_email": sender["email"]})
        print(f"{s['company_name']}: {'success' if thanked else 'uncertain'}")
    a.merge()


def record(names: str) -> None:
    """Write the forms the person says they sent into output/state.json."""
    if a.run_active():
        sys.exit("a run is writing state.json right now - record again when it has finished")
    sites = {s["company_name"].lower(): s for s in json.loads(QUEUE.read_text())["sites"]}
    cfg = a.cfg_for_script()
    for name in [x.strip() for x in names.split(",") if x.strip()]:
        s = sites.get(name.lower())
        if not s:
            print(f"not on the list: {name}")
            continue
        sender = a.sender_for(cfg, s)
        a.append_log({"timestamp": datetime.now().isoformat(timespec="seconds"), "website": s["website"],
                      "company_name": s["company_name"], "contact_page": s["contact_page"],
                      "status": "uncertain", "detail": "sent by hand from the Webify Fill window (person says so)",
                      "sender": sender["name"], "sender_email": sender["email"]})
    a.merge()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int)
    ap.add_argument("--record")
    ap.add_argument("--record-events", action="store_true")
    ap.add_argument("--serve", action="store_true")
    args = ap.parse_args()
    if args.serve:
        serve()
    elif args.record_events:
        record_events()
    elif args.record:
        record(args.record)
    elif args.batch:
        open_batch(args.batch)
    else:
        ap.error("give --batch N, --serve, --record-events or --record NAMES")
    return 0


if __name__ == "__main__":
    sys.exit(main())
