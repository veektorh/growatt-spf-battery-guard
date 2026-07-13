    (function () {
      const canvas = document.getElementById("history-chart");
      const dataEl = document.getElementById("chart-data");
      if (canvas && dataEl) {
        try {
          const data = JSON.parse(dataEl.textContent);
          const PAD = { top: 12, right: 12, bottom: 28, left: 32 };
          const SERIES = [
            { key: "preserve_checks", label: "Preserve checks", color: "#3AC87A" },
            { key: "utility_switches", label: "Utility switches", color: "#F5A82A" },
            { key: "watchdog_repairs", label: "Watchdog repairs", color: "#5B8DEF" }
          ];

          function setupHistoryCanvas() {
            const ctx = canvas.getContext("2d");
            const dpr = window.devicePixelRatio || 1;
            const rect = canvas.getBoundingClientRect();
            canvas.width = rect.width * dpr || 600 * dpr;
            canvas.height = 160 * dpr;
            ctx.scale(dpr, dpr);
            return { ctx, width: canvas.width / dpr, height: 160 };
          }

          function drawHistoryTooltip(ctx, lines, x, width) {
            ctx.font = "bold 11px system-ui, sans-serif";
            const lineH = 16;
            const tipW = Math.max(...lines.map(function(line) { return ctx.measureText(line).width; })) + 20;
            const tipH = lines.length * lineH + 12;
            let tx = x + 10;
            if (tx + tipW > width - PAD.right) tx = x - tipW - 10;
            const ty = PAD.top + 4;
            ctx.fillStyle = "rgba(22,27,36,0.92)";
            ctx.beginPath();
            ctx.roundRect(tx, ty, tipW, tipH, 6);
            ctx.fill();
            ctx.strokeStyle = "rgba(255,255,255,0.1)";
            ctx.lineWidth = 1;
            ctx.stroke();
            lines.forEach(function(line, i) {
              ctx.fillStyle = i === 0 ? "#6A7A99" : "#E2E8F0";
              ctx.font = i === 0 ? "11px system-ui, sans-serif" : "bold 11px system-ui, sans-serif";
              ctx.fillText(line, tx + 10, ty + 14 + i * lineH);
            });
          }

          function drawHistoryChart() {
            const setup = setupHistoryCanvas();
            const ctx = setup.ctx;
            const W = setup.width, H = setup.height;
            const chartW = W - PAD.left - PAD.right;
            const chartH = H - PAD.top - PAD.bottom;
            const n = data.labels.length;
            const maxVal = Math.max(1, ...data.preserve_checks, ...data.utility_switches, ...data.watchdog_repairs);
            const yStep = Math.ceil(maxVal / 4);
            ctx.font = "11px system-ui, sans-serif";
            ctx.fillStyle = "#6A7A99";
            for (let y = 0; y <= maxVal; y += yStep) {
              const px = PAD.top + chartH - (y / maxVal) * chartH;
              ctx.fillText(y, 0, px + 4);
              ctx.strokeStyle = "#2C3548"; ctx.lineWidth = 1;
              ctx.beginPath(); ctx.moveTo(PAD.left, px); ctx.lineTo(PAD.left + chartW, px); ctx.stroke();
            }
            const groupW = n > 0 ? chartW / n : chartW;
            const barW = Math.max(4, groupW / 4 - 2);
            SERIES.forEach(function (series, si) {
              ctx.fillStyle = series.color;
              data[series.key].forEach(function (val, i) {
                const x = PAD.left + i * groupW + si * (barW + 2) + (groupW - SERIES.length * (barW + 2)) / 2;
                const barH = (val / maxVal) * chartH;
                ctx.fillRect(x, PAD.top + chartH - barH, barW, barH || 1);
              });
            });
            data.labels.forEach(function (label, i) {
              ctx.fillStyle = "#6A7A99";
              const x = PAD.left + i * groupW + groupW / 2;
              ctx.textAlign = "center";
              ctx.fillText(label, x, H - 6);
            });
            ctx.textAlign = "left";
            const legendY = PAD.top; const legendX = PAD.left + chartW - 200;
            SERIES.forEach(function (series, i) {
              ctx.fillStyle = series.color;
              ctx.fillRect(legendX + i * 70, legendY, 8, 8);
              ctx.fillStyle = "#6A7A99";
              ctx.fillText(series.label.split(" ")[0], legendX + i * 70 + 11, legendY + 8);
            });
          }

          function drawHistoryTip(mx) {
            const rect = canvas.getBoundingClientRect();
            const W = rect.width || 600, H = 160;
            const chartW = W - PAD.left - PAD.right;
            const chartH = H - PAD.top - PAD.bottom;
            if (!data.labels.length || mx < PAD.left || mx > W - PAD.right) return;
            const groupW = chartW / data.labels.length;
            const idx = Math.floor((mx - PAD.left) / groupW);
            if (idx < 0 || idx >= data.labels.length) return;
            const x = PAD.left + idx * groupW + groupW / 2;
            const ctx = canvas.getContext("2d");
            ctx.save();
            ctx.fillStyle = "rgba(255,255,255,0.04)";
            ctx.fillRect(PAD.left + idx * groupW, PAD.top, groupW, chartH);
            ctx.strokeStyle = "rgba(255,255,255,0.15)";
            ctx.lineWidth = 1;
            ctx.setLineDash([3, 3]);
            ctx.beginPath();
            ctx.moveTo(x, PAD.top);
            ctx.lineTo(x, H - PAD.bottom);
            ctx.stroke();
            ctx.setLineDash([]);
            const lines = [data.labels[idx]];
            SERIES.forEach(function (series) {
              const value = data[series.key][idx];
              if (typeof value === "number" && isFinite(value)) {
                lines.push(series.label + ":  " + Math.round(value));
              }
            });
            drawHistoryTooltip(ctx, lines, x, W);
            ctx.restore();
          }

          drawHistoryChart();
          canvas.addEventListener("mousemove", function(e) {
            const rect = canvas.getBoundingClientRect();
            drawHistoryChart();
            drawHistoryTip(e.clientX - rect.left);
          });
          canvas.addEventListener("mouseleave", drawHistoryChart);
        } catch (e) { /* chart render failed */ }
      }
    })();
    (function () {
      const dataEl = document.getElementById("metric-history-data");
      if (!dataEl) return;

      function clean(values) {
        return values.map(function (v) { return typeof v === "number" && isFinite(v) ? v : null; });
      }

      function setupCanvas(id) {
        const canvas = document.getElementById(id);
        if (!canvas) return null;
        const ctx = canvas.getContext("2d");
        const dpr = window.devicePixelRatio || 1;
        const rect = canvas.getBoundingClientRect();
        const width = rect.width || 600;
        const height = rect.height || 220;
        canvas.width = width * dpr;
        canvas.height = height * dpr;
        ctx.scale(dpr, dpr);
        return { canvas, ctx, width, height };
      }

      function noData(ctx, width, height) {
        ctx.fillStyle = "#6A7A99";
        ctx.font = "13px system-ui, sans-serif";
        ctx.fillText("No local history yet", 18, height / 2);
      }

      function drawGrid(ctx, width, height, pad, maxVal, suffix) {
        ctx.font = "11px system-ui, sans-serif";
        ctx.fillStyle = "#6A7A99";
        ctx.strokeStyle = "#2C3548";
        ctx.lineWidth = 1;
        for (let i = 0; i <= 4; i++) {
          const y = pad.top + ((height - pad.top - pad.bottom) / 4) * i;
          const val = maxVal - (maxVal / 4) * i;
          ctx.beginPath();
          ctx.moveTo(pad.left, y);
          ctx.lineTo(width - pad.right, y);
          ctx.stroke();
          ctx.fillText(Math.round(val) + suffix, 6, y + 4);
        }
      }

      function formatChartValue(value, suffix) {
        if (suffix === "%") return value.toFixed(0) + "%";
        if (suffix === "kWh") return value.toFixed(value >= 10 ? 1 : 2) + " kWh";
        if (!suffix) return Math.round(value).toString();
        return value >= 1000 ? (value / 1000).toFixed(1) + " k" + suffix : Math.round(value) + " " + suffix;
      }

      function drawTooltipBox(ctx, lines, x, width, pad) {
        ctx.font = "bold 11px system-ui, sans-serif";
        const lineH = 16;
        const tipW = Math.max(...lines.map(function(l) { return ctx.measureText(l).width; })) + 20;
        const tipH = lines.length * lineH + 12;
        let tx = x + 10;
        if (tx + tipW > width - pad.right) tx = x - tipW - 10;
        const ty = pad.top + 4;
        ctx.fillStyle = "rgba(22,27,36,0.92)";
        ctx.beginPath();
        ctx.roundRect(tx, ty, tipW, tipH, 6);
        ctx.fill();
        ctx.strokeStyle = "rgba(255,255,255,0.1)";
        ctx.lineWidth = 1;
        ctx.stroke();
        lines.forEach(function(line, i) {
          ctx.fillStyle = i === 0 ? "#6A7A99" : "#E2E8F0";
          ctx.font = i === 0 ? "11px system-ui, sans-serif" : "bold 11px system-ui, sans-serif";
          ctx.fillText(line, tx + 10, ty + 14 + i * lineH);
        });
      }

      function drawLineChart(id, labels, series, options) {
        const setup = setupCanvas(id);
        if (!setup) return;
        const { ctx, width, height } = setup;
        const pad = { top: 14, right: 16, bottom: 28, left: 48 };
        const values = series.flatMap(function (s) { return clean(s.values).filter(function (v) { return v !== null; }); });
        if (labels.length < 2 || values.length === 0) {
          noData(ctx, width, height);
          return;
        }
        const maxVal = Math.max(options.minMax || 1, ...values);
        drawGrid(ctx, width, height, pad, maxVal, options.suffix || "");
        const chartW = width - pad.left - pad.right;
        const chartH = height - pad.top - pad.bottom;
        series.forEach(function (s) {
          const vals = clean(s.values);
          ctx.strokeStyle = s.color;
          ctx.lineWidth = 2;
          ctx.beginPath();
          let started = false;
          vals.forEach(function (value, index) {
            if (value === null) return;
            const x = pad.left + (chartW * index) / Math.max(1, labels.length - 1);
            const y = pad.top + chartH - (value / maxVal) * chartH;
            if (!started) {
              ctx.moveTo(x, y);
              started = true;
            } else {
              ctx.lineTo(x, y);
            }
          });
          ctx.stroke();
        });
        ctx.fillStyle = "#6A7A99";
        ctx.font = "11px system-ui, sans-serif";
        ctx.textAlign = "left";
        ctx.fillText(labels[0] || "", pad.left, height - 8);
        ctx.textAlign = "right";
        ctx.fillText(labels[labels.length - 1] || "", width - pad.right, height - 8);
        ctx.textAlign = "left";
      }

      function setupLineTooltip(id, labels, series, options) {
        const canvas = document.getElementById(id);
        if (!canvas) return;
        const pad = { top: 14, right: 16, bottom: 28, left: 48 };
        const suffix = options.suffix || "";
        let tipVisible = false;

        function redrawWithTip(mx) {
          const dpr = window.devicePixelRatio || 1;
          const rect = canvas.getBoundingClientRect();
          const width = rect.width || 600;
          const height = rect.height || 220;
          const chartW = width - pad.left - pad.right;
          const chartH = height - pad.top - pad.bottom;
          const vals = series.flatMap(function(s) { return s.values.filter(function(v) { return typeof v === "number" && isFinite(v); }); });
          if (vals.length === 0) return;
          const maxVal = Math.max(options.minMax || 1, ...vals);
          const idx = Math.round(((mx - pad.left) / chartW) * (labels.length - 1));
          if (idx < 0 || idx >= labels.length) return;
          const x = pad.left + (chartW * idx) / Math.max(1, labels.length - 1);
          const ctx = canvas.getContext("2d");
          ctx.save();
          ctx.strokeStyle = "rgba(255,255,255,0.15)";
          ctx.lineWidth = 1;
          ctx.setLineDash([3, 3]);
          ctx.beginPath();
          ctx.moveTo(x, pad.top);
          ctx.lineTo(x, height - pad.bottom);
          ctx.stroke();
          ctx.setLineDash([]);
          const lines = [labels[idx]];
          series.forEach(function(s) {
            const v = s.values[idx];
            if (typeof v === "number" && isFinite(v)) {
              lines.push(s.label + ":  " + formatChartValue(v, suffix));
            }
          });
          if (options.modes && options.modes[idx]) {
            lines.push("Mode:  " + options.modes[idx]);
          }
          if (options.batteryNet && typeof options.batteryNet[idx] === "number" && isFinite(options.batteryNet[idx])) {
            const bw = options.batteryNet[idx];
            const dir = bw > 0 ? "discharging" : (bw < 0 ? "charging" : "standby");
            lines.push("Battery:  " + Math.round(Math.abs(bw)) + " W " + dir);
          }
          drawTooltipBox(ctx, lines, x, width, pad);
          ctx.restore();
        }

        canvas.addEventListener("mousemove", function(e) {
          const rect = canvas.getBoundingClientRect();
          const mx = (e.clientX - rect.left);
          tipVisible = true;
          const setup = setupCanvas(id);
          if (!setup) return;
          drawLineChart(id, labels, series, options);
          redrawWithTip(mx);
        });
        canvas.addEventListener("mouseleave", function() {
          tipVisible = false;
          drawLineChart(id, labels, series, options);
        });
      }

      function drawBarChart(id, labels, series, suffix) {
        const setup = setupCanvas(id);
        if (!setup) return;
        const { ctx, width, height } = setup;
        const pad = { top: 14, right: 16, bottom: 34, left: 44 };
        const values = series.flatMap(function (s) { return clean(s.values).filter(function (v) { return v !== null; }); });
        if (labels.length === 0 || values.length === 0) {
          noData(ctx, width, height);
          return;
        }
        const maxVal = Math.max(1, ...values);
        drawGrid(ctx, width, height, pad, maxVal, suffix || "");
        const chartW = width - pad.left - pad.right;
        const chartH = height - pad.top - pad.bottom;
        const groupW = chartW / labels.length;
        const barW = Math.max(5, groupW / (series.length + 1) - 4);
        series.forEach(function (s, si) {
          ctx.fillStyle = s.color;
          clean(s.values).forEach(function (value, i) {
            if (value === null) return;
            const x = pad.left + i * groupW + si * (barW + 4) + (groupW - series.length * (barW + 4)) / 2;
            const barH = (value / maxVal) * chartH;
            ctx.fillRect(x, pad.top + chartH - barH, barW, Math.max(1, barH));
          });
        });
        ctx.fillStyle = "#6A7A99";
        ctx.font = "11px system-ui, sans-serif";
        ctx.textAlign = "center";
        labels.forEach(function (label, i) {
          ctx.fillText(label, pad.left + i * groupW + groupW / 2, height - 10);
        });
        ctx.textAlign = "left";
      }

      function setupBarTooltip(id, labels, series, suffix) {
        const canvas = document.getElementById(id);
        if (!canvas) return;
        const pad = { top: 14, right: 16, bottom: 34, left: 44 };

        function redrawWithTip(mx) {
          const rect = canvas.getBoundingClientRect();
          const width = rect.width || 600;
          const height = rect.height || 220;
          const chartW = width - pad.left - pad.right;
          const chartH = height - pad.top - pad.bottom;
          const values = series.flatMap(function (s) { return clean(s.values).filter(function (v) { return v !== null; }); });
          if (labels.length === 0 || values.length === 0 || mx < pad.left || mx > width - pad.right) return;
          const groupW = chartW / labels.length;
          const idx = Math.floor((mx - pad.left) / groupW);
          if (idx < 0 || idx >= labels.length) return;
          const x = pad.left + idx * groupW + groupW / 2;
          const ctx = canvas.getContext("2d");
          ctx.save();
          ctx.fillStyle = "rgba(255,255,255,0.04)";
          ctx.fillRect(pad.left + idx * groupW, pad.top, groupW, chartH);
          ctx.strokeStyle = "rgba(255,255,255,0.15)";
          ctx.lineWidth = 1;
          ctx.setLineDash([3, 3]);
          ctx.beginPath();
          ctx.moveTo(x, pad.top);
          ctx.lineTo(x, height - pad.bottom);
          ctx.stroke();
          ctx.setLineDash([]);
          const lines = [labels[idx]];
          series.forEach(function (s) {
            const v = s.values[idx];
            if (typeof v === "number" && isFinite(v)) {
              lines.push(s.label + ":  " + formatChartValue(v, suffix || ""));
            }
          });
          if (lines.length > 1) drawTooltipBox(ctx, lines, x, width, pad);
          ctx.restore();
        }

        canvas.addEventListener("mousemove", function(e) {
          const rect = canvas.getBoundingClientRect();
          drawBarChart(id, labels, series, suffix);
          redrawWithTip(e.clientX - rect.left);
        });
        canvas.addEventListener("mouseleave", function() {
          drawBarChart(id, labels, series, suffix);
        });
      }

      try {
        const data = JSON.parse(dataEl.textContent);
        const powerSeries = [
          { color: "#F5A82A", label: "PV", values: data.power.pv_w || [] },
          { color: "#EF6F6F", label: "Load", values: data.power.load_w || [] },
          { color: "#5B8DEF", label: "Grid", values: data.power.grid_w || [] }
        ];
        const socSeries = [
          { color: "#35C4A0", label: "SOC", values: data.soc.soc || [] }
        ];
        drawLineChart("power-trend-chart", data.power.labels || [], powerSeries, { suffix: "W", minMax: 1000 });
        setupLineTooltip("power-trend-chart", data.power.labels || [], powerSeries, { suffix: "W", minMax: 1000, modes: data.power.mode || [], batteryNet: data.power.battery_net_w || [] });
        drawLineChart("soc-trend-chart", data.soc.labels || [], socSeries, { suffix: "%", minMax: 100 });
        setupLineTooltip("soc-trend-chart", data.soc.labels || [], socSeries, { suffix: "%", minMax: 100 });
        const batteryEnergySeries = [
          { color: "#35C4A0", label: "Charge", values: data.daily.charge_kwh || [] },
          { color: "#6A7A99", label: "Discharge", values: data.daily.discharge_kwh || [] }
        ];
        const supplyEnergySeries = [
          { color: "#F5A82A", label: "PV", values: data.daily.pv_kwh || [] },
          { color: "#5B8DEF", label: "Grid", values: data.daily.grid_kwh || [] },
          { color: "#EF6F6F", label: "Load", values: data.daily.load_kwh || [] }
        ];
        drawBarChart("battery-energy-chart", data.daily.labels || [], batteryEnergySeries, "kWh");
        setupBarTooltip("battery-energy-chart", data.daily.labels || [], batteryEnergySeries, "kWh");
        drawBarChart("supply-energy-chart", data.daily.labels || [], supplyEnergySeries, "kWh");
        setupBarTooltip("supply-energy-chart", data.daily.labels || [], supplyEnergySeries, "kWh");
      } catch (e) { /* metric chart render failed */ }
    })();
    (function () {
      const badge = document.querySelector("[data-refresh-badge]");
      const ageNodes = Array.from(document.querySelectorAll("[data-refresh-age]"));
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
        ageNodes.forEach(function (node) {
          node.textContent = "Generated " + formatAge(ageMs) + " ago; stale after " + staleMinutes + " minutes.";
        });
      }

      updateRefreshHealth();
      window.setInterval(updateRefreshHealth, 30000);
    })();
    function setDashLayout(layout) {
      const html = document.documentElement;
      const btn = document.getElementById('layout-toggle-btn');
      const night = layout === 'night';
      html.classList.toggle('layout-night', night);
      try { localStorage.setItem('dash-view', night ? 'design' : 'operations'); } catch(e) {}
      if (btn) {
        btn.textContent = night ? 'Operations' : 'New design';
        btn.classList.toggle('layout-toggle-active', night);
      }
    }
    function toggleDashLayout() {
      setDashLayout(document.documentElement.classList.contains('layout-night') ? 'current' : 'night');
    }
    function toggleDashTheme() {
      const html = document.documentElement;
      const btn = document.getElementById('theme-toggle-btn');
      const isLight = html.classList.toggle('theme-light');
      try { localStorage.setItem('dash-theme', isLight ? 'light' : 'dark'); } catch(e) {}
      if (btn) btn.textContent = isLight ? 'Dark' : 'Light';
    }
    (function() {
      try {
        setDashLayout(localStorage.getItem('dash-view') === 'operations' ? 'current' : 'night');
        if (localStorage.getItem('dash-theme') === 'light') {
          document.documentElement.classList.add('theme-light');
          const btn = document.getElementById('theme-toggle-btn');
          if (btn) btn.textContent = 'Dark';
        }
      } catch(e) {}
    })();
