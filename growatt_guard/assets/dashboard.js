    (function () {
      /* ---------- shared chart helpers (SVG, coloured from CSS variables) ---------- */
      const isNum = function (v) { return typeof v === "number" && isFinite(v); };
      const esc = function (v) {
        return String(v === null || v === undefined ? "" : v).replace(/[&<>"]/g, function (c) {
          return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
        });
      };
      const clamp = function (v, lo, hi) { return Math.max(lo, Math.min(hi, v)); };
      const fmtKwh = function (v) { return isNum(v) ? v.toFixed(1) + " kWh" : "--"; };
      const fmtPct = function (v) { return isNum(v) ? Math.round(v) + "%" : "--"; };
      const fmtWatts = function (v) {
        if (!isNum(v)) return "--";
        return Math.abs(v) >= 1000 ? (v / 1000).toFixed(1) + " kW" : Math.round(v) + " W";
      };
      const fmtCount = function (v) { return isNum(v) ? String(Math.round(v)) : "--"; };

      function readJson(id, fallback) {
        const el = document.getElementById(id);
        if (!el) return fallback;
        try { return JSON.parse(el.textContent); } catch (e) { return fallback; }
      }

      function niceMax(value) {
        const steps = [1, 2, 4, 5, 6, 8, 10, 20, 30, 40, 50, 60, 80, 100];
        for (let i = 0; i < steps.length; i++) if (value <= steps[i]) return steps[i];
        return Math.ceil(value / 50) * 50;
      }

      function tipHtml(title, rows) {
        return "<strong>" + esc(title) + "</strong>" + rows.map(function (r) {
          const swatch = r.c ? '<i style="background:var(--' + r.c + ')"></i>' : "";
          return "<div>" + swatch + "<span>" + esc(r.n) + "</span><b>" + esc(r.v) + "</b></div>";
        }).join("");
      }

      function placeTip(host, tip, px) {
        tip.hidden = false;
        const w = tip.offsetWidth, W = host.clientWidth;
        tip.style.left = clamp(px + 12 + w > W ? px - w - 12 : px + 12, 0, Math.max(0, W - w)) + "px";
      }

      function showEmpty(host) {
        host.innerHTML = '<p class="chart-empty">No local history yet</p>';
      }

      function drawLineChart(id, labels, series, options) {
        const host = document.getElementById(id);
        if (!host) return;
        const W = host.clientWidth, H = host.clientHeight;
        if (!W) return;
        const n = labels.length;
        const hasValues = series.some(function (s) { return s.values.some(isNum); });
        if (n < 2 || !hasValues) { showEmpty(host); return; }
        const m = { l: 36, r: 8, t: 8, b: 20 };
        const x = function (i) { return m.l + (W - m.l - m.r) * i / (n - 1); };
        const y = function (v) { return m.t + (H - m.t - m.b) * (1 - clamp(v, 0, options.yMax) / options.yMax); };
        let s = "";
        options.ticks.forEach(function (t) {
          s += '<line class="' + (t === 0 ? "axisline" : "gridline") + '" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + y(t) + '" y2="' + y(t) + '"/>';
          s += '<text x="' + (m.l - 6) + '" y="' + (y(t) + 4) + '" text-anchor="end">' + esc(options.tickFmt(t)) + "</text>";
        });
        let prevHour = null;
        labels.forEach(function (label, i) {
          const hour = parseInt(String(label).slice(0, 2), 10);
          if (hour !== prevHour && hour % 4 === 0 && i > 2 && i < n - 3) {
            s += '<text x="' + x(i) + '" y="' + (H - 4) + '" text-anchor="middle">' + String(hour).padStart(2, "0") + ":00</text>";
          }
          prevHour = hour;
        });
        if (options.ref && isNum(options.ref.value)) {
          const ry = y(options.ref.value);
          s += '<line x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + ry + '" y2="' + ry + '" stroke="var(--ink-3)" stroke-width="1" stroke-dasharray="3 4"/>';
          s += '<text x="' + (W - m.r) + '" y="' + (ry - 5) + '" text-anchor="end">' + esc(options.ref.label) + "</text>";
        }
        series.forEach(function (se) {
          let d = "", pen = false, first = null, last = null;
          se.values.forEach(function (v, i) {
            if (!isNum(v)) { pen = false; return; }
            d += (pen ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1);
            pen = true;
            if (first === null) first = i;
            last = i;
          });
          if (last === null) return;
          if (options.area) {
            s += '<path d="' + d + "L" + x(last).toFixed(1) + " " + y(0) + "L" + x(first).toFixed(1) + " " + y(0) + 'Z" fill="var(--' + se.c + ')" opacity=".14"/>';
          }
          s += '<path d="' + d + '" fill="none" stroke="var(--' + se.c + ')" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';
          s += '<circle cx="' + x(last) + '" cy="' + y(se.values[last]) + '" r="4" fill="var(--' + se.c + ')" stroke="var(--surface)" stroke-width="2"/>';
        });
        s += '<line data-cross y1="' + m.t + '" y2="' + (H - m.b) + '" stroke="var(--ink-3)" stroke-width="1" visibility="hidden"/>';
        host.innerHTML = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + esc(options.label) + '">' + s + '</svg><div class="chart-tip" hidden></div>';
        const svg = host.firstChild, cross = svg.querySelector("[data-cross]"), tip = host.lastChild;
        svg.addEventListener("pointermove", function (e) {
          const px = e.clientX - svg.getBoundingClientRect().left;
          const i = clamp(Math.round((px - m.l) / (W - m.l - m.r) * (n - 1)), 0, n - 1);
          cross.setAttribute("x1", x(i));
          cross.setAttribute("x2", x(i));
          cross.setAttribute("visibility", "visible");
          const rows = series.map(function (se) { return { c: se.c, n: se.name, v: options.fmt(se.values[i]) }; });
          (options.extraRows ? options.extraRows(i) : []).forEach(function (row) { rows.push(row); });
          tip.innerHTML = tipHtml(labels[i], rows);
          placeTip(host, tip, x(i));
        });
        svg.addEventListener("pointerleave", function () {
          tip.hidden = true;
          cross.setAttribute("visibility", "hidden");
        });
      }

      /* options.mode is "stack" or "group"; options.tick is an optional series drawn as a line across each bar. */
      function drawBarChart(id, labels, series, options) {
        const host = document.getElementById(id);
        if (!host) return;
        const W = host.clientWidth, H = host.clientHeight;
        if (!W) return;
        const n = labels.length;
        const hasValues = series.some(function (s) { return s.values.some(isNum); });
        if (n === 0 || !hasValues) { showEmpty(host); return; }
        const fmt = options.fmt || fmtKwh;
        const m = { l: 30, r: 6, t: 8, b: 20 }, band = (W - m.l - m.r) / n;
        const y = function (v) { return m.t + (H - m.t - m.b) * (1 - clamp(v, 0, options.yMax) / options.yMax); };
        const base = y(0);
        /* A bar from y0 (bottom) up to y1 (top) with rounded top corners. */
        const bar = function (bx, y0, y1, bw, r) {
          const h = y0 - y1;
          if (h <= 0.5) return "";
          r = Math.min(r, h, bw / 2);
          return "M" + bx + " " + y0 + "V" + (y1 + r) + "Q" + bx + " " + y1 + " " + (bx + r) + " " + y1 +
            "H" + (bx + bw - r) + "Q" + (bx + bw) + " " + y1 + " " + (bx + bw) + " " + (y1 + r) + "V" + y0 + "Z";
        };
        let s = "";
        [0, options.yMax / 2, options.yMax].forEach(function (t) {
          s += '<line class="' + (t === 0 ? "axisline" : "gridline") + '" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + y(t) + '" y2="' + y(t) + '"/>';
          s += '<text x="' + (m.l - 6) + '" y="' + (y(t) + 4) + '" text-anchor="end">' + t + "</text>";
        });
        labels.forEach(function (label, i) {
          const cx = m.l + band * i + band / 2;
          const emphasis = options.emphasizeLast && i === n - 1 ? ' style="fill:var(--ink);font-weight:500"' : "";
          s += '<text x="' + cx + '" y="' + (H - 4) + '" text-anchor="middle"' + emphasis + ">" + esc(label) + "</text>";
          if (options.mode === "stack") {
            const bw = Math.min(26, band * 0.5);
            const live = series.filter(function (se) { return isNum(se.values[i]) && se.values[i] > 0; });
            let acc = 0;
            live.forEach(function (se, k) {
              const y0 = y(acc);
              acc += se.values[i];
              const isTop = k === live.length - 1;
              const y1 = y(acc) + (isTop ? 0 : 2);
              s += isTop
                ? '<path d="' + bar(cx - bw / 2, y0, y1, bw, 4) + '" fill="var(--' + se.c + ')"/>'
                : '<rect x="' + (cx - bw / 2) + '" y="' + y1 + '" width="' + bw + '" height="' + Math.max(0, y0 - y1) + '" fill="var(--' + se.c + ')"/>';
            });
            if (options.tick && isNum(options.tick.values[i])) {
              const ty = y(options.tick.values[i]);
              s += '<line x1="' + (cx - bw / 2 - 5) + '" x2="' + (cx + bw / 2 + 5) + '" y1="' + ty + '" y2="' + ty + '" stroke="var(--ink)" stroke-width="2"/>';
            }
          } else {
            const k = series.length, bw = Math.min(14, band * 0.7 / k), gap = 2, total = bw * k + gap * (k - 1);
            series.forEach(function (se, j) {
              const v = se.values[i];
              if (isNum(v) && v > 0) s += '<path d="' + bar(cx - total / 2 + j * (bw + gap), base, y(v), bw, 4) + '" fill="var(--' + se.c + ')"/>';
            });
          }
          s += '<rect data-i="' + i + '" x="' + (m.l + band * i) + '" y="' + m.t + '" width="' + band + '" height="' + (H - m.t - m.b) + '" fill="transparent"/>';
        });
        host.innerHTML = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + esc(options.label) + '">' + s + '</svg><div class="chart-tip" hidden></div>';
        const svg = host.firstChild, tip = host.lastChild;
        svg.addEventListener("pointermove", function (e) {
          const raw = e.target && e.target.getAttribute ? e.target.getAttribute("data-i") : null;
          if (raw === null) { tip.hidden = true; return; }
          const i = Number(raw);
          const rows = series.map(function (se) { return { c: se.c, n: se.name, v: fmt(se.values[i]) }; });
          if (options.tick) rows.push({ c: "ink", n: options.tick.name, v: fmt(options.tick.values[i]) });
          tip.innerHTML = tipHtml(options.titles ? options.titles[i] : labels[i], rows);
          placeTip(host, tip, m.l + band * i + band / 2);
        });
        svg.addEventListener("pointerleave", function () { tip.hidden = true; });
      }

      /* ---------- chart data ---------- */
      const history = readJson("metric-history-data", {});
      const power = history.power || {}, socHistory = history.soc || {}, daily = history.daily || {};
      const automation = readJson("chart-data", {});
      const forecasts = readJson("forecast-history-data", []);
      const generatedAt = new Date(document.body.dataset.generatedAt || Date.now());
      const maxOf = function (lists) {
        return Math.max.apply(null, [0].concat(lists.map(function (list) {
          return Math.max.apply(null, [0].concat((list || []).filter(isNum)));
        })));
      };

      /* Daily history labels are "MM-DD"; show weekdays and keep the full date for the tooltip. */
      function dayLabels(labels) {
        const short = [], long = [];
        (labels || []).forEach(function (label, i) {
          const match = /^(\d{2})-(\d{2})$/.exec(String(label));
          if (!match) { short.push(String(label)); long.push(String(label)); return; }
          const date = new Date(generatedAt.getFullYear(), Number(match[1]) - 1, Number(match[2]));
          if (date.getTime() - generatedAt.getTime() > 2 * 86400000) date.setFullYear(date.getFullYear() - 1);
          short.push(i === labels.length - 1 ? "Today" : date.toLocaleDateString("en-GB", { weekday: "short" }));
          long.push(date.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "short" }));
        });
        return { short: short, long: long };
      }

      function drawCharts() {
        const powerMax = Math.max(1, niceMax(maxOf([power.pv_w, power.load_w, power.grid_w]) / 1000)) * 1000;
        drawLineChart("power-trend-chart", power.labels || [], [
          { name: "Solar", c: "solar", values: power.pv_w || [] },
          { name: "Load", c: "load-c", values: power.load_w || [] },
          { name: "Grid", c: "grid-c", values: power.grid_w || [] }
        ], {
          label: "Solar, load and grid power over the last 24 hours",
          yMax: powerMax, ticks: [0, powerMax / 2, powerMax],
          tickFmt: function (t) { return (t / 1000) + " kW"; }, fmt: fmtWatts,
          extraRows: function (i) {
            const rows = [];
            const net = (power.battery_net_w || [])[i];
            if (isNum(net)) rows.push({ n: "Battery", v: fmtWatts(Math.abs(net)) + (net > 0 ? " out" : net < 0 ? " in" : "") });
            const mode = (power.mode || [])[i];
            if (mode) rows.push({ n: "Mode", v: mode });
            return rows;
          }
        });
        const socHost = document.getElementById("soc-trend-chart");
        const reference = socHost ? parseFloat(socHost.dataset.reference) : NaN;
        drawLineChart("soc-trend-chart", socHistory.labels || [], [
          { name: "SOC", c: "battery", values: socHistory.soc || [] }
        ], {
          label: "Battery state of charge over the last 24 hours",
          yMax: 100, ticks: [0, 50, 100], tickFmt: function (t) { return t + "%"; }, fmt: fmtPct, area: true,
          ref: isNum(reference) ? { value: reference, label: "reserve " + fmtPct(reference) } : null
        });
        const days = dayLabels(daily.labels);
        const supplyTotals = (daily.pv_kwh || []).map(function (v, i) {
          return (isNum(v) ? v : 0) + (isNum((daily.grid_kwh || [])[i]) ? daily.grid_kwh[i] : 0);
        });
        drawBarChart("supply-energy-chart", days.short, [
          { name: "Solar", c: "solar", values: daily.pv_kwh || [] },
          { name: "Grid", c: "grid-c", values: daily.grid_kwh || [] }
        ], {
          label: "Daily solar and grid supply with load, last 7 days, in kWh", mode: "stack", titles: days.long,
          emphasizeLast: true, yMax: niceMax(Math.max(1, maxOf([supplyTotals, daily.load_kwh]))),
          tick: { name: "Load", values: daily.load_kwh || [] }
        });
        const energyHistory = readJson("energy-history-data", {});
        const monthDays = energyHistory.month_days || [];
        if (monthDays.length) {
          const monthOf = String(energyHistory.month || "");
          const monthName = monthOf
            ? new Date(monthOf + "-01T12:00").toLocaleDateString("en-GB", { month: "long", year: "numeric" })
            : "This month";
          const solar = monthDays.map(function (row) { return row.solar_kwh; });
          const utility = monthDays.map(function (row) { return row.utility_kwh; });
          const load = monthDays.map(function (row) { return row.load_kwh; });
          const supply = solar.map(function (v, i) {
            return (isNum(v) ? v : 0) + (isNum(utility[i]) ? utility[i] : 0);
          });
          drawBarChart("month-energy-chart", monthDays.map(function (row) { return String(row.day); }), [
            { name: "Solar", c: "solar", values: solar },
            { name: "Utility", c: "grid-c", values: utility }
          ], {
            label: "Daily solar and Utility supply with load for " + monthName + ", in kWh",
            mode: "stack", emphasizeLast: false,
            titles: monthDays.map(function (row) {
              return new Date(row.date + "T12:00").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
            }),
            yMax: niceMax(Math.max(1, maxOf([supply, load]))),
            tick: { name: "Load", values: load }
          });
        }
        drawBarChart("battery-energy-chart", days.short, [
          { name: "Charge", c: "battery", values: daily.charge_kwh || [] },
          { name: "Discharge", c: "neutral-mark", values: daily.discharge_kwh || [] }
        ], {
          label: "Battery energy charged and discharged per day, in kWh", mode: "group", titles: days.long,
          emphasizeLast: true, yMax: niceMax(Math.max(1, maxOf([daily.charge_kwh, daily.discharge_kwh])))
        });
        const forecastDate = function (row, options) {
          const date = new Date(String(row.forecast_date) + "T12:00");
          return isNaN(date.getTime()) ? String(row.forecast_date) : date.toLocaleDateString("en-GB", options);
        };
        const predicted = forecasts.map(function (row) { return row.predicted_kwh; });
        const actual = forecasts.map(function (row) { return row.actual_kwh; });
        drawBarChart("forecast-accuracy-chart", forecasts.map(function (row) { return forecastDate(row, { weekday: "short" }); }), [
          { name: "Forecast", c: "neutral-mark", values: predicted },
          { name: "Actual", c: "solar", values: actual }
        ], {
          label: "Forecast and actual solar generation per day, in kWh", mode: "group",
          titles: forecasts.map(function (row) {
            return forecastDate(row, { weekday: "long", day: "numeric", month: "short" }) + (row.weather_category ? " · " + row.weather_category : "");
          }),
          yMax: niceMax(Math.max(1, maxOf([predicted, actual])))
        });
        const counts = [automation.preserve_checks, automation.utility_switches, automation.watchdog_repairs];
        drawBarChart("history-chart", (automation.labels || []).map(function (l) { return String(l).slice(0, 3); }), [
          { name: "Preserve checks", c: "battery", values: automation.preserve_checks || [] },
          { name: "Utility switches", c: "grid-c", values: automation.utility_switches || [] },
          { name: "Watchdog repairs", c: "solar", values: automation.watchdog_repairs || [] }
        ], {
          label: "Preserve checks, utility switches and watchdog repairs per day", mode: "group",
          titles: automation.labels || [], fmt: fmtCount,
          yMax: Math.max(4, Math.ceil(maxOf(counts) / 2) * 2)
        });
      }

      /* ---------- tabs: one panel at a time, chosen by the URL hash ---------- */
      const TABS = ["now", "energy", "automation", "system"];
      function showTab() {
        const wanted = location.hash.slice(1);
        const current = TABS.indexOf(wanted) >= 0 ? wanted : "now";
        document.querySelectorAll("[data-tab-panel]").forEach(function (panel) {
          panel.classList.toggle("active", panel.dataset.tabPanel === current);
        });
        document.querySelectorAll(".tabs a[data-tab]").forEach(function (link) {
          const active = link.dataset.tab === current;
          link.classList.toggle("active", active);
          if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
        });
        drawCharts();
      }
      document.documentElement.classList.add("js-tabs");
      window.addEventListener("hashchange", showTab);
      let resizeTimer = null;
      window.addEventListener("resize", function () {
        window.clearTimeout(resizeTimer);
        resizeTimer = window.setTimeout(drawCharts, 120);
      });
      showTab();
    })();
    (function () {
      /* ---------- data freshness: badge, age text and the stale alert ---------- */
      const badge = document.querySelector("[data-refresh-badge]");
      const ageNodes = Array.from(document.querySelectorAll("[data-refresh-age]"));
      const staleAlert = document.querySelector("[data-stale-alert]");
      if (!badge || ageNodes.length === 0) return;

      const generatedAt = new Date(badge.dataset.generatedAt);
      const staleMinutes = Number(badge.dataset.staleMinutes || "30");

      function plural(value, unit) {
        return value + " " + unit + (value === 1 ? "" : "s");
      }

      function formatAge(milliseconds) {
        const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
        if (totalSeconds < 60) return plural(totalSeconds, "second");
        const totalMinutes = Math.floor(totalSeconds / 60);
        if (totalMinutes < 60) return plural(totalMinutes, "minute");
        const hours = Math.floor(totalMinutes / 60);
        const minutes = totalMinutes % 60;
        return minutes ? plural(hours, "hour") + " " + plural(minutes, "minute") : plural(hours, "hour");
      }

      function updateRefreshHealth() {
        if (Number.isNaN(generatedAt.getTime())) {
          badge.textContent = "UNKNOWN";
          badge.className = "badge badge-warn";
          ageNodes.forEach(function (node) { node.textContent = "Generated time could not be read."; });
          return;
        }
        const ageMs = Date.now() - generatedAt.getTime();
        const stale = ageMs > staleMinutes * 60 * 1000;
        badge.textContent = stale ? "STALE" : "OK";
        badge.className = "badge " + (stale ? "badge-warn" : "badge-ok");
        if (staleAlert) staleAlert.hidden = !stale;
        ageNodes.forEach(function (node) {
          node.textContent = node.dataset.refreshAge === "short"
            ? "Updated " + formatAge(ageMs) + " ago"
            : "Generated " + formatAge(ageMs) + " ago; stale after " + staleMinutes + " minutes.";
        });
      }

      updateRefreshHealth();
      window.setInterval(updateRefreshHealth, 30000);
    })();
    function toggleDashTheme() {
      const html = document.documentElement;
      const btn = document.getElementById('theme-toggle-btn');
      const isLight = html.classList.toggle('theme-light');
      try { localStorage.setItem('dash-theme', isLight ? 'light' : 'dark'); } catch(e) {}
      if (btn) btn.textContent = isLight ? 'Dark' : 'Light';
    }
    (function() {
      try {
        if (localStorage.getItem('dash-theme') === 'light') {
          document.documentElement.classList.add('theme-light');
          const btn = document.getElementById('theme-toggle-btn');
          if (btn) btn.textContent = 'Dark';
        }
      } catch(e) {}
    })();
    (function() {
      const card = document.getElementById('estate-profile');
      if (!card) return;
      const labelEl = document.getElementById('profile-label');
      const descEl = document.getElementById('profile-description');
      const outagesEl = document.getElementById('profile-outages');
      const buttonsEl = document.getElementById('profile-buttons');
      const dialog = document.getElementById('profile-confirm');
      const form = document.getElementById('profile-confirm-form');
      const nameInput = document.getElementById('profile-confirm-name');
      const titleEl = document.getElementById('profile-confirm-title');
      const dialogDesc = document.getElementById('profile-confirm-desc');
      const dialogOutages = document.getElementById('profile-confirm-outages');
      const jobList = document.getElementById('profile-confirm-jobs');
      const cancelBtn = document.getElementById('profile-confirm-cancel');
      let profiles = {};

      function openConfirm(name) {
        const profile = profiles[name];
        if (!profile || !form || !nameInput) return;
        nameInput.value = name;
        if (titleEl) titleEl.textContent = 'Switch to ' + profile.label;
        if (dialogDesc) dialogDesc.textContent = profile.description || '';
        if (dialogOutages) dialogOutages.textContent = profile.outages_text || 'none';
        if (jobList) {
          jobList.textContent = '';
          (profile.jobs || []).forEach(function(job) {
            const item = document.createElement('li');
            item.textContent = job.when + ' — ' + job.name;
            jobList.appendChild(item);
          });
        }
        if (dialog && typeof dialog.showModal === 'function') {
          dialog.showModal();
        } else if (window.confirm('Switch to ' + profile.label + '?')) {
          form.submit();
        }
      }

      function renderProfile(data) {
        if (!data || !data.active_profile) return;
        if (labelEl) labelEl.textContent = data.label || data.active_profile;
        if (descEl) descEl.textContent = data.description || '';
        if (outagesEl) outagesEl.textContent = 'Outages: ' + (data.outages_text || 'none');
        profiles = {};
        (data.profiles || []).forEach(function(profile) {
          profiles[profile.name] = profile;
        });
        if (!buttonsEl) return;
        buttonsEl.textContent = '';
        (data.profiles || []).forEach(function(profile) {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'pill profile-button';
          button.dataset.profile = profile.name;
          button.textContent = profile.active ? profile.label + ' · active' : profile.label;
          if (profile.active) {
            button.disabled = true;
          } else {
            button.addEventListener('click', function() { openConfirm(profile.name); });
          }
          buttonsEl.appendChild(button);
        });
      }

      if (cancelBtn && dialog) {
        cancelBtn.addEventListener('click', function() { dialog.close(); });
      }

      fetch('/api/profile', { cache: 'no-store' })
        .then(function(response) { return response.ok ? response.json() : null; })
        .then(renderProfile)
        .catch(function() {});
    })();
