/* Pre-IR Screener — frontend.
 * Loads JSON produced by the GitHub Actions pipeline and renders a sortable,
 * filterable table. No mock data — if the JSON files are missing, an error
 * banner is shown.
 */
(function () {
  "use strict";

  const DATA_BASE = "./data";

  const $  = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  let evaluated = [];
  let screened  = [];
  let lastUpdated = null;
  let sortKey = "announcement_date";
  let sortDir = 1;

  function fmtDate(s) {
    if (!s) return "—";
    const d = new Date(s);
    if (isNaN(d.getTime())) return s;
    const m = (d.getMonth() + 1).toString();
    const day = d.getDate().toString();
    const wk = ["日","月","火","水","木","金","土"][d.getDay()];
    return `${m}/${day} (${wk})`;
  }

  function fmtPct(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return v.toFixed(1) + "%";
  }

  function fmtSlope(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    const sign = v > 0 ? "+" : "";
    return sign + v.toFixed(2);
  }

  async function fetchJson(name) {
    const url = `${DATA_BASE}/${name}.json?ts=${Date.now()}`;
    const resp = await fetch(url, { cache: "no-store" });
    if (!resp.ok) throw new Error(`${name}.json HTTP ${resp.status}`);
    return resp.json();
  }

  async function load() {
    try {
      const [allEval, scr, last] = await Promise.all([
        fetchJson("all_evaluated"),
        fetchJson("screened"),
        fetchJson("last_updated").catch(() => null),
      ]);
      evaluated = allEval.items || [];
      screened  = scr.items || [];
      lastUpdated = last;

      if (scr.criteria) {
        $("#th-up").textContent  = scr.criteria.min_upward_revisions;
        $("#th-div").textContent = scr.criteria.min_consecutive_dividend_years;
      }

      renderMeta();
      renderSummary();
      render();
    } catch (e) {
      const main = document.querySelector("main.wrap");
      const div = document.createElement("div");
      div.className = "error";
      div.innerHTML = `
        <strong>データ読み込みエラー</strong><br/>
        ${e.message}<br/><br/>
        まだスクレイピングが一度も実行されていない可能性があります。<br/>
        GitHub Actions の <code>update-data</code> ワークフローを実行してください。
      `;
      main.prepend(div);
      $("#meta").textContent = "データ未取得";
    }
  }

  function renderMeta() {
    const meta = $("#meta");
    if (!lastUpdated || !lastUpdated.generated_at) {
      meta.textContent = "最終更新: 不明 (データ未取得)";
      return;
    }
    const d = new Date(lastUpdated.generated_at);
    const primary = lastUpdated.schedule_primary_source || "なし";
    meta.textContent =
      `最終更新: ${d.toLocaleString("ja-JP")} ` +
      `/ スケジュール元: ${primary} ` +
      `/ 評価対象: ${lastUpdated.evaluated_count} 銘柄 ` +
      `/ 通過: ${lastUpdated.screened_count} 銘柄`;

    renderDiagnostics();
  }

  function renderDiagnostics() {
    const host = document.getElementById("diagnostics");
    if (!host) return;
    if (!lastUpdated) { host.innerHTML = ""; return; }

    const parts = [];
    const sr = lastUpdated.schedule_sources_results || {};
    const keys = Object.keys(sr);
    if (keys.length) {
      const rows = keys.map((k) => {
        const v = sr[k] || {};
        const cls = v.items > 0 ? "ok" : "ng";
        const status = v.http_status ? ` HTTP ${v.http_status}` : "";
        const err = v.error ? ` — ${escapeHtml(v.error).slice(0, 140)}` : "";
        return `<li class="${cls}"><strong>${k}</strong>: ${v.items || 0} rows${status}${err}</li>`;
      }).join("");
      parts.push(
        `<div class="diag-section">
          <h3>スケジュール・スクレイピング結果</h3>
          <ul class="diag-list">${rows}</ul>
        </div>`
      );
    }

    const ce = lastUpdated.fundamentals_connectivity_error;
    if (ce) {
      parts.push(
        `<div class="diag-section">
          <h3>ファンダメンタルズ取得エラー</h3>
          <p class="ng"><strong>${escapeHtml(ce.source || "")}</strong>
          — HTTP ${ce.status || "?"}</p>
          <pre>${escapeHtml(ce.body || "")}</pre>
        </div>`
      );
    } else if (lastUpdated.fundamentals_count !== undefined) {
      parts.push(
        `<div class="diag-section">
          <h3>ファンダメンタルズ取得結果</h3>
          <p>${lastUpdated.fundamentals_count} 銘柄取得
          (失敗 ${lastUpdated.fundamentals_failure_count || 0})</p>
        </div>`
      );
    }

    host.innerHTML = parts.join("");
  }

  function renderSummary() {
    const upcoming = evaluated.length;
    const passed = evaluated.filter((x) => x.passes_all).length;
    const avgScore = passed
      ? Math.round(
          evaluated
            .filter((x) => x.passes_all)
            .reduce((a, b) => a + b.score, 0) / passed
        )
      : 0;
    const next7 = evaluated.filter((x) => {
      if (!x.announcement_date) return false;
      const d = new Date(x.announcement_date);
      const days = (d - new Date()) / (1000 * 60 * 60 * 24);
      return days >= -1 && days <= 7;
    }).length;

    $("#summary").innerHTML = `
      <div class="card"><div class="label">評価対象</div><div class="value">${upcoming}</div></div>
      <div class="card"><div class="label">通過銘柄</div><div class="value">${passed}</div></div>
      <div class="card"><div class="label">通過の平均スコア</div><div class="value">${avgScore}</div></div>
      <div class="card"><div class="label">7日以内の発表</div><div class="value">${next7}</div></div>
    `;
  }

  function getRows() {
    const passOnly = $("#filter-pass").checked;
    const minScore = parseInt($("#filter-score").value || "0", 10);
    const days = parseInt($("#filter-days").value || "9999", 10);
    const q = ($("#filter-q").value || "").trim().toLowerCase();
    const today = new Date(); today.setHours(0,0,0,0);

    let rows = passOnly ? evaluated.filter((x) => x.passes_all) : evaluated;

    rows = rows.filter((r) => {
      if ((r.score || 0) < minScore) return false;

      if (days < 9999) {
        if (!r.announcement_date) return false;
        const d = new Date(r.announcement_date);
        const diff = Math.round((d - today) / (1000 * 60 * 60 * 24));
        if (diff < -1 || diff > days) return false;
      }

      if (q) {
        const hay = `${r.code} ${r.name || ""}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    const k = sortKey;
    rows.sort((a, b) => {
      let va, vb;
      if (k === "op_margin_slope") {
        va = (a.op_margin_trend && a.op_margin_trend.slope) || 0;
        vb = (b.op_margin_trend && b.op_margin_trend.slope) || 0;
      } else {
        va = a[k]; vb = b[k];
      }
      if (va === null || va === undefined) va = "";
      if (vb === null || vb === undefined) vb = "";
      if (typeof va === "number" && typeof vb === "number") return (va - vb) * sortDir;
      return String(va).localeCompare(String(vb)) * sortDir;
    });

    return rows;
  }

  function render() {
    const rows = getRows();
    const tbody = $("#tbody");

    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="10" class="empty">該当する銘柄がありません</td></tr>`;
      return;
    }

    const html = rows.map((r) => {
      const passClass = r.passes_all ? "pass" : "fail";
      const scoreClass = r.score >= 70 ? "high" : r.score < 40 ? "low" : "";
      const c = r.criteria || {};
      const trend = r.op_margin_trend || {};

      const irbankUrl  = `https://irbank.net/${r.code}`;
      const kabutanUrl = `https://kabutan.jp/stock/?code=${r.code}`;

      return `
        <tr class="${passClass}">
          <td>${fmtDate(r.announcement_date)}</td>
          <td><strong>${r.code}</strong></td>
          <td>${escapeHtml(r.name || "")}</td>
          <td><span class="score-pill ${scoreClass}">${r.score}</span></td>
          <td>${r.upward_revisions} 回</td>
          <td>${r.dividend_streak_years} 年</td>
          <td>${fmtPct(r.op_margin_latest)}</td>
          <td>${fmtSlope(trend.slope)}</td>
          <td>
            <span class="crit-marks">
              <span class="${c.upward_ok ? "ok" : "ng"}" title="上方修正">U</span>
              <span class="${c.dividend_ok ? "ok" : "ng"}" title="連続増配">D</span>
              <span class="${c.margin_trend_ok ? "ok" : "ng"}" title="営業利益率トレンド">M</span>
            </span>
            ${r.passes_all ? '<span class="badge">通過</span>' : '<span class="badge ng">未通過</span>'}
          </td>
          <td>
            <a href="${kabutanUrl}" target="_blank" rel="noopener">株探</a> ·
            <a href="${irbankUrl}" target="_blank" rel="noopener">IR</a>
          </td>
        </tr>
      `;
    }).join("");

    tbody.innerHTML = html;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c])
    );
  }

  function bindControls() {
    ["filter-pass", "filter-score", "filter-days", "filter-q"].forEach((id) => {
      const el = document.getElementById(id);
      el.addEventListener("input", render);
      el.addEventListener("change", render);
    });

    $$("thead th").forEach((th) => {
      const k = th.dataset.sort;
      if (!k) return;
      th.addEventListener("click", () => {
        if (sortKey === k) { sortDir *= -1; }
        else { sortKey = k; sortDir = 1; }
        render();
      });
    });
  }

  bindControls();
  load();
})();
