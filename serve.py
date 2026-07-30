#!/usr/bin/env python3
"""Local preview server. Type a GitHub username, see the animation.

Calls the same pipeline glife.py uses, so what you see here is exactly what the
workflow would publish.

  python serve.py            # http://localhost:8765
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import glife

LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")

_token = None
_lock = threading.Lock()
_contrib: dict[str, dict] = {}
_builds: dict[tuple, tuple] = {}


def token() -> str:
    global _token
    if _token:
        return _token
    _token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not _token:
        try:
            _token = subprocess.run(["gh", "auth", "token"], capture_output=True,
                                    text=True, timeout=15).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            _token = ""
    if not _token:
        raise glife.GraphQLError(
            "no token: set GITHUB_TOKEN or run `gh auth login`")
    return _token


def contributions(login: str) -> dict:
    """cached per login; the calendar only changes once a day"""
    with _lock:
        if login in _contrib:
            return _contrib[login]
    path = os.path.join(CACHE_DIR, f"{login.lower()}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    else:
        payload = glife.fetch_calendar(login, token())
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    with _lock:
        _contrib[login] = payload
    return payload


def build(login: str, q: dict):
    """-> (frames, bins, layout, total, cfg), memoised on the parameter set"""
    key = (login.lower(), q["rule"], q["gens"], q["color"], q["hold"],
           q["edges"], q["frame_ms"], q["seed_level"], q["layout"], q["fade"])
    with _lock:
        hit = _builds.get(key)
    if hit:
        return hit
    cfg = glife.make_config(q["rule"], gens=q["gens"], color=q["color"],
                            hold=q["hold"], fade=q["fade"], torus=q["torus"],
                            frame_ms=q["frame_ms"], seed_level=q["seed_level"],
                            layout=q["layout"])
    frames, bins, layout, total = glife.build_frames(contributions(login), cfg)
    out = (frames, bins, layout, total, cfg)
    with _lock:
        if len(_builds) > 64:
            _builds.clear()
        _builds[key] = out
    return out


def params(qs: dict) -> dict:
    def one(name, default):
        return qs.get(name, [default])[0]

    def num(name, default, lo, hi):
        try:
            return max(lo, min(hi, int(one(name, str(default)))))
        except ValueError:
            return default

    rule = one("rule", "B3/S23").upper()
    if not re.fullmatch(r"B[0-8]*/S[0-8]*", rule):
        raise ValueError(f"bad rule {rule!r}")
    color = one("color", "hybrid")
    if color not in glife.COLOR_MODES:
        raise ValueError(f"bad color {color!r}")
    layout = one("layout", "calendar")
    if layout not in glife.LAYOUTS:
        raise ValueError(f"bad layout {layout!r}")
    edges = one("edges", "torus")
    if edges not in ("auto", "torus", "dead"):
        raise ValueError(f"bad edges {edges!r}")
    return {
        "rule": rule,
        "color": color,
        "layout": layout,
        "gens": num("gens", 0, 0, 400),          # 0 = auto
        "hold": num("hold", 5, 0, 60),
        "fade": num("fade", 2, 1, 30),
        "seed_level": num("seed_level", 0, 0, 4),   # 0 = auto
        "frame_ms": num("frame_ms", 150, 30, 2000),
        "edges": edges,
        "torus": {"auto": None, "torus": True, "dead": False}[edges],
        "theme": "dark" if one("theme", "light") == "dark" else "light",
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if "/svg" in args[0] if args else False:
            return
        sys.stderr.write(f"  {args[0] if args else ''}\n")

    def send(self, code, ctype, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, code, msg):
        self.send(code, "application/json; charset=utf-8",
                  json.dumps({"error": msg}).encode())

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)

        if u.path == "/":
            return self.send(200, "text/html; charset=utf-8", PAGE.encode())
        if u.path == "/favicon.ico":
            return self.send(204, "image/x-icon", b"")
        if u.path not in ("/svg", "/stats"):
            return self.fail(404, "not found")

        login = (qs.get("login", [""])[0] or "").strip()
        if not LOGIN_RE.match(login):
            return self.fail(400, "username may only contain letters, digits "
                                  "and hyphens")
        try:
            q = params(qs)
            frames, bins, layout, total, cfg = build(login, q)
        except ValueError as e:
            return self.fail(400, str(e))
        except glife.GraphQLError as e:
            return self.fail(404, f"{login}: {e}")
        except urllib.error.HTTPError as e:
            return self.fail(502, f"GitHub API returned {e.code}")
        except urllib.error.URLError as e:
            return self.fail(502, f"cannot reach GitHub: {e.reason}")

        if u.path == "/svg":
            svg = glife.render_svg(frames, layout, cfg, q["theme"])
            return self.send(200, "image/svg+xml; charset=utf-8", svg.encode())

        d = glife.stats_data(frames, bins, cfg)
        d["login"] = login
        d["rule"] = q["rule"]
        d["total"] = total
        light = glife.render_svg(frames, layout, cfg, "light")
        d["bytes"] = len(light.encode())
        d["frame_ms"] = cfg.frame_ms
        d["gens"] = cfg.gens                  # resolved, if it came in as auto
        d["auto"] = [k for k in ("gens", "seed_level") if not q[k]]
        if q["edges"] == "auto":
            d["auto"].append("edges")
        d["intro"] = d["frames"] - (cfg.gens + 1)
        d["fades"] = [list(iv) for iv in glife.smooth_intervals(cfg)]
        d["svg"] = {"light": light,
                    "dark": glife.render_svg(frames, layout, cfg, "dark", ns="d")}
        return self.send(200, "application/json; charset=utf-8",
                         json.dumps(d).encode())


PAGE = r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>contribution-life preview</title>
<style>
  :root{
    --bg:#0d1117; --card:#161b22; --line:#30363d; --fg:#e6edf3; --dim:#8b949e;
    --accent:#2ea043;
  }
  *{box-sizing:border-box}
  body{margin:0;padding:20px;background:var(--bg);color:var(--fg);
       font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
  .wrap{max-width:900px;margin:0 auto}
  h1{font-size:15px;font-weight:600;margin:0 0 2px}
  .sub{color:var(--dim);font-size:12px;margin-bottom:16px}
  form{display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;
       background:var(--card);border:1px solid var(--line);border-radius:8px;
       padding:12px}
  label{display:flex;flex-direction:column;gap:4px;font-size:11px;
        color:var(--dim);letter-spacing:.03em;text-transform:uppercase}
  input,select{background:#0d1117;color:var(--fg);border:1px solid var(--line);
               border-radius:6px;padding:6px 8px;font:13px/1.2 inherit}
  input[type=text]{width:180px}
  input[type=number]{width:76px}
  .chk{flex-direction:row;align-items:center;gap:6px;text-transform:none;
       font-size:12px;padding-bottom:7px}
  button{background:var(--accent);color:#fff;border:0;border-radius:6px;
         padding:7px 16px;font:600 13px inherit;cursor:pointer}
  button:disabled{opacity:.5;cursor:default}
  .presets{margin:10px 0 0;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
  .presets span{color:var(--dim);font-size:11px;margin-right:2px}
  .presets a{color:var(--fg);background:var(--card);border:1px solid var(--line);
             border-radius:999px;padding:3px 10px;font-size:12px;cursor:pointer;
             text-decoration:none}
  .presets a:hover{border-color:var(--accent)}
  .card{margin-top:14px;border:1px solid var(--line);border-radius:8px;
        padding:12px 14px;overflow-x:auto}
  .card.d{background:var(--card)}
  .card.l{background:#fff}
  .card h2{font:600 10px/1 inherit;letter-spacing:.06em;text-transform:uppercase;
           color:var(--dim);margin:0 0 10px}
  .card.l h2{color:#57606a}
  img,svg{display:block}
  .scrub{display:none;margin-top:14px;gap:10px;align-items:center;
         background:var(--card);border:1px solid var(--line);border-radius:8px;
         padding:8px 12px}
  .scrub input[type=range]{flex:1;accent-color:var(--accent)}
  .scrub button{padding:4px 12px;font-size:12px;min-width:64px}
  .scrub span{color:var(--dim);font-size:12px;font-variant-numeric:tabular-nums;
              min-width:150px;text-align:right}
  #msg{margin-top:14px;padding:10px 14px;border-radius:8px;font-size:13px;
       border:1px solid #f85149;background:#25171c;color:#ff9492;display:none}
  #warn{margin-top:14px;padding:10px 14px;border-radius:8px;font-size:13px;
        border:1px solid #9e6a03;background:#241c14;color:#e3b341;display:none}
  #stats{margin-top:14px;display:none;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));
         gap:1px;background:var(--line);border:1px solid var(--line);border-radius:8px;
         overflow:hidden}
  .stat{background:var(--card);padding:9px 12px}
  .stat b{display:block;font:600 16px/1.3 inherit}
  .stat span{color:var(--dim);font-size:11px;letter-spacing:.03em;
             text-transform:uppercase}
  .bars{display:flex;height:6px;border-radius:3px;overflow:hidden;margin-top:5px}
  .spark{display:block;margin-top:4px}
</style>
<div class="wrap">
  <h1>contribution-life preview</h1>
  <div class="sub">Renders through the same pipeline the workflow publishes.</div>

  <form id="f">
    <label>username<input type="text" id="login" value="satomasahiro2005"
      autocomplete="off" spellcheck="false"></label>
    <label>layout
      <select id="layout">
        <option value="calendar">calendar 53&times;7</option>
        <option value="split">split 27&times;7 &times;2</option>
        <option value="square">square 19&times;19</option>
      </select></label>
    <label>rule
      <select id="rule">
        <option>B3/S23</option><option>B36/S23</option><option>B34/S34</option>
        <option>B36/S125</option><option>B35/S236</option><option>B368/S238</option>
      </select></label>
    <label>gens<input type="number" id="gens" value="0" min="0" max="400"
      title="0 = auto"></label>
    <label>color
      <select id="color">
        <option>hybrid</option><option>density</option><option>age</option>
        <option>gene</option>
      </select></label>
    <label>seed &ge;
      <select id="seed_level">
        <option value="0">auto</option><option value="1">L1</option>
        <option value="2">L2</option><option value="3">L3</option>
        <option value="4">L4</option>
      </select></label>
    <label>hold<input type="number" id="hold" value="5" min="0" max="60"></label>
    <label>fade<input type="number" id="fade" value="2" min="1" max="30"></label>
    <label>ms/frame<input type="number" id="frame_ms" value="150" min="30" max="2000"></label>
    <label>edges
      <select id="edges">
        <option value="torus">torus</option><option value="dead">dead</option>
        <option value="auto">auto</option>
      </select></label>
    <button id="go">Render</button>
  </form>

  <div class="presets"><span>try</span>
    <a data-u="torvalds">torvalds</a><a data-u="sindresorhus">sindresorhus</a>
    <a data-u="yyx990803">yyx990803</a><a data-u="gaearon">gaearon</a>
    <a data-u="simonw">simonw</a></div>

  <div id="msg"></div>
  <div id="warn"></div>
  <div id="stats"></div>

  <div class="scrub">
    <button id="play">Pause</button>
    <input type="range" id="seek" min="0" max="1" step="1" value="0">
    <span id="tick">frame 0</span>
  </div>

  <div class="card d"><h2 id="hd">dark</h2><div id="dark"></div></div>
  <div class="card l"><h2>light</h2><div id="light"></div></div>
</div>
<script>
const $ = id => document.getElementById(id);
const fields = ['login','rule','gens','color','frame_ms','seed_level','hold',
                'layout','fade','edges'];

function query(){
  const p = new URLSearchParams();
  fields.forEach(k => p.set(k, $(k).value.trim()));
  return p;
}

function pct(n){ return n + '%'; }

function showStats(d){
  const pal = ['#0e4429','#006d32','#26a641','#39d353'];
  const bars = d.palette.map((v,i) =>
    `<i style="flex:${v};background:${pal[i]}"></i>`).join('');
  const w = 120, h = 26, n = d.pops.length;
  const hi = Math.max(...d.pops) || 1;
  const pts = d.pops.map((p,i) =>
    `${(i/(n-1)*w).toFixed(1)},${(h - p/hi*h).toFixed(1)}`).join(' ');
  $('stats').innerHTML = `
    <div class="stat"><span>seed density</span><b>${pct(d.seed_pct)}</b>
      ${d.total} contributions, L${d.seed_level}+${
        d.auto.includes('seed_level') ? ' (auto)' : ''}</div>
    <div class="stat"><span>generations</span><b>${d.gens}</b>${
      d.auto.includes('gens') ? 'auto-fitted' : 'fixed'}</div>
    <div class="stat"><span>edges</span><b>${d.edges}</b>${
      d.auto.includes('edges') ? 'auto' : 'fixed'}</div>
    <div class="stat"><span>population</span><b>${d.pop_min}&ndash;${d.pop_max}</b>
      <svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
        <polyline points="${pts}" fill="none" stroke="#2ea043" stroke-width="1.5"/>
      </svg></div>
    <div class="stat"><span>churn</span><b>${pct(d.churn_pct)}</b>per frame</div>
    <div class="stat"><span>loop</span><b>${d.loop_s}s</b>${d.frames} frames</div>
    <div class="stat"><span>palette use</span>
      <b>${d.palette.map(pct).join(' ')}</b>
      <div class="bars">${bars}</div></div>
    <div class="stat"><span>svg size</span><b>${Math.round(d.bytes/1024)} KB</b>
      raw</div>`;
  $('stats').style.display = 'grid';

  if (d.pop_min_pct < 4) {
    const dense = d.seed_pct > 45;
    $('warn').textContent = `Population drops to ${pct(d.pop_min_pct)} of the ` +
      `board. ` + (dense
        ? `The seed is ${pct(d.seed_pct)} full, so Conway kills almost ` +
          `everything by overpopulation on the first step — raise "seed ≥" to ` +
          `thin it out.`
        : `Lower "gens" so the loop restarts before it thins out, or pick a ` +
          `rule that sustains itself (B34/S34, B36/S23).`);
    $('warn').style.display = 'block';
  }
}

let meta = null, playing = true;

function anims(){ return document.getAnimations(); }

function label(f){
  const parts = [`frame ${f} / ${meta.frames}`];
  if (f < meta.intro) {
    const iv = meta.fades.find(([s,e]) => f >= s && f < e);
    parts.push(iv ? `fade ${f - iv[0] + 1}/${iv[1] - iv[0]}` : 'graph held');
  } else {
    parts.push(`gen ${f - meta.intro}`);
  }
  return parts.join('  ·  ');
}

function setupScrub(d){
  meta = d;
  const s = $('seek');
  s.max = d.frames - 1;
  if (+s.value > s.max) s.value = 0;
  document.querySelector('.scrub').style.display = 'flex';
  setPlaying(playing);
  if (!playing) seekTo(+s.value);
}

function setPlaying(on){
  playing = on;
  $('play').textContent = on ? 'Pause' : 'Play';
  anims().forEach(a => on ? a.play() : a.pause());
  if (on) $('tick').textContent = 'playing';
}

// step-end holds each frame, so land mid-frame: outside a fade that reads the
// held state, inside one it catches the interpolation halfway
function seekTo(f){
  anims().forEach(a => { a.pause(); a.currentTime = (f + 0.5) * meta.frame_ms; });
  $('tick').textContent = label(f);
}

$('play').addEventListener('click', e => { e.preventDefault(); setPlaying(!playing); });
$('seek').addEventListener('input', () => {
  if (playing) setPlaying(false);
  seekTo(+$('seek').value);
});

async function render(e){
  if (e) e.preventDefault();
  const login = $('login').value.trim();
  if (!login) return;
  const p = query();
  $('msg').style.display = $('warn').style.display = 'none';
  $('go').disabled = true; $('go').textContent = 'Rendering…';
  history.replaceState(null, '', '?' + p);

  try {
    const r = await fetch('/stats?' + p);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    showStats(d);
    $('dark').innerHTML  = d.svg.dark;
    $('light').innerHTML = d.svg.light;
    $('hd').textContent = `${login} — ${$('rule').value} — ` +
      `${$('layout').value} — dark`;
    setupScrub(d);
  } catch (err) {
    $('msg').textContent = err.message;
    $('msg').style.display = 'block';
    $('stats').style.display = 'none';
  } finally {
    $('go').disabled = false; $('go').textContent = 'Render';
  }
}

$('f').addEventListener('submit', render);
document.querySelectorAll('.presets a').forEach(a =>
  a.addEventListener('click', () => { $('login').value = a.dataset.u; render(); }));

const init = new URLSearchParams(location.search);
if (init.get('login')) {
  fields.forEach(k => { if (init.get(k)) $(k).value = init.get(k); });
}
render();
</script>
"""


def main():
    ap = argparse.ArgumentParser(prog="serve")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"contribution-life preview -> http://{a.host}:{a.port}")
    print(f"contribution cache -> {CACHE_DIR}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
