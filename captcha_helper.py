#!/usr/bin/env python3
"""A helper page for doing CAPTCHA sites by hand in an ordinary Chrome window.

The Playwright-controlled window could fill forms but sites refused the
submissions: reCAPTCHA and Cloudflare distrust a browser a program drives
(2026-10-02). In an ordinary Chrome nothing is filled automatically, so this
page carries everything to paste: per site an Open link, the message already
personalised, the subject, and Sent / Skip buttons that record the outcome in
the CAPTCHA log - once only, as everywhere else. Nothing is submitted by code.

    python captcha_helper.py            # serve on http://127.0.0.1:5055 and open Chrome
"""
from __future__ import annotations

import csv
import html
import json
import os
import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import captcha_assist as c
from agent.senders import pick_sender
from agent.templating import context_for, greeting_for, make_env, render_file, render_string

PORT = 5055
PROFILE = Path(c.m.OUT).resolve() / "captcha_chrome_normal_profile"
TYPE = {"pest_control_bedbugs": "Hotel", "pest_control_restaurants": "Restaurant",
        "pest_control_care_homes": "Care home", "pest_control_care_homes_followup_1": "Care home - follow-up",
        "pest_control_letting_agents": "Letting agent",
        "pest_control_letting_agents_bedbugs": "Letting agent - bed bugs"}


def build_sites() -> list[dict]:
    env = make_env(Path("."))
    cfgs: dict = {}
    out = []
    for s in c.pending_sites():
        cfg = cfgs.setdefault(s["script"], c.cfg_for(s["script"]))
        sender = pick_sender(cfg, 0, "form_senders")
        ctx = context_for(cfg, {"website": s["website"], "company_name": s["company_name"]}, sender)
        ctx["greeting_name"] = greeting_for(s["company_name"], "")
        s = dict(s, type=TYPE.get(s["script"], s["script"]),
                 message=render_file(env, cfg, cfg.path("form", "message_tiers")["full"], ctx),
                 subject=render_string(env, str(cfg.path("form", "subject_line")), ctx))
        out.append(s)
    return out


SITES: list[dict] = []
SENDER = {}

# The bookmark that fills a form. It runs as the person's own click in an
# ordinary Chrome, so nothing marks the browser as automated. Everything it
# needs is baked in: one message per pitch with a {{G}} greeting slot, and a
# host -> (pitch, greeting) map for the sites on the list.
FILL_JS = r"""(()=>{const D=__DATA__;
const h=location.hostname.replace(/^www\./,'');let s=D.sites[h];
if(!s){for(const k in D.sites){if(h.endsWith('.'+k)||k.endsWith('.'+h)){s=D.sites[k];break;}}}
if(!s){alert('Pest Stop: this site is not on the list');return;}
const msg=D.tpl[s[0]].split('{{G}}').join(s[1]),sub=D.sub[s[0]],me=D.me;
const forms=[...document.querySelectorAll('form')].filter(f=>f.querySelector('textarea'));
const root=forms.find(f=>f.offsetParent)||forms[0]||document;
const vis=e=>e.offsetParent!==null&&e.type!=='hidden';
const lab=e=>[e.name,e.id,e.placeholder,e.getAttribute('aria-label'),e.title,(e.labels&&e.labels[0]?e.labels[0].innerText:''),(e.closest('label')?e.closest('label').innerText:'')].join(' ').toLowerCase();
// the message box: a textarea labelled as a message, never an address box
const AD=/address|street/,tas=[...root.querySelectorAll('textarea')].filter(vis);
const mb=tas.find(e=>/messag|enquir|inquir|comment|question|detail|help|request|descri/.test(lab(e))&&!AD.test(lab(e)))||tas.find(e=>!AD.test(lab(e)))||null;
const put=(e,v)=>{const P=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
Object.getOwnPropertyDescriptor(P,'value').set.call(e,v);for(const t of['input','change','blur'])e.dispatchEvent(new Event(t,{bubbles:true}));};
let n=0,box=null;
for(const e of root.querySelectorAll('input,textarea')){if(!vis(e)||e.readOnly||e.disabled)continue;const t=(e.type||'').toLowerCase(),d=lab(e);
if(e.tagName==='TEXTAREA'){if(e===mb){if(e.value.trim()!==msg.trim()){put(e,msg);n++;}box=e;}else if(AD.test(d)&&(!e.value||e.value.includes(msg.split('\n')[0]))){put(e,me.address);n++;}continue;}
if(t==='checkbox'){if(/agree|consent|privacy|terms|accept|condition|confirm/.test(d)&&!/newsletter|subscri|marketing|offers|updates/.test(d)&&!e.checked){e.click();n++;}continue;}
if(['radio','submit','button','file','search','date','time','number'].includes(t))continue;
let v=null;
const intl=!!e.closest('.iti,.intl-tel-input,.react-tel-input,.react-international-phone-input-container,.PhoneInput')||/^\+/.test(e.placeholder||'')||/^\+\d{0,4}$/.test(e.value||'');
if(t==='email'||/e-?mail/.test(d))v=me.email;else if(t==='tel'||/phone|tel|mobile/.test(d))v=intl?me.phone_intl:me.phone;
else if(/first/.test(d))v=me.first;else if(/last|surname/.test(d))v=me.last;
else if(/company|organi|business/.test(d))v=me.company;else if(/subject|regarding|topic/.test(d))v=sub;
else if(/post ?code|postal|zip/.test(d))v=me.postcode;else if(AD.test(d))v=me.address;else if(/website|url/.test(d))v=me.website;
else if(/town|city/.test(d))v='London';else if(/name/.test(d))v=me.name;
// only an empty box, or a phone box holding just a dial code, is filled
if(v!==null&&(!e.value||((v===me.phone||v===me.phone_intl)&&/^\+?\d{0,4}$/.test(e.value.trim())))){put(e,v);n++;}}
if(box)box.scrollIntoView({block:'center'});
const b=document.createElement('div');b.textContent='Pest Stop: '+n+' fields filled - check, solve the CAPTCHA, then Submit';
b.style.cssText='position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#14532d;color:#fff;font:600 14px system-ui;padding:8px;text-align:center';
document.documentElement.appendChild(b);setTimeout(()=>b.remove(),6000);})();"""


def bookmarklet() -> str:
    import urllib.parse
    from agent.templating import render_file
    env = make_env(Path("."))
    tpl, sub = {}, {}
    for key in {x["script"] for x in SITES}:
        cfg = c.cfg_for(key)
        ctx = context_for(cfg, {"company_name": ""}, pick_sender(cfg, 0, "form_senders"))
        ctx["greeting_name"] = "{{G}}"
        tpl[key] = render_file(env, cfg, cfg.path("form", "message_tiers")["full"], ctx)
        sub[key] = render_string(env, str(cfg.path("form", "subject_line")), ctx)
    sites = {}
    for x in SITES:
        g = greeting_for(x["company_name"], "")
        for u in (x["contact_page"], x["website"]):
            h = c.m.host_of(u)
            if h:
                sites.setdefault(h, [x["script"], g])
    me = {"first": SENDER["first_name"], "last": SENDER["last_name"], "name": SENDER["name"],
          "email": SENDER["email"], "phone": "02034112329", "phone_intl": "+44 20 3411 2329",
          "company": SENDER["company"],
          "postcode": SENDER["postcode"], "website": SENDER["website"],
          # the registered office on peststopcontrol.com/contact
          "address": "1st Floor Operations Office, A355 Windsor End, Burtley Wood, Beaconsfield HP9 2SE"}
    js = FILL_JS.replace("__DATA__", json.dumps({"tpl": tpl, "sub": sub, "sites": sites, "me": me}))
    return "javascript:" + urllib.parse.quote(js, safe="")


def page() -> str:
    me = SENDER
    fields = [("First name", me["first_name"]), ("Last name", me["last_name"]), ("Full name", me["name"]),
              ("Email", me["email"]), ("Phone", "02034112329"), ("Company", me["company"]),
              ("Website", me["website"]), ("Postcode", me["postcode"]), ("Town", "London")]
    chips = "".join(f'<button class=chip data-copy="{html.escape(v)}">{k}: <b>{html.escape(v)}</b></button>'
                    for k, v in fields)
    rows = []
    # sites already marked Sent or Skip stay off the list, across reloads
    done = {(r["host"], r["script"]) for r in c.m.read_csv(str(c.LOG))
            if r["status"] in ("success", "skipped") and "helper page" in r.get("detail", "")}
    for i, s in enumerate(SITES):
        if (s["host"], s["script"]) in done:
            continue
        rows.append(f"""<div class=site id=s{i} data-host="{html.escape(s['host'])}" data-script="{s['script']}">
  <div class=head><span class=type>{html.escape(s['type'])}</span> <b>{html.escape(s['company_name'])}</b>
    <a class=open href="{html.escape(s['contact_page'])}" target=_blank rel=noopener>Open form &#8599;</a></div>
  <div class=btns><button data-copy="{html.escape(s['message'])}">Copy message</button>
    <button data-copy="{html.escape(s['subject'])}">Copy subject</button>
    <button class=sent onclick="mark({i},'success')">Sent &#10003;</button>
    <button class=skip onclick="mark({i},'skipped')">Skip</button></div>
</div>""")
    BOOKMARK = html.escape(bookmarklet(), quote=True)
    return f"""<!doctype html><html><head><meta charset=utf-8><title>Pest Stop CAPTCHA list</title><style>
body{{font:14px system-ui;margin:0;background:#f6f7f6;color:#1c1c1c}}
header{{position:sticky;top:0;background:#14532d;color:#fff;padding:10px 16px;z-index:2}}
header h1{{font-size:17px;margin:0 0 6px}} .chips{{display:flex;flex-wrap:wrap;gap:6px}}
.chip{{background:#fff;color:#14532d;border:0;border-radius:6px;padding:4px 8px;cursor:pointer;font-size:12px}}
main{{padding:12px 16px;max-width:900px}} .site{{background:#fff;border-radius:8px;padding:10px 12px;margin:8px 0;box-shadow:0 1px 2px #0001}}
.type{{font-size:11px;background:#e7efe9;color:#14532d;padding:2px 6px;border-radius:4px}}
.open{{margin-left:10px}} .btns{{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap}}
.btns button{{padding:5px 10px;border-radius:6px;border:1px solid #ccd;background:#fafafa;cursor:pointer}}
.sent{{background:#14532d!important;color:#fff;border-color:#14532d!important}} .done{{display:none}}
#toast{{position:fixed;bottom:16px;right:16px;background:#14532d;color:#fff;padding:8px 12px;border-radius:6px;opacity:0;transition:.2s}}
</style></head><body><header><h1>Pest Stop CAPTCHA list - <span id=left>{len(rows)}</span> left &middot; <span id=sent>{sum(1 for k in done)}</span> done</h1>
<div class=chips>{chips}</div>
<div style="margin-top:8px">One-time setup: show the bookmarks bar (&#8984;&#8679;B) and <b>drag this button onto it</b> &rarr;
<a class=chip style="background:#facc15;color:#1c1c1c;text-decoration:none;font-weight:700" href="{BOOKMARK}">Fill Pest Stop form</a>
&nbsp;Then on each form page, click it once.</div></header><main>
<p>For each site: <b>Open form</b>, click <b>Fill Pest Stop form</b> in your bookmarks bar (or paste with the copy buttons), solve the CAPTCHA, press Submit on the site, then press <b>Sent</b> here. Can't send it? Press <b>Skip</b>.</p>
{''.join(rows)}</main><div id=toast></div><script>
const toast = t => {{ const e = document.getElementById('toast'); e.textContent = t; e.style.opacity = 1; setTimeout(() => e.style.opacity = 0, 1200); }};
document.addEventListener('click', async e => {{ const b = e.target.closest('[data-copy]'); if (!b) return;
  await navigator.clipboard.writeText(b.dataset.copy); toast('Copied'); }});
setInterval(async () => {{ try {{
  const done = new Set(await (await fetch('/done')).json());
  document.querySelectorAll('.site:not(.done)').forEach(el => {{
    if (done.has(el.dataset.host + '|' + el.dataset.script)) el.classList.add('done'); }});
  document.getElementById('left').textContent = document.querySelectorAll('.site:not(.done)').length;
  document.getElementById('sent').textContent = done.size; }} catch (e) {{}} }}, 4000);
async function mark(i, status) {{
  const el = document.getElementById('s' + i);
  const r = await fetch('/mark', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{i: i, status: status}})}});
  if (r.ok) {{ el.classList.add('done'); document.getElementById('left').textContent = document.querySelectorAll('.site:not(.done)').length; toast(status === 'success' ? 'Recorded as sent' : 'Skipped'); }}
  else toast('Could not record - try again');
}}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        if self.path.startswith("/sent"):
            if "v=" in self.path:
                Path(c.m.OUT, "extension_seen.txt").write_text(f"{datetime.now():%H:%M:%S} {self.path}\n")
            sent = sorted({r["host"] for r in c.m.read_csv(str(c.LOG))
                           if r["status"] == "success" and "helper page" in r.get("detail", "")})
            body = json.dumps(sent).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self._cors()
            self.end_headers(); self.wfile.write(body); return
        if self.path == "/done":
            done = sorted(f"{r['host']}|{r['script']}" for r in c.m.read_csv(str(c.LOG))
                          if r["status"] in ("success", "skipped") and "helper page" in r.get("detail", ""))
            body = json.dumps(done).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self._cors()
            self.end_headers(); self.wfile.write(body); return
        body = page().encode()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        if self.path == "/auto_mark":
            # The extension saw the site's thank-you after the person pressed
            # Submit: record that site as sent, once.
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            h = str(data.get("host", "")).lower().removeprefix("www.") if hasattr(str, "removeprefix") else str(data.get("host", "")).lower()
            h = h[4:] if h.startswith("www.") else h
            done = {(r["host"], r["script"]) for r in c.m.read_csv(str(c.LOG))
                    if r["status"] in ("success", "skipped") and "helper page" in r.get("detail", "")}
            for s in SITES:
                hosts = {s["host"], c.m.host_of(s["contact_page"]), c.m.host_of(s["website"])}
                if (h in hosts or any(h.endswith("." + x) for x in hosts if x)) and (s["host"], s["script"]) not in done:
                    c.append_log({"timestamp": datetime.now().isoformat(timespec="seconds"), "host": s["host"],
                                  "website": s["website"], "company_name": s["company_name"],
                                  "contact_page": s["contact_page"], "script": s["script"], "status": "success",
                                  "detail": "sent by hand in ordinary Chrome - thank-you seen after the user's Submit "
                                            f"({str(data.get('evidence', ''))[:80]}) (helper page)"})
                    print(f"auto     {s['company_name']} ({s['script']})", flush=True)
                    break
            self.send_response(200); self._cors(); self.end_headers(); return
        if self.path != "/mark":
            self.send_response(404); self.end_headers(); return
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        s = SITES[int(data["i"])]
        status = "success" if data.get("status") == "success" else "skipped"
        c.append_log({"timestamp": datetime.now().isoformat(timespec="seconds"), "host": s["host"],
                      "website": s["website"], "company_name": s["company_name"],
                      "contact_page": s["contact_page"], "script": s["script"], "status": status,
                      "detail": ("sent by hand in ordinary Chrome - confirmed by the user on the helper page"
                                 if status == "success" else "skipped by the user on the helper page")})
        print(f"{status:8} {s['company_name']} ({s['script']})", flush=True)
        self.send_response(200); self.end_headers()


def main() -> int:
    global SITES, SENDER
    SITES = build_sites()
    SENDER = pick_sender(c.cfg_for("pest_control_bedbugs"), 0, "form_senders")
    print(f"{len(SITES)} sites on the helper page", flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    if "--no-browser" not in sys.argv:
        # An ordinary Chrome with a profile of its own - no automation attached,
        # so reCAPTCHA / Cloudflare treat it like any person's browser.
        subprocess.Popen(["open", "-na", "Google Chrome", "--args", f"--user-data-dir={PROFILE}",
                          "--no-first-run", "--no-default-browser-check", f"http://127.0.0.1:{PORT}/"])
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
