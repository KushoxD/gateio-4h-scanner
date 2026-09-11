"""The HTML dashboard served by the scanner's own HTTP server.

Self-contained: no CDN, no build step, no external fonts. The page fetches
/api/state and re-renders, so the worker only ever serves static markup plus
one JSON endpoint.
"""

from __future__ import annotations

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gate.io 4H Scanner</title>
<style>
  :root {
    --bg: #f6f7f9;      --panel: #ffffff;   --panel-2: #fbfcfd;
    --ink: #11161d;     --muted: #5b6675;   --faint: #8b95a4;
    --line: #e3e7ec;    --accent: #1f6feb; --up: #0f9960; --warn: #b7791f;
    --shadow: 0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.04);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0c0f14;    --panel: #141922;   --panel-2: #10151d;
      --ink: #e9eef6;   --muted: #97a3b4;   --faint: #6b7789;
      --line: #222a36;  --accent: #5b9dff; --up: #35d08a; --warn: #e0b155;
      --shadow: none;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.5 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI",
          Roboto, Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1120px; margin: 0 auto; padding: 28px 16px 64px; }
  header { display: flex; flex-wrap: wrap; gap: 12px; align-items: baseline;
           justify-content: space-between; margin-bottom: 22px; }
  h1 { font-size: 20px; margin: 0; letter-spacing: -.01em; }
  h1 span { color: var(--faint); font-weight: 400; }
  .sub { color: var(--muted); font-size: 13px; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
         background: var(--up); margin-right: 6px; vertical-align: 1px; }
  .dot.warn { background: var(--warn); }

  .cards { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
           margin-bottom: 22px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
          padding: 13px 15px; box-shadow: var(--shadow); }
  .card .k { color: var(--muted); font-size: 12px; text-transform: uppercase;
             letter-spacing: .04em; }
  .card .v { font-size: 21px; font-weight: 600; margin-top: 3px;
             font-variant-numeric: tabular-nums; }
  .card .v.sm { font-size: 15px; font-weight: 500; }

  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .05em;
       color: var(--muted); margin: 26px 0 10px; font-weight: 600; }

  .funnel { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .step { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
          padding: 7px 11px; font-size: 13px; box-shadow: var(--shadow); }
  .step b { font-variant-numeric: tabular-nums; }
  .step i { color: var(--faint); font-style: normal; font-size: 11px;
            display: block; text-transform: uppercase; letter-spacing: .04em; }
  .arrow { color: var(--faint); font-size: 12px; }

  .alerts { display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(290px, 1fr)); }
  .alert { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
           padding: 15px; box-shadow: var(--shadow); position: relative; }
  .alert .top { display: flex; align-items: baseline; justify-content: space-between;
                gap: 10px; margin-bottom: 10px; }
  .pair { font-size: 17px; font-weight: 650; letter-spacing: -.01em; }
  .pair a { color: inherit; text-decoration: none; }
  .pair a:hover { color: var(--accent); text-decoration: underline; }
  .tag { font-size: 11px; padding: 2px 7px; border-radius: 20px; border: 1px solid var(--line);
         color: var(--muted); white-space: nowrap; }
  .tag.sent { color: var(--up); border-color: color-mix(in srgb, var(--up) 35%, var(--line)); }
  .rows { display: grid; grid-template-columns: auto 1fr; gap: 4px 14px; font-size: 13px; }
  .rows dt { color: var(--muted); }
  .rows dd { margin: 0; text-align: right; font-variant-numeric: tabular-nums; }
  .rows dd.up { color: var(--up); }
  .barts { margin-top: 11px; padding-top: 10px; border-top: 1px solid var(--line);
           color: var(--faint); font-size: 12px; display: flex; justify-content: space-between; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .scroll { overflow-x: auto; background: var(--panel); border: 1px solid var(--line);
            border-radius: 10px; box-shadow: var(--shadow); }
  th, td { padding: 9px 13px; text-align: right; white-space: nowrap;
           border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums; }
  th { color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase;
       letter-spacing: .04em; background: var(--panel-2); }
  th:first-child, td:first-child { text-align: left; }
  tbody tr:last-child td { border-bottom: none; }

  .empty { background: var(--panel); border: 1px dashed var(--line); border-radius: 10px;
           padding: 34px 20px; text-align: center; color: var(--muted); }
  .empty b { color: var(--ink); display: block; margin-bottom: 5px; font-size: 15px; }
  footer { margin-top: 34px; color: var(--faint); font-size: 12px; text-align: center; }
  .banner { background: color-mix(in srgb, var(--warn) 12%, var(--panel));
            border: 1px solid color-mix(in srgb, var(--warn) 40%, var(--line));
            color: var(--ink); border-radius: 10px; padding: 11px 14px; font-size: 13px;
            margin-bottom: 18px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Gate.io <span>/</span> 4H MACD Scanner</h1>
      <div class="sub" id="status">loading…</div>
    </div>
    <div class="sub" id="clock"></div>
  </header>

  <div id="banner"></div>
  <div class="cards" id="cards"></div>

  <h2>Alerts</h2>
  <div id="alerts"></div>

  <h2>Last scan funnel</h2>
  <div class="funnel" id="funnel"></div>

  <h2>Scan history</h2>
  <div class="scroll"><table>
    <thead><tr>
      <th>Bar close (UTC)</th><th>Scanned</th><th>Hits</th><th>Sent</th>
      <th>Dupes</th><th>Errors</th><th>Took</th>
    </tr></thead>
    <tbody id="scans"></tbody>
  </table></div>

  <footer id="foot"></footer>
</div>

<script>
const fmtUsd = v => {
  if (!v || v <= 0) return "n/a";
  if (v >= 1e12) return "$" + (v / 1e12).toFixed(2) + "T";
  if (v >= 1e9)  return "$" + (v / 1e9).toFixed(2) + "B";
  if (v >= 1e6)  return "$" + (v / 1e6).toFixed(2) + "M";
  if (v >= 1e3)  return "$" + (v / 1e3).toFixed(2) + "K";
  return "$" + v.toFixed(0);
};
const fmtPrice = v => {
  const m = Math.abs(v);
  if (m === 0) return "0";
  if (m >= 1000) return v.toLocaleString(undefined, {maximumFractionDigits: 2});
  if (m >= 1) return v.toFixed(4);
  if (m >= 0.01) return v.toFixed(6);
  return v.toPrecision(4);
};
const fmtInd = v => {
  const m = Math.abs(v);
  if (m >= 1) return v.toFixed(4);
  if (m >= 1e-4) return v.toFixed(8);
  return v.toExponential(3);
};
const utc = ts => ts ? new Date(ts * 1000).toISOString().slice(0, 16).replace("T", " ") : "—";
const ago = ts => {
  if (!ts) return "never";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
};
const until = ts => {
  if (!ts) return "—";
  const s = Math.max(0, Math.floor(ts - Date.now() / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m ${s % 60}s`;
};
const esc = s => String(s).replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

let state = null;

function render() {
  if (!state) return;
  const s = state;

  document.getElementById("status").innerHTML =
    `<span class="dot${s.last_error ? " warn" : ""}"></span>` +
    `${s.scans_completed} scan${s.scans_completed === 1 ? "" : "s"} · ` +
    `last ${ago(s.last_scan_at)} · next in ${until(s.next_scan_at)}`;
  document.getElementById("clock").textContent = utc(Date.now() / 1000) + " UTC";

  const warn = [];
  if (!s.telegram_enabled)
    warn.push("Telegram is not configured — hits are recorded here but no messages are sent. " +
              "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Railway.");
  if (s.last_error) warn.push("Last scan error: " + esc(s.last_error));
  document.getElementById("banner").innerHTML =
    warn.map(w => `<div class="banner">${w}</div>`).join("");

  const last = s.last_scan || {};
  document.getElementById("cards").innerHTML = [
    ["Alerts total", s.totals.alerted],
    ["Hits recorded", s.totals.hits],
    ["Pairs scanned", last.scanned ?? "—"],
    ["Last bar close", `<span class="v sm">${utc(s.last_bar_close_ts)} UTC</span>`, true],
    ["Next scan", `<span class="v sm">${until(s.next_scan_at)}</span>`, true],
  ].map(([k, v, raw]) =>
    `<div class="card"><div class="k">${k}</div>` +
    (raw ? v : `<div class="v">${v}</div>`) + `</div>`).join("");

  const hits = s.hits || [];
  document.getElementById("alerts").innerHTML = hits.length ? (
    `<div class="alerts">` + hits.map(h => {
      const closeTs = h.bar_ts + h.interval_seconds;
      return `<div class="alert">
        <div class="top">
          <div class="pair"><a href="https://www.gate.io/trade/${esc(h.pair)}"
             target="_blank" rel="noopener">${esc(h.pair)}</a></div>
          <span class="tag${h.alerted && s.telegram_enabled ? " sent" : ""}"
            >${h.alerted ? (s.telegram_enabled ? "sent" : "logged only") : "not sent"}</span>
        </div>
        <dl class="rows">
          <dt>Close</dt><dd>${fmtPrice(h.close)}</dd>
          <dt>EMA${h.ema_len}</dt><dd>${fmtPrice(h.ema)}</dd>
          <dt>MACD</dt><dd class="up">${fmtInd(h.macd)}</dd>
          <dt>Signal</dt><dd>${fmtInd(h.signal)}</dd>
          <dt>Market cap</dt><dd>${fmtUsd(h.market_cap)}</dd>
          <dt>24h vol</dt><dd>${fmtUsd(h.quote_volume_24h)}</dd>
        </dl>
        <div class="barts"><span>${esc(h.timeframe || "4H")} bar close</span>
          <span>${utc(closeTs)} UTC</span></div>
      </div>`;
    }).join("") + `</div>`
  ) : `<div class="empty"><b>No alerts yet</b>
       The scanner runs after every 4H close (00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC).
       Anything that crosses will show up here.</div>`;

  const steps = [
    ["All pairs", last.universe_total], ["USDT", last.after_quote],
    ["Tradable", last.after_status], ["Non-leveraged", last.after_leveraged],
    ["Volume", last.after_volume], ["Market cap", last.after_mcap],
    ["Hits", last.hits],
  ].filter(([, v]) => v !== undefined && v !== null);
  document.getElementById("funnel").innerHTML = steps.length
    ? steps.map(([k, v]) => `<div class="step"><i>${k}</i><b>${v}</b></div>`)
        .join('<span class="arrow">→</span>')
    : '<div class="step"><i>waiting for the first scan</i></div>';

  document.getElementById("scans").innerHTML = (s.scans || []).map(r => `
    <tr><td>${utc(r.bar_ts + (last.interval_seconds || 14400))}</td>
    <td>${r.scanned}</td><td>${r.hits}</td><td>${r.alerted}</td>
    <td>${r.skipped_duplicate}</td><td>${r.errors}</td>
    <td>${r.duration != null ? r.duration.toFixed(1) + "s" : "—"}</td></tr>`).join("")
    || `<tr><td colspan="7" style="text-align:center;color:var(--muted)">no scans recorded yet</td></tr>`;

  document.getElementById("foot").textContent =
    `MACD(${s.config.macd_fast},${s.config.macd_slow},${s.config.macd_signal}) · ` +
    `EMA${s.config.ema_len} · ${s.config.interval} · min mcap ${fmtUsd(s.config.min_mcap)} · ` +
    `min 24h vol ${s.config.min_quote_volume_24h > 0 ? fmtUsd(s.config.min_quote_volume_24h) : "off"} · ` +
    `uptime ${Math.floor(s.uptime_seconds / 3600)}h ${Math.floor((s.uptime_seconds % 3600) / 60)}m`;
}

async function load() {
  try {
    const r = await fetch("/api/state", {cache: "no-store"});
    if (r.ok) { state = await r.json(); render(); }
  } catch (e) { /* keep showing the last good state */ }
}
load();
setInterval(load, 30000);
setInterval(render, 1000);   // keeps the countdown live between fetches
</script>
</body>
</html>
"""
