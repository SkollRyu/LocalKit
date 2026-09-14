#!/usr/bin/env python3
"""
Bookmark Check - find out which of your saved links still work.

Usage:
    python3 bookmark_checker.py

Then open the address it prints (http://127.0.0.1:8777) and drop in your
Firefox bookmark backup. Requires Python 3.8+ and nothing else.

Why this needs to run locally: a web page cannot check other people's sites
from your browser - the same-origin policy makes every response look
identical whether the page is alive or a 404. This script does the checking
from outside the browser, so the answers are real.
"""

import argparse
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import http.client
import socket
import ssl
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
    "Gecko/20100101 Firefox/128.0"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Categories the UI groups results into.
LIVE = "live"
MOVED = "moved"
BLOCKED = "blocked"
GONE = "gone"
UNREACHABLE = "unreachable"
SKIPPED = "skipped"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener(insecure):
    handlers = []
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    handlers.append(urllib.request.HTTPCookieProcessor())
    return urllib.request.build_opener(*handlers)


def _classify_exception(exc):
    """Map a transport-level exception to (category, short message)."""
    if isinstance(exc, urllib.error.HTTPError):
        return None  # handled by caller

    reason = getattr(exc, "reason", exc)

    if isinstance(reason, socket.gaierror):
        return GONE, "Domain not found"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return UNREACHABLE, "Timed out"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return UNREACHABLE, "Certificate not valid"
    if isinstance(reason, ssl.SSLError):
        return UNREACHABLE, "Secure connection failed"
    if isinstance(reason, ConnectionRefusedError):
        return GONE, "Connection refused"
    if isinstance(reason, ConnectionResetError):
        return UNREACHABLE, "Connection reset"
    if isinstance(exc, (http.client.HTTPException,)):
        return UNREACHABLE, "Invalid server response"
    if isinstance(reason, OSError) and getattr(reason, "errno", None) == -2:
        return GONE, "Domain not found"

    text = str(reason) or exc.__class__.__name__
    if "Name or service not known" in text or "nodename nor servname" in text:
        return GONE, "Domain not found"
    if "timed out" in text.lower():
        return UNREACHABLE, "Timed out"
    return UNREACHABLE, text[:120]


def _status_category(code):
    if 200 <= code < 300:
        return LIVE, "OK"
    if code in (401, 403):
        return BLOCKED, "Refused to answer a robot"
    if code == 429:
        return BLOCKED, "Rate limited"
    if code in (404, 410):
        return GONE, "Page not found" if code == 404 else "Removed by the site"
    if 400 <= code < 500:
        return BLOCKED, "Request rejected"
    if 500 <= code < 600:
        return UNREACHABLE, "Server error"
    return UNREACHABLE, "Unexpected response"


def _request(url, method, timeout, opener):
    req = urllib.request.Request(url, method=method, headers=HEADERS)
    resp = opener.open(req, timeout=timeout)
    try:
        code = resp.status
        final = resp.geturl()
    finally:
        resp.close()
    return code, final


def check_url(url, timeout=10.0, insecure=False):
    """Return a dict describing the fate of one URL."""
    result = {"url": url, "code": None, "final": None, "note": ""}
    opener = _build_opener(insecure)

    code = final = None
    for method in ("HEAD", "GET"):
        try:
            code, final = _request(url, method, timeout, opener)
        except urllib.error.HTTPError as exc:
            code, final = exc.code, (exc.url or url)
            try:
                exc.close()
            except Exception:
                pass
            # Plenty of servers dislike HEAD; confirm with a real GET.
            if method == "HEAD" and code in (400, 403, 404, 405, 406, 409, 501):
                continue
        except Exception as exc:  # noqa: BLE001 - transport failures are data here
            mapped = _classify_exception(exc)
            if method == "HEAD":
                # Retry once with GET before trusting a transport failure.
                fallback = mapped
                try:
                    code, final = _request(url, "GET", timeout, opener)
                except urllib.error.HTTPError as exc2:
                    code, final = exc2.code, (exc2.url or url)
                    try:
                        exc2.close()
                    except Exception:
                        pass
                except Exception:  # noqa: BLE001
                    result["status"], result["note"] = fallback
                    return result
            else:
                result["status"], result["note"] = mapped
                return result
        break

    result["code"] = code
    result["final"] = final
    status, note = _status_category(code)

    if status == LIVE and final and _differs(url, final):
        status, note = MOVED, "Redirected"

    result["status"] = status
    result["note"] = note
    return result


def _differs(original, final):
    """True if the redirect is meaningful rather than a trailing-slash tidy-up."""
    def norm(u):
        u = u.split("#", 1)[0]
        if u.endswith("/"):
            u = u[:-1]
        return u.replace("://www.", "://")
    return norm(original) != norm(final)


def check_many(urls, timeout=10.0, insecure=False, workers=16):
    workers = max(1, min(int(workers), 64))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda u: check_url(u, timeout, insecure), urls))


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bookmark Check</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --paper:#E7E9E4;
  --surface:#FBFBF9;
  --ink:#1A2029;
  --soft:#5C6673;
  --line:#CFD4CC;
  --line-strong:#AEB5AC;
  --accent:#1B5A57;
  --live:#2E6B4F;
  --moved:#2A5B8C;
  --blocked:#8A6216;
  --gone:#A3352B;
  --unreachable:#615C74;
  --skipped:#9AA1A8;
  --pending:#C6CBC4;
  --sans:"IBM Plex Sans",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
html,body{margin:0}
body{
  background:var(--paper);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased;
}
.wrap{max-width:1080px;margin:0 auto;padding:0 24px 96px}

.masthead{display:flex;align-items:baseline;gap:16px;padding:28px 0 22px;flex-wrap:wrap}
.masthead h1{font-size:19px;font-weight:600;letter-spacing:-.01em;margin:0}
.masthead p{margin:0;color:var(--soft);font-size:13.5px}

.stage{background:var(--surface);border:1px solid var(--line);border-radius:3px}
.drop{padding:56px 32px;text-align:center;border:1px dashed var(--line-strong);
  border-radius:3px;margin:-1px;transition:background .12s ease,border-color .12s ease}
.drop.hot{background:#F2F5EF;border-color:var(--accent)}
.drop h2{margin:0 0 6px;font-size:17px;font-weight:600}
.drop p{margin:0 0 20px;color:var(--soft);font-size:13.5px}
.drop p.fine{margin:16px 0 0;font-size:12.5px}

.btn{font:inherit;font-weight:500;font-size:14px;background:var(--accent);color:#fff;
  border:1px solid var(--accent);padding:9px 18px;border-radius:2px;cursor:pointer}
.btn:hover{background:#17504D}
.btn.ghost{background:transparent;color:var(--ink);border-color:var(--line-strong)}
.btn.ghost:hover{background:#EFF1EC}
.btn.danger{background:var(--gone);border-color:var(--gone);color:#fff}
.btn.danger:hover{background:#8C2C23}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.sm{padding:6px 12px;font-size:13px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

.tally{padding:24px 26px 22px;display:none}
.tally.on{display:block}
.tally-head{display:flex;justify-content:space-between;align-items:baseline;gap:16px;
  margin-bottom:14px;flex-wrap:wrap}
.count{font-size:30px;font-weight:600;letter-spacing:-.02em;line-height:1}
.count span{font-size:14px;font-weight:400;color:var(--soft);letter-spacing:0}
.phase{font-family:var(--mono);font-size:12.5px;color:var(--soft)}

.bar{display:flex;height:22px;background:var(--pending);border-radius:2px;overflow:hidden}
.bar i{display:block;height:100%;transition:width .3s ease}
.bar i.live{background:var(--live)}.bar i.moved{background:var(--moved)}
.bar i.blocked{background:var(--blocked)}.bar i.gone{background:var(--gone)}
.bar i.unreachable{background:var(--unreachable)}.bar i.skipped{background:var(--skipped)}

.legend{display:flex;flex-wrap:wrap;gap:6px;margin-top:14px}
.key{display:flex;align-items:center;gap:7px;background:transparent;border:1px solid var(--line);
  border-radius:2px;padding:5px 11px 5px 9px;font:inherit;font-size:13px;cursor:pointer;color:var(--ink)}
.key:hover{border-color:var(--line-strong)}
.key[aria-pressed="true"]{background:var(--ink);border-color:var(--ink);color:#fff}
.key b{font-weight:600;font-variant-numeric:tabular-nums}
.dot{width:9px;height:9px;border-radius:1px;flex:none}
.dot.live{background:var(--live)}.dot.moved{background:var(--moved)}
.dot.blocked{background:var(--blocked)}.dot.gone{background:var(--gone)}
.dot.unreachable{background:var(--unreachable)}.dot.skipped{background:var(--skipped)}

.controls{display:flex;gap:14px;align-items:center;flex-wrap:wrap;padding:16px 0;
  margin-top:22px;border-top:1px solid var(--line)}
.hidden{display:none!important}
label.opt{display:flex;align-items:center;gap:7px;font-size:13.5px;color:var(--soft)}
label.opt input[type=number]{font:inherit;font-family:var(--mono);font-size:13px;width:62px;
  padding:5px 7px;border:1px solid var(--line-strong);border-radius:2px;background:var(--surface);color:var(--ink)}
.spacer{flex:1}

.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding-bottom:12px}
.search{font:inherit;font-size:14px;flex:1;min-width:190px;padding:8px 11px;
  border:1px solid var(--line-strong);border-radius:2px;background:var(--surface);color:var(--ink)}
.search::placeholder{color:#93999E}

.selbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;
  background:var(--ink);color:#fff;border-radius:3px 3px 0 0;padding:10px 14px;font-size:13.5px}
.selbar .btn.ghost{color:#fff;border-color:#4A535F}
.selbar .btn.ghost:hover{background:#2A3441}
.selbar strong{font-weight:600;font-variant-numeric:tabular-nums}

.log{background:var(--surface);border:1px solid var(--line);border-radius:3px}
.log.joined{border-top-left-radius:0;border-top-right-radius:0}
.row{display:grid;grid-template-columns:4px 30px 74px 1fr auto;gap:0 12px;align-items:baseline;
  padding:11px 16px 11px 0;border-top:1px solid var(--line)}
.row:first-child{border-top:0}
.row.picked{background:#F1F4EE}
.rail{grid-row:1/span 2;align-self:stretch;background:var(--pending);margin:-11px 0}
.rail.live{background:var(--live)}.rail.moved{background:var(--moved)}
.rail.blocked{background:var(--blocked)}.rail.gone{background:var(--gone)}
.rail.unreachable{background:var(--unreachable)}.rail.skipped{background:var(--skipped)}
.tick{grid-row:1/span 2;align-self:center;justify-self:center;width:16px;height:16px;
  margin:0;accent-color:var(--accent);cursor:pointer}
.code{font-family:var(--mono);font-size:12.5px;color:var(--soft);font-variant-numeric:tabular-nums}
.title{font-size:14px;font-weight:500;overflow-wrap:anywhere}
.title a{color:inherit;text-decoration:none}
.title a:hover{color:var(--accent);text-decoration:underline}
.folder{font-size:12px;color:var(--soft);text-align:right;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;max-width:220px}
.link{grid-column:4/span 2;margin-top:3px;font-family:var(--mono);font-size:12.5px;overflow-wrap:anywhere}
.link a{color:var(--soft);text-decoration:none;border-bottom:1px solid var(--line-strong)}
.link a:hover{color:var(--accent);border-bottom-color:var(--accent)}
.note{color:var(--soft);font-size:12px;font-family:var(--sans)}
.note a{color:var(--moved);border-bottom-color:var(--moved)}

.more{display:block;width:100%;padding:13px;border:0;border-top:1px solid var(--line);
  background:transparent;font:inherit;font-size:13.5px;color:var(--accent);cursor:pointer}
.more:hover{background:#F2F5EF}
.empty{padding:34px 18px;text-align:center;color:var(--soft);font-size:14px}

.save{margin-top:22px;padding:18px 20px;background:var(--surface);
  border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:3px}
.save h3{margin:0 0 4px;font-size:15px;font-weight:600}
.save p{margin:0 0 14px;color:var(--soft);font-size:13.5px;max-width:64ch}
.save-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.save-note{margin:12px 0 0;font-size:12.5px;color:var(--soft);max-width:70ch}

.err{margin-top:14px;padding:12px 15px;border:1px solid var(--gone);border-left-width:3px;
  border-radius:2px;background:#FAF0EF;font-size:13.5px;display:none}
.err.on{display:block}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media (max-width:620px){
  .row{grid-template-columns:4px 28px 58px 1fr;padding-right:12px}
  .folder{display:none}
  .link{grid-column:4/span 1}
  .count{font-size:25px}
}
</style>
</head>
<body>
<div class="wrap">

  <header class="masthead">
    <h1>Bookmark check</h1>
    <p>Find out which of your saved links still go somewhere, then clear out the rest.</p>
  </header>

  <section class="stage">
    <div class="drop" id="drop">
      <h2>Drop your bookmark file here</h2>
      <p>Firefox <code>.json</code> or <code>.jsonlz4</code> backup, or an exported <code>.html</code> file.</p>
      <button class="btn" id="pick">Choose a file</button>
      <input type="file" id="file" accept=".json,.jsonlz4,.html,.htm" hidden>
      <p class="fine">Nothing leaves your machine except the requests to the bookmarked sites themselves.</p>
    </div>

    <div class="tally" id="tally">
      <div class="tally-head">
        <div class="count" id="count">0 <span>links</span></div>
        <div class="phase" id="phase">ready</div>
      </div>
      <div class="bar" id="bar">
        <i class="live"></i><i class="moved"></i><i class="blocked"></i>
        <i class="gone"></i><i class="unreachable"></i><i class="skipped"></i>
      </div>
      <div class="legend" id="legend"></div>
    </div>
  </section>

  <div class="err" id="err"></div>

  <div class="controls hidden" id="controls">
    <button class="btn" id="start">Start checking</button>
    <button class="btn ghost" id="stop" disabled>Stop</button>
    <button class="btn ghost hidden" id="retry">Retry the unreachable</button>
    <div class="spacer"></div>
    <label class="opt">At once <input type="number" id="workers" value="16" min="1" max="64"></label>
    <label class="opt">Give up after <input type="number" id="timeout" value="10" min="2" max="60"> s</label>
    <label class="opt"><input type="checkbox" id="insecure"> Accept bad certificates</label>
  </div>

  <div class="toolbar hidden" id="toolbar">
    <input class="search" id="search" placeholder="Filter by title, address or folder">
    <button class="btn ghost sm" id="selectAll">Select all shown</button>
    <button class="btn ghost sm hidden" id="fixMoved">Update moved links</button>
    <button class="btn ghost sm" id="csv">Report CSV</button>
    <button class="btn ghost sm" id="reset">Start over</button>
  </div>

  <div class="selbar hidden" id="selbar">
    <strong id="selCount">0 selected</strong>
    <div class="spacer"></div>
    <button class="btn ghost sm" id="clearSel">Clear selection</button>
    <button class="btn danger sm" id="del">Remove from list</button>
  </div>

  <div class="log hidden" id="log"></div>

  <section class="save hidden" id="save">
    <h3>Save your cleaned bookmarks</h3>
    <p id="saveSummary"></p>
    <div class="save-actions">
      <button class="btn" id="expHtml">Download bookmarks HTML</button>
      <button class="btn ghost hidden" id="expJson">Download Firefox backup</button>
      <button class="btn ghost hidden" id="undo">Undo last removal</button>
    </div>
    <p class="save-note" id="saveNote"></p>
  </section>
</div>

<script>
const STATUSES = ["live","moved","blocked","gone","unreachable","skipped"];
const LABELS = {
  live:"Working", moved:"Moved", blocked:"Blocked", gone:"Broken",
  unreachable:"No answer", skipped:"Not checked", pending:"Waiting"
};

let rows = [];
let removed = 0;
let undoStack = [];
let sel = new Set();
let filters = new Set();
let query = "";
let shown = 300;
let abort = false;
let sourceKind = "";
let nextId = 1;

const $ = id => document.getElementById(id);

/* ---------------- file intake ---------------- */

function lz4Decompress(src, destSize){
  const dest = new Uint8Array(destSize);
  let s = 0, d = 0;
  while (s < src.length){
    const token = src[s++];
    let litLen = token >> 4;
    if (litLen === 15){ let n; do { n = src[s++]; litLen += n; } while (n === 255); }
    for (let i = 0; i < litLen; i++) dest[d++] = src[s++];
    if (s >= src.length) break;
    const offset = src[s++] | (src[s++] << 8);
    let mLen = token & 0x0f;
    if (mLen === 15){ let n; do { n = src[s++]; mLen += n; } while (n === 255); }
    mLen += 4;
    let m = d - offset;
    for (let i = 0; i < mLen; i++) dest[d++] = dest[m++];
  }
  return dest.subarray(0, d);
}

async function readFile(file){
  const buf = new Uint8Array(await file.arrayBuffer());
  const magic = String.fromCharCode.apply(null, buf.subarray(0,8));
  if (magic === "mozLz40\0"){
    const size = buf[8] | (buf[9]<<8) | (buf[10]<<16) | (buf[11]<<24);
    return new TextDecoder().decode(lz4Decompress(buf.subarray(12), size));
  }
  return new TextDecoder("utf-8").decode(buf);
}

const CHECKABLE = /^https?:\/\//i;

function add(out, title, url, path, dateAdded, rootId){
  const checkable = CHECKABLE.test(url);
  out.push({
    id: nextId++,
    title: title || url,
    url: url,
    path: path.slice(),
    rootId: rootId || "",
    dateAdded: dateAdded || 0,
    status: checkable ? "pending" : "skipped",
    code: null, final: null,
    note: checkable ? "" : "Not a web address"
  });
}

function fromFirefox(node, path, out, rootId){
  if (!node || typeof node !== "object") return;
  if (Array.isArray(node.children)){
    const name = node.title || "";
    const rid = node.root === "toolbarFolder" ? "toolbar" : (node.root ? "other" : rootId);
    const next = name ? path.concat([name]) : path;
    node.children.forEach(k => fromFirefox(k, next, out, rid));
    return;
  }
  if (node.uri) add(out, node.title, node.uri, path, node.dateAdded, rootId);
}

function fromChrome(json, out){
  if (!json.roots) return false;
  const walk = (node, path, rootId) => {
    if (node.type === "url" && node.url){
      add(out, node.name, node.url, path, 0, rootId);
    } else if (Array.isArray(node.children)){
      const next = node.name ? path.concat([node.name]) : path;
      node.children.forEach(c => walk(c, next, rootId));
    }
  };
  Object.keys(json.roots).forEach(k => {
    const r = json.roots[k];
    if (r && typeof r === "object") walk(r, [], k === "bookmark_bar" ? "toolbar" : "other");
  });
  return true;
}

function fromHTML(text, out){
  const doc = new DOMParser().parseFromString(text, "text/html");
  const walk = (dl, path, rootId) => {
    for (const dt of dl.children){
      if (dt.tagName !== "DT" && dt.tagName !== "DD") continue;
      const h3 = dt.querySelector(":scope > H3");
      const a = dt.querySelector(":scope > A");
      const sub = dt.querySelector(":scope > DL");
      if (h3){
        const rid = h3.hasAttribute("PERSONAL_TOOLBAR_FOLDER") ? "toolbar" : rootId;
        if (sub) walk(sub, path.concat([h3.textContent.trim()]), rid);
      } else if (a && a.getAttribute("href")){
        const d = parseInt(a.getAttribute("ADD_DATE") || "0", 10);
        add(out, a.textContent.trim(), a.getAttribute("href"), path, d * 1000000, rootId);
      } else if (sub){
        walk(sub, path, rootId);
      }
    }
  };
  doc.querySelectorAll("body > DL").forEach(dl => walk(dl, [], ""));
  if (!out.length){
    doc.querySelectorAll("a[href]").forEach(a =>
      add(out, a.textContent.trim(), a.getAttribute("href"), [], 0, ""));
  }
}

function parse(text, filename){
  const out = [];
  const trimmed = text.trimStart();
  if (trimmed.startsWith("{") || trimmed.startsWith("[")){
    const json = JSON.parse(text);
    if (fromChrome(json, out)) sourceKind = "chrome";
    else { fromFirefox(json, [], out, ""); sourceKind = "firefox"; }
  } else if (/\.html?$/i.test(filename) || /<a\s/i.test(trimmed)){
    fromHTML(text, out);
    sourceKind = "html";
  } else {
    throw new Error("that is neither bookmark JSON nor a bookmark HTML export");
  }
  return out;
}

/* ---------------- rendering ---------------- */

function counts(){
  const c = { pending: 0 };
  STATUSES.forEach(s => c[s] = 0);
  rows.forEach(r => c[r.status] = (c[r.status] || 0) + 1);
  return c;
}

function paintTally(){
  const c = counts();
  const total = rows.length || 1;
  const checkable = rows.length - c.skipped;
  const done = checkable - c.pending;

  $("count").textContent = "";
  $("count").append(done.toLocaleString());
  const span = document.createElement("span");
  span.textContent = " of " + checkable.toLocaleString() + " checked";
  $("count").append(span);

  STATUSES.forEach(s => {
    $("bar").querySelector("i." + s).style.width = (100 * c[s] / total) + "%";
  });

  const legend = $("legend");
  legend.textContent = "";
  STATUSES.forEach(s => {
    if (!c[s]) return;
    const b = document.createElement("button");
    b.className = "key";
    b.setAttribute("aria-pressed", filters.has(s) ? "true" : "false");
    b.title = "Show only these";
    b.onclick = () => {
      filters.has(s) ? filters.delete(s) : filters.add(s);
      shown = 300; paintTally(); paintLog(); paintSelbar();
    };
    const d = document.createElement("span"); d.className = "dot " + s;
    const n = document.createElement("b"); n.textContent = c[s].toLocaleString();
    b.append(d, n, document.createTextNode(" " + LABELS[s]));
    legend.append(b);
  });
}

function visible(){
  const q = query.toLowerCase();
  return rows.filter(r => {
    if (filters.size && !filters.has(r.status)) return false;
    if (!q) return true;
    return (r.title + " " + r.url + " " + r.path.join(" ")).toLowerCase().includes(q);
  });
}

function paintSelbar(){
  const n = sel.size;
  $("selbar").classList.toggle("hidden", n === 0);
  $("log").classList.toggle("joined", n > 0);
  $("selCount").textContent = n.toLocaleString() + (n === 1 ? " link selected" : " links selected");

  const vis = visible();
  const allPicked = vis.length > 0 && vis.every(r => sel.has(r.id));
  $("selectAll").textContent = allPicked ? "Unselect all shown" : "Select all shown";
  $("selectAll").disabled = vis.length === 0;
}

function paintSave(){
  const moved = rows.filter(r => r.status === "moved" && r.final).length;
  $("fixMoved").classList.toggle("hidden", moved === 0);
  $("fixMoved").textContent = "Update " + moved.toLocaleString() + " moved link" + (moved === 1 ? "" : "s");

  $("save").classList.toggle("hidden", rows.length === 0 && removed === 0);
  $("expJson").classList.toggle("hidden", sourceKind !== "firefox");
  $("undo").classList.toggle("hidden", undoStack.length === 0);

  $("saveSummary").textContent = removed
    ? rows.length.toLocaleString() + " links kept, " + removed.toLocaleString() + " removed."
    : rows.length.toLocaleString() + " links, nothing removed yet. Tick the ones you don't want, or filter to Broken and select all shown.";

  $("saveNote").textContent = sourceKind === "firefox"
    ? "The HTML file adds to what you already have (Bookmarks \u2192 Manage bookmarks \u2192 Import and Backup \u2192 Import Bookmarks from HTML). The Firefox backup replaces your bookmarks completely when restored, which is the point after pruning \u2014 but it is a replace, so keep your original file until you are happy."
    : "Import it through Bookmarks \u2192 Manage bookmarks \u2192 Import and Backup \u2192 Import Bookmarks from HTML. It adds to your existing bookmarks rather than replacing them.";
}

function paintAll(){ paintTally(); paintLog(); paintSelbar(); paintSave(); }

function paintLog(){
  const log = $("log");
  log.textContent = "";
  const list = visible();
  if (!list.length){
    const e = document.createElement("div");
    e.className = "empty";
    e.textContent = rows.length ? "No links match that filter." : "Every link has been removed.";
    log.append(e);
    return;
  }
  const frag = document.createDocumentFragment();
  list.slice(0, shown).forEach(r => frag.append(rowEl(r)));
  log.append(frag);
  if (list.length > shown){
    const more = document.createElement("button");
    more.className = "more";
    more.textContent = "Show more (" + (list.length - shown).toLocaleString() + " still hidden)";
    more.onclick = () => { shown += 300; paintLog(); };
    log.append(more);
  }
}

function newTabLink(href, text){
  const a = document.createElement("a");
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.textContent = text;
  return a;
}

function rowEl(r){
  const el = document.createElement("div");
  el.className = "row" + (sel.has(r.id) ? " picked" : "");

  const rail = document.createElement("i");
  rail.className = "rail " + r.status;

  const tick = document.createElement("input");
  tick.type = "checkbox";
  tick.className = "tick";
  tick.checked = sel.has(r.id);
  tick.setAttribute("aria-label", "Select " + r.title);
  tick.onchange = () => {
    tick.checked ? sel.add(r.id) : sel.delete(r.id);
    el.classList.toggle("picked", tick.checked);
    paintSelbar();
  };

  const code = document.createElement("div");
  code.className = "code";
  code.textContent = r.code != null ? r.code
    : (r.status === "pending" ? "\u00B7\u00B7\u00B7" : "\u2014");

  const title = document.createElement("div");
  title.className = "title";
  title.append(newTabLink(r.url, r.title));

  const folder = document.createElement("div");
  folder.className = "folder";
  folder.textContent = r.path.join(" / ");
  folder.title = r.path.join(" / ");

  const link = document.createElement("div");
  link.className = "link";
  link.append(newTabLink(r.url, r.url));

  if (r.status === "moved" && r.final){
    const n = document.createElement("div");
    n.className = "note";
    n.append(document.createTextNode("now at "), newTabLink(r.final, r.final));
    link.append(n);
  } else if (r.note && r.status !== "live"){
    const n = document.createElement("div");
    n.className = "note";
    n.textContent = r.note;
    link.append(n);
  }

  el.append(rail, tick, code, title, folder, link);
  return el;
}

/* ---------------- scanning ---------------- */

async function scan(targets){
  abort = false;
  $("start").disabled = true; $("stop").disabled = false;
  $("retry").classList.add("hidden");

  const size = Math.max(1, Math.min(64, +$("workers").value || 16));
  const timeout = Math.max(2, Math.min(60, +$("timeout").value || 10));
  const insecure = $("insecure").checked;

  // One request per distinct address, applied to every bookmark that uses it.
  const byUrl = new Map();
  targets.forEach(r => {
    if (!byUrl.has(r.url)) byUrl.set(r.url, []);
    byUrl.get(r.url).push(r);
  });
  const unique = Array.from(byUrl.keys());

  for (let i = 0; i < unique.length && !abort; i += size){
    const batch = unique.slice(i, i + size);
    $("phase").textContent = "checking \u2026";
    let data;
    try{
      const res = await fetch("/api/check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ urls: batch, timeout, insecure, workers: size })
      });
      if (!res.ok) throw new Error("the checker returned " + res.status);
      data = await res.json();
    }catch(e){
      fail("Lost contact with the checker: " + e.message + ". Is the script still running in your terminal?");
      break;
    }
    data.results.forEach(res => {
      (byUrl.get(res.url) || []).forEach(row => {
        row.status = res.status; row.code = res.code;
        row.final = res.final; row.note = res.note;
      });
    });
    paintAll();
  }

  $("start").disabled = false; $("stop").disabled = true;
  const left = rows.filter(r => r.status === "pending").length;
  const stuck = rows.filter(r => r.status === "unreachable").length;
  $("phase").textContent = abort ? "stopped, " + left.toLocaleString() + " left" : "done";
  $("start").textContent = left ? "Check the remaining " + left.toLocaleString() : "Check everything again";
  $("retry").classList.toggle("hidden", stuck === 0);
  $("retry").textContent = "Retry the " + stuck.toLocaleString() + " with no answer";
  paintAll();
}

function fail(msg){
  const e = $("err");
  e.textContent = msg;
  e.classList.add("on");
}

/* ---------------- export ---------------- */

function esc(s){
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;")
                  .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function buildTree(list){
  const root = { name:"", toolbar:false, folders:new Map(), links:[] };
  list.forEach(r => {
    let node = root;
    r.path.forEach((seg, i) => {
      if (!node.folders.has(seg)){
        node.folders.set(seg, { name:seg, toolbar:false, folders:new Map(), links:[] });
      }
      node = node.folders.get(seg);
      if (i === 0 && r.rootId === "toolbar") node.toolbar = true;
    });
    node.links.push(r);
  });
  return root;
}

function emitFolder(node, depth, out){
  const pad = "    ".repeat(depth + 1);
  node.folders.forEach(f => {
    out.push(pad + "<DT><H3" + (f.toolbar ? " PERSONAL_TOOLBAR_FOLDER=\u0022true\u0022" : "") + ">" + esc(f.name) + "</H3>");
    out.push(pad + "<DL><p>");
    emitFolder(f, depth + 1, out);
    out.push(pad + "</DL><p>");
  });
  node.links.forEach(l => {
    const d = l.dateAdded ? " ADD_DATE=\u0022" + Math.floor(l.dateAdded / 1000000) + "\u0022" : "";
    out.push(pad + "<DT><A HREF=\u0022" + esc(l.url) + "\u0022" + d + ">" + esc(l.title) + "</A>");
  });
}

function toNetscapeHTML(){
  const out = [
    "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
    "<!-- Cleaned with Bookmark Check. -->",
    "<META HTTP-EQUIV=\u0022Content-Type\u0022 CONTENT=\u0022text/html; charset=UTF-8\u0022>",
    "<TITLE>Bookmarks</TITLE>",
    "<H1>Bookmarks</H1>",
    "<DL><p>"
  ];
  emitFolder(buildTree(rows), 0, out);
  out.push("</DL><p>", "");
  return out.join("\n");
}

const MICRO = () => Date.now() * 1000;

function container(guid, root, title, children){
  return { guid:guid, title:title, index:0, dateAdded:MICRO(), lastModified:MICRO(),
           typeCode:2, type:"text/x-moz-place-container", root:root, children:children };
}

function folderNode(name, children){
  return { title:name, index:0, dateAdded:MICRO(), lastModified:MICRO(),
           typeCode:2, type:"text/x-moz-place-container", children:children };
}

function treeToPlaces(node){
  const kids = [];
  node.folders.forEach(f => kids.push(folderNode(f.name, treeToPlaces(f))));
  node.links.forEach(l => kids.push({
    title: l.title, uri: l.url, index:0,
    dateAdded: l.dateAdded || MICRO(), lastModified: MICRO(),
    typeCode:1, type:"text/x-moz-place"
  }));
  kids.forEach((k, i) => k.index = i);
  return kids;
}

function toFirefoxJSON(){
  // Firefox restores into four fixed roots. The top path segment is the root
  // name itself, so it gets stripped before rebuilding the tree underneath.
  const strip = list => treeToPlaces(
    buildTree(list.map(r => Object.assign({}, r, { path: r.path.slice(1) }))));

  const backup = container("root________", "placesRoot", "", [
    container("menu________", "bookmarksMenuFolder", "menu",
              strip(rows.filter(r => r.rootId !== "toolbar"))),
    container("toolbar_____", "toolbarFolder", "toolbar",
              strip(rows.filter(r => r.rootId === "toolbar"))),
    container("unfiled_____", "unfiledBookmarksFolder", "unfiled", []),
    container("mobile______", "mobileFolder", "mobile", [])
  ]);
  backup.children.forEach((c, i) => c.index = i);
  return JSON.stringify(backup);
}

function download(text, name, type){
  const blob = new Blob([text], { type: type });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

function stamp(){ return new Date().toISOString().slice(0,10); }

/* ---------------- wiring ---------------- */

$("pick").onclick = () => $("file").click();
$("file").onchange = e => { if (e.target.files[0]) load(e.target.files[0]); };

const drop = $("drop");
["dragenter","dragover"].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.add("hot");
}));
["dragleave","drop"].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.remove("hot");
}));
drop.addEventListener("drop", e => { const f = e.dataTransfer.files[0]; if (f) load(f); });

async function load(file){
  $("err").classList.remove("on");
  try{
    rows = parse(await readFile(file), file.name);
  }catch(err){
    fail("Could not read that file: " + err.message);
    return;
  }
  if (!rows.length){
    fail("That file parsed cleanly but held no bookmarks.");
    return;
  }
  removed = 0; undoStack = []; sel = new Set(); filters = new Set();
  query = ""; shown = 300;
  $("search").value = "";
  drop.style.display = "none";
  $("tally").classList.add("on");
  ["controls","toolbar","log"].forEach(i => $(i).classList.remove("hidden"));
  $("phase").textContent = "ready";
  $("start").textContent = "Start checking";
  paintAll();
}

$("start").onclick = () => {
  let todo = rows.filter(r => r.status === "pending");
  if (!todo.length){
    rows.forEach(r => {
      if (r.status === "skipped") return;
      r.status = "pending"; r.code = null; r.final = null; r.note = "";
    });
    todo = rows.filter(r => r.status === "pending");
    filters = new Set();
    paintAll();
  }
  scan(todo);
};

$("stop").onclick = () => {
  abort = true; $("stop").disabled = true;
  $("phase").textContent = "stopping \u2026";
};

$("retry").onclick = () => {
  const stuck = rows.filter(r => r.status === "unreachable");
  stuck.forEach(r => { r.status = "pending"; r.code = null; });
  paintAll();
  scan(stuck);
};

$("search").oninput = e => {
  query = e.target.value.trim(); shown = 300;
  paintLog(); paintSelbar();
};

$("selectAll").onclick = () => {
  const vis = visible();
  const allPicked = vis.length > 0 && vis.every(r => sel.has(r.id));
  vis.forEach(r => allPicked ? sel.delete(r.id) : sel.add(r.id));
  paintLog(); paintSelbar();
};

$("clearSel").onclick = () => { sel = new Set(); paintLog(); paintSelbar(); };

$("del").onclick = () => {
  if (!sel.size) return;
  const gone = rows.filter(r => sel.has(r.id));
  undoStack.push(gone);
  rows = rows.filter(r => !sel.has(r.id));
  removed += gone.length;
  sel = new Set();
  shown = 300;
  // Clearing out a whole category would otherwise leave an empty screen.
  if (rows.length && !visible().length){ filters = new Set(); query = ""; $("search").value = ""; }
  paintAll();
};

$("undo").onclick = () => {
  const back = undoStack.pop();
  if (!back) return;
  rows = rows.concat(back);
  rows.sort((a,b) => a.id - b.id);
  removed -= back.length;
  paintAll();
};

$("fixMoved").onclick = () => {
  let n = 0;
  rows.forEach(r => {
    if (r.status === "moved" && r.final){
      r.url = r.final; r.status = "live"; r.note = "Address updated"; n++;
    }
  });
  if (n) paintAll();
};

$("expHtml").onclick = () =>
  download(toNetscapeHTML(), "bookmarks-cleaned-" + stamp() + ".html", "text/html");
$("expJson").onclick = () =>
  download(toFirefoxJSON(), "bookmarks-cleaned-" + stamp() + ".json", "application/json");

$("csv").onclick = () => {
  const q = v => "\u0022" + String(v == null ? "" : v).replace(/"/g, "\u0022\u0022") + "\u0022";
  const lines = [["status","code","title","url","redirected_to","note","folder"].join(",")];
  visible().forEach(r => lines.push(
    [r.status, r.code, r.title, r.url, r.final || "", r.note, r.path.join(" / ")].map(q).join(",")));
  download(lines.join("\n"), "bookmark-report-" + stamp() + ".csv", "text/csv");
};

$("reset").onclick = () => location.reload();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "BookmarkCheck"

    def _guard(self):
        """Refuse requests that did not come from this machine."""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("localhost", "127.0.0.1", "[::1]", "::1", ""):
            self._send(403, "text/plain", b"Only reachable from this computer.")
            return False
        return True

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if not self._guard():
            return
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        else:
            self._send(404, "text/plain", b"Not found")

    def do_POST(self):
        if not self._guard():
            return
        if self.path != "/api/check":
            self._send(404, "text/plain", b"Not found")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            urls = [u for u in payload.get("urls", []) if isinstance(u, str)][:200]
            timeout = float(payload.get("timeout", 10))
            insecure = bool(payload.get("insecure", False))
            workers = int(payload.get("workers", 16))
        except Exception as exc:  # noqa: BLE001
            self._send(400, "application/json",
                       json.dumps({"error": str(exc)}).encode())
            return

        results = check_many(urls, timeout=timeout, insecure=insecure, workers=workers)
        body = json.dumps({"results": results}).encode("utf-8")
        self._send(200, "application/json", body)

    def log_message(self, *args):
        pass  # keep the terminal quiet


def main():
    ap = argparse.ArgumentParser(description="Check which bookmarks still work.")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true",
                    help="do not open a browser window automatically")
    args = ap.parse_args()

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print("Could not start on port %d: %s" % (args.port, exc))
        print("Try a different one:  python3 %s --port 8890" % sys.argv[0])
        return 1

    url = "http://%s:%d" % (args.host, args.port)
    print("Bookmark Check is running at %s" % url)
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
