from __future__ import annotations

DASHBOARD_CSS = r'''
    :root {
      color-scheme: dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
      --bg: #0F1318;
      --surface: #161B24;
      --panel: #1D2330;
      --panel-2: #242D3E;
      --border: #2C3548;
      --border-strong: #3D4D6B;
      --ink: #CDD5E8;
      --muted: #6A7A99;
      --soft: #3D4D6B;
      --solar: #F5A82A;
      --battery: #35C4A0;
      --grid-c: #5B8DEF;
      --load-c: #EF6F6F;
      --accent: #5B8DEF;
      --accent-soft: #162040;
      --good: #3AC87A;
      --warn: #F5A82A;
      --crit: #EF5E5E;
      --radius: 10px;
    }
    .theme-light {
      color-scheme: light;
      --bg: #f1f5f9;
      --surface: #f8fafc;
      --panel: #ffffff;
      --panel-2: #f1f5f9;
      --border: #e2e8f0;
      --border-strong: #cbd5e1;
      --ink: #111827;
      --muted: #6b7280;
      --soft: #9ca3af;
      --solar: #b45309;
      --battery: #047857;
      --grid-c: #1d4ed8;
      --load-c: #b91c1c;
      --accent: #2563eb;
      --accent-soft: #eff6ff;
      --good: #047857;
      --warn: #b45309;
      --crit: #b91c1c;
    }
    .theme-light .badge-ok { background: #ecfdf5; color: #065f46; border-color: #6ee7b7; }
    .theme-light .badge-warn { background: #fffbeb; color: #92400e; border-color: #fcd34d; }
    .theme-light .badge-fail { background: #fef2f2; color: #991b1b; border-color: #fca5a5; }
    .theme-light .flow-tile { background: var(--panel-2); }
    .theme-light .mix-panel { background: var(--panel-2); }
    .theme-light th { background: var(--panel-2); }
    .theme-light .flow-stage, .theme-light .card, .theme-light .detail-panel,
    .theme-light .flow-tile, .theme-light .mix-panel, .theme-light .planner-card, .theme-light .reserve-details {
      box-shadow: 0 1px 3px rgba(0,0,0,0.1), 0 0 0 1px rgba(0,0,0,0.06);
    }
    .theme-toggle {
      cursor: pointer;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: var(--panel);
      color: var(--muted);
      padding: 6px 12px;
      font-size: 12px;
      font-weight: 680;
      font-family: inherit;
      min-height: 32px;
      white-space: nowrap;
    }
    .theme-toggle:hover { color: var(--ink); border-color: var(--border-strong); }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      margin: 0;
      background: var(--surface);
      color: var(--ink);
      font-size: 14px;
      line-height: 1.45;
    }
    .app-shell {
      display: grid;
      grid-template-columns: 248px minmax(0, 1fr);
      min-height: 100vh;
    }
    .sidebar {
      position: sticky;
      top: 0;
      height: 100vh;
      display: flex;
      flex-direction: column;
      padding: 24px 18px;
      background: var(--bg);
      border-right: 1px solid var(--border);
    }
    .sidebar-brand { display: flex; align-items: center; gap: 12px; margin-bottom: 36px; }
    .sidebar-title { font-weight: 760; font-size: 16px; color: var(--ink); }
    .sidebar-nav { display: grid; gap: 4px; }
    .sidebar-nav a {
      display: flex;
      align-items: center;
      min-height: 38px;
      padding: 8px 10px;
      border-radius: 8px;
      color: var(--muted);
      text-decoration: none;
      font-weight: 620;
      font-size: 14px;
    }
    .sidebar-nav a:hover, .sidebar-nav a.active { background: var(--panel); color: var(--ink); }
    .sidebar-status {
      margin-top: auto;
      display: grid;
      gap: 12px;
      padding: 14px;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--panel);
    }
    main { max-width: 1360px; width: 100%; margin: 0 auto; padding: 28px 28px 44px; }
    h1 { font-size: clamp(24px, 3vw, 34px); line-height: 1.08; margin: 0; letter-spacing: 0; font-weight: 760; color: var(--ink); }
    h2 { font-size: 18px; line-height: 1.3; margin: 40px 0 0; letter-spacing: 0; font-weight: 720; color: var(--ink); }
    code { color: var(--muted); font-size: 12px; white-space: normal; overflow-wrap: anywhere; }
    .muted { color: var(--muted); font-size: 14px; }
    .small { font-size: 13px; margin-top: 8px; }
    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      margin-bottom: 22px;
    }
    .brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .brand-mark {
      width: 32px;
      height: 32px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--panel);
      position: relative;
      flex: 0 0 auto;
    }
    .brand-mark::after {
      content: "";
      position: absolute;
      inset: 10px;
      border-radius: 999px;
      background: var(--solar);
    }
    .brand-title { font-weight: 720; font-size: 16px; color: var(--ink); }
    .top-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 32px;
      padding: 6px 10px;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: var(--panel);
      color: var(--ink);
      font-size: 13px;
      font-weight: 620;
      white-space: nowrap;
    }
    .glance-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin: 0 0 12px;
    }
    .glance-card {
      min-width: 0;
      min-height: 164px;
      display: grid;
      gap: 10px;
      align-content: space-between;
      padding: 15px;
      border-radius: var(--radius);
      background: var(--panel);
      border: 1px solid var(--border);
      box-shadow: 0 1px 3px rgba(0,0,0,0.26), 0 0 0 1px rgba(255,255,255,0.04);
      position: relative;
      overflow: hidden;
    }
    .glance-card::before { content: ""; position: absolute; inset: 0 auto 0 0; width: 3px; background: var(--accent); }
    .glance-battery::before { background: var(--battery); }
    .glance-solar::before { background: var(--solar); }
    .glance-utility::before { background: var(--grid-c); }
    .glance-risk::before { background: var(--warn); }
    .glance-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; min-width: 0; }
    .glance-head > div { min-width: 0; }
    .glance-head .badge { flex: 0 0 auto; }
    .glance-card .label { color: var(--ink); }
    .glance-value {
      margin-top: 6px;
      font-size: clamp(26px, 2.6vw, 36px);
      line-height: 1;
      font-weight: 780;
      font-variant-numeric: tabular-nums;
      color: var(--ink);
      overflow-wrap: anywhere;
    }
    .glance-battery .glance-value { color: var(--battery); }
    .glance-solar .glance-value { color: var(--solar); }
    .glance-utility .glance-value { color: var(--grid-c); }
    .glance-risk .glance-value { color: var(--warn); font-size: clamp(20px, 2vw, 28px); line-height: 1.08; }
    .glance-detail { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.35; overflow-wrap: anywhere; }
    .glance-stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
    .glance-stats div { min-width: 0; padding: 8px 9px; border-radius: 8px; background: var(--panel-2); border: 1px solid var(--border); }
    .glance-stats .glance-primary-stat { border-color: var(--border-strong); background: rgba(91, 141, 239, 0.08); }
    .glance-stats span { display: block; color: var(--muted); font-size: 11px; font-weight: 720; letter-spacing: 0.06em; text-transform: uppercase; }
    .glance-stats strong { display: block; margin-top: 4px; color: var(--ink); font-size: 13px; line-height: 1.2; font-weight: 720; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
    .flow-stage, .card, .detail-panel {
      background: var(--panel);
      box-shadow: 0 1px 3px rgba(0,0,0,0.28), 0 0 0 1px rgba(255,255,255,0.05);
      border-radius: var(--radius);
    }
    table {
      background: var(--panel);
      border-radius: var(--radius);
    }
    .hero-kicker { color: var(--solar); font-size: 12px; font-weight: 720; text-transform: uppercase; letter-spacing: 0.07em; }
    .battery-overview {
      padding: 24px;
      display: grid;
      gap: 18px;
    }
    .reserve-badges { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
    .battery-stats span, .battery-outlook span { color: var(--muted); font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; }
    .battery-stats strong, .battery-outlook strong { color: var(--ink); font-size: 18px; line-height: 1.1; font-weight: 740; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
    .battery-stats em, .battery-outlook em { color: var(--muted); font-size: 12px; font-style: normal; line-height: 1.35; overflow-wrap: anywhere; }
    .rec-high { border-color: rgba(239, 94, 94, 0.34); }
    .rec-watch { border-color: rgba(245, 168, 42, 0.34); }
    .rec-good { border-color: rgba(58, 200, 122, 0.28); }
    .battery-panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
    .battery-command { grid-template-columns: 168px minmax(0, 1fr); gap: 18px; margin-top: 0; align-items: stretch; }
    .battery-stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .battery-stats div {
      min-width: 0;
      display: grid;
      gap: 4px;
      padding: 11px 12px;
      border-radius: 8px;
      background: var(--panel-2);
      border: 1px solid var(--border);
    }
    .battery-outlook {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 10px;
    }
    .battery-outlook div {
      min-width: 0;
      display: grid;
      gap: 4px;
      padding: 11px 12px;
      border-radius: 8px;
      background: rgba(91, 141, 239, 0.08);
      border: 1px solid rgba(91, 141, 239, 0.2);
    }
    .soc-command {
      display: grid;
      grid-template-columns: 176px minmax(0, 1fr);
      gap: 24px;
      align-items: center;
      margin-top: 24px;
    }
    .soc-command.battery-command {
      grid-template-columns: 168px minmax(0, 1fr);
      gap: 18px;
      align-items: stretch;
      margin-top: 0;
    }
    .soc-ring {
      width: min(176px, 52vw);
      aspect-ratio: 1;
      border-radius: 999px;
      display: grid;
      place-items: center;
      background:
        radial-gradient(circle at center, var(--panel) 0 57%, transparent 58%),
        conic-gradient(var(--battery) 0 var(--soc, 0%), rgba(53, 196, 160, 0.12) var(--soc, 0%) 100%);
      border: 1px solid var(--border);
      box-shadow: inset 0 0 0 10px rgba(53, 196, 160, 0.08), 0 10px 24px rgba(0,0,0,0.18);
    }
    .theme-light .soc-ring {
      background:
        radial-gradient(circle at center, var(--panel) 0 57%, transparent 58%),
        conic-gradient(var(--battery) 0 var(--soc, 0%), rgba(53, 196, 160, 0.16) var(--soc, 0%) 100%);
    }
    .soc-core { text-align: center; }
    .soc-core strong { display: block; font-size: clamp(40px, 6vw, 56px); line-height: 0.95; letter-spacing: 0; font-weight: 760; font-variant-numeric: tabular-nums; color: var(--ink); }
    .soc-core span { color: var(--muted); font-size: 12px; text-transform: uppercase; font-weight: 680; letter-spacing: 0.06em; }
    .mode-stack { display: grid; gap: 12px; min-width: 0; }
    .mode-line { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
    .mode-value { font-size: 24px; line-height: 1.15; font-weight: 720; overflow-wrap: anywhere; color: var(--ink); }
    .flow-stage { padding: 18px; margin-top: 16px; }
    .section-head, .flow-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; margin: 40px 0 16px; }
    .section-head h2, .flow-head h2 { margin: 0; }
    .flow-map {
      display: grid;
      grid-template-columns: minmax(110px, 1fr) 32px minmax(110px, 1fr) 32px minmax(110px, 1fr) 32px minmax(110px, 1fr) 32px minmax(110px, 1fr);
      column-gap: 0;
      row-gap: 10px;
      align-items: center;
    }
    .flow-chain {
      display: grid;
      grid-template-columns: 1fr;
      gap: 12px;
      padding: 12px;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--panel);
    }
    .flow-main-row {
      display: grid;
      grid-template-columns: minmax(180px, 1fr) 40px minmax(210px, 1.1fr) 40px minmax(180px, 1fr);
      align-items: stretch;
      gap: 0;
    }
    .flow-support-row {
      display: grid;
      grid-template-columns: repeat(2, minmax(180px, 1fr));
      gap: 12px;
      max-width: 620px;
      width: 100%;
      margin: 0 auto;
    }
    .flow-tile {
      min-height: 96px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.28), 0 0 0 1px rgba(255,255,255,0.05);
      border-radius: 10px;
      padding: 14px 16px;
      background: var(--panel-2);
      display: grid;
      align-content: space-between;
      position: relative;
    }
    .flow-tile::before { content: ""; position: absolute; inset: 0 auto 0 0; width: 3px; background: var(--accent); border-radius: 10px 0 0 10px; }
    .flow-tile.solar::before { background: var(--solar); }
    .flow-tile.battery::before { background: var(--battery); }
    .flow-tile.grid-source::before { background: var(--grid-c); }
    .flow-tile.load::before { background: var(--load-c); }
    .flow-tile.solar .flow-value { color: var(--solar); }
    .flow-tile.battery .flow-value { color: var(--battery); }
    .flow-tile.grid-source .flow-value { color: var(--grid-c); }
    .flow-tile.load .flow-value { color: var(--load-c); }
    .flow-tile.solar, .flow-tile.grid-source, .flow-tile.inverter, .flow-tile.battery, .flow-tile.load { grid-column: auto; grid-row: auto; }
    .flow-label { color: var(--muted); font-size: 12px; font-weight: 680; text-transform: uppercase; letter-spacing: 0.06em; }
    .flow-value { font-size: 22px; font-weight: 740; line-height: 1.05; margin-top: 6px; overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
    .flow-detail { color: var(--muted); font-size: 13px; margin-top: 8px; }
    .flow-chip { min-height: 84px; }
    @keyframes flow-stream {
      from { background-position-x: 0px; }
      to { background-position-x: 20px; }
    }
    .connector {
      display: flex;
      align-items: center;
      justify-content: center;
      position: relative;
      align-self: center;
      height: 20px;
      color: var(--border-strong);
      opacity: 0.5;
    }
    .connector.pv { color: var(--solar); }
    .connector.battery { color: var(--battery); }
    .connector.grid { color: var(--grid-c); }
    .connector.load { color: var(--load-c); }
    .connector.active { opacity: 1; }
    .connector::before {
      content: "";
      position: absolute;
      left: 2px; right: 12px; top: 50%;
      height: 2px;
      transform: translateY(-50%);
      background: repeating-linear-gradient(
        90deg,
        currentColor 0px, currentColor 10px,
        transparent 10px, transparent 18px
      );
      animation: flow-stream 0.6s linear infinite;
    }
    .connector:not(.active)::before { animation: none; }
    .connector.reverse::before { left: 12px; right: 2px; animation-direction: reverse; }
    .connector::after {
      content: "";
      position: absolute;
      right: 2px; top: 50%;
      transform: translateY(-50%);
      border-top: 5px solid transparent;
      border-bottom: 5px solid transparent;
      border-left: 10px solid currentColor;
    }
    .connector.reverse::after {
      right: auto;
      left: 2px;
      border-left: 0;
      border-right: 10px solid currentColor;
    }
    @media (prefers-reduced-motion: reduce) {
      .connector::before { animation: none; }
    }
    .energy-map {
      position: relative;
      min-height: 320px;
      display: block;
      overflow: hidden;
      border-radius: 12px;
      background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0));
      border: 1px solid var(--border);
    }
    .energy-lines {
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      pointer-events: none;
    }
    .energy-line {
      fill: none;
      stroke: var(--border-strong);
      stroke-width: 1.4;
      stroke-linecap: round;
      opacity: 0.3;
      stroke-dasharray: 1 7;
    }
    .energy-line.active {
      opacity: 0.95;
      stroke-dasharray: 7 8;
      animation: energy-flow 1.2s linear infinite;
    }
    .energy-line.reverse { animation-direction: reverse; }
    .solar-line { stroke: var(--solar); }
    .battery-line { stroke: var(--battery); }
    .grid-line { stroke: var(--grid-c); }
    .load-line { stroke: var(--load-c); }
    @keyframes energy-flow {
      to { stroke-dashoffset: -30; }
    }
    .energy-node {
      position: absolute;
      width: min(205px, 34%);
      min-height: 98px;
      transition: transform 160ms ease, border-color 160ms ease, box-shadow 160ms ease;
      z-index: 1;
    }
    .energy-node:hover {
      transform: translate3d(0, -2px, 0);
      border-color: var(--border-strong);
      box-shadow: 0 8px 22px rgba(0,0,0,0.22), 0 0 0 1px rgba(255,255,255,0.07);
    }
    .energy-node.solar { top: 12px; left: 50%; transform: translateX(-50%); }
    .energy-node.inverter { top: 50%; left: 50%; transform: translate(-50%, -50%); }
    .energy-node.battery { bottom: 12px; left: 50%; transform: translateX(-50%); }
    .energy-node.grid-source { top: 50%; left: 12px; transform: translateY(-50%); }
    .energy-node.load { top: 50%; right: 12px; transform: translateY(-50%); }
    .energy-node.solar:hover { transform: translate(-50%, -2px); }
    .energy-node.inverter:hover { transform: translate(-50%, calc(-50% - 2px)); }
    .energy-node.battery:hover { transform: translate(-50%, -2px); }
    .energy-node.grid-source:hover, .energy-node.load:hover { transform: translateY(calc(-50% - 2px)); }
    @media (prefers-reduced-motion: reduce) {
      .energy-line.active { animation: none; }
      .energy-node { transition: none; }
    }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-top: 16px; }
    .daily-grid { grid-template-columns: repeat(auto-fit, minmax(224px, 1fr)); }
    .daily-mix { display: grid; gap: 16px; margin-top: 16px; }
    .mix-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
    .mix-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
    .mix-panel {
      min-width: 0;
      padding: 14px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.22), 0 0 0 1px rgba(255,255,255,0.04);
      border-radius: 10px;
      background: var(--panel-2);
    }
    .mix-row-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
    .mix-row-head strong { font-size: 15px; font-weight: 720; color: var(--ink); }
    .mix-row-head span { color: var(--muted); font-size: 13px; font-weight: 640; white-space: nowrap; font-variant-numeric: tabular-nums; }
    .mix-bar { display: flex; height: 8px; margin: 14px 0 12px; overflow: hidden; border-radius: 999px; background: var(--border); }
    .mix-segment { display: block; height: 100%; }
    .mix-segment.primary { background: var(--solar); }
    .mix-segment.neutral { background: var(--grid-c); opacity: 0.7; }
    .mix-legend { display: grid; gap: 8px; }
    .mix-legend div { display: flex; justify-content: space-between; gap: 12px; color: var(--muted); font-size: 12px; }
    .mix-legend strong { color: var(--ink); font-weight: 680; text-align: right; font-variant-numeric: tabular-nums; }
    .ops-grid { grid-template-columns: repeat(auto-fit, minmax(216px, 1fr)); }
    .insight-grid { grid-template-columns: repeat(auto-fit, minmax(232px, 1fr)); }
    .status-activity-grid { display: grid; grid-template-columns: minmax(280px, 0.9fr) minmax(320px, 1.1fr); gap: 12px; margin-top: 12px; }
    .card { padding: 16px; }
    .metric-card { min-height: 148px; display: grid; align-content: space-between; gap: 12px; }
    .insight-card { min-height: 120px; display: grid; align-content: space-between; gap: 8px; }
    .insight-card .muted.small { font-size: 13px; line-height: 1.5; }
    .metric-head { display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; }
    .metric-meter { height: 6px; border-radius: 999px; background: var(--border); overflow: hidden; }
    .metric-meter span { display: block; height: 100%; max-width: 100%; background: var(--accent); border-radius: inherit; }
    .accent-pv { border-top: 2px solid var(--solar); }
    .accent-grid { border-top: 2px solid var(--grid-c); }
    .accent-load { border-top: 2px solid var(--load-c); }
    .accent-battery { border-top: 2px solid var(--battery); }
    .accent-pv .metric-meter span { background: var(--solar); }
    .accent-grid .metric-meter span { background: var(--grid-c); }
    .accent-load .metric-meter span { background: var(--load-c); }
    .accent-battery .metric-meter span { background: var(--battery); }
    .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; font-weight: 680; }
    .value { font-size: 24px; font-weight: 740; margin-top: 8px; line-height: 1.08; overflow-wrap: anywhere; font-variant-numeric: tabular-nums; color: var(--ink); }
    .badge {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 5px 9px;
      font-size: 12px;
      font-weight: 680;
      line-height: 1;
      border: 1px solid transparent;
    }
    .badge-ok { background: rgba(58, 200, 122, 0.12); color: #3AC87A; border-color: rgba(58, 200, 122, 0.3); }
    .badge-warn { background: rgba(245, 168, 42, 0.12); color: var(--warn); border-color: rgba(245, 168, 42, 0.3); }
    .badge-fail { background: rgba(239, 94, 94, 0.12); color: #EF5E5E; border-color: rgba(239, 94, 94, 0.3); }
    .badge-neutral { background: rgba(106, 122, 153, 0.12); color: var(--muted); border-color: rgba(106, 122, 153, 0.25); }
    .rec-section { padding: 20px 24px; display: grid; gap: 12px; }
    .rec-item {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: 12px;
      align-items: flex-start;
      padding: 12px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--panel-2);
      font-size: 14px;
      line-height: 1.5;
      transition: transform 160ms ease, border-color 160ms ease;
    }
    .rec-item:hover { transform: translateY(-1px); border-color: var(--border-strong); }
    .rec-icon {
      display: grid;
      place-items: center;
      min-width: 42px;
      height: 34px;
      border-radius: 8px;
      background: var(--panel);
      color: var(--muted);
      font-size: 11px;
      font-weight: 780;
      margin-top: 1px;
    }
    .rec-item strong { display: block; color: var(--ink); font-size: 14px; line-height: 1.25; margin-bottom: 2px; }
    .rec-item em { display: block; margin-top: 4px; color: var(--muted); font-size: 12px; font-style: normal; }
    .planner-grid { display: grid; grid-template-columns: minmax(260px, 0.9fr) repeat(3, minmax(160px, 1fr)); gap: 12px; margin-top: 16px; }
    .planner-card { min-height: 124px; padding: 16px; background: var(--panel); box-shadow: 0 1px 3px rgba(0,0,0,0.22), 0 0 0 1px rgba(255,255,255,0.04); border-radius: var(--radius); }
    .planner-card.primary { background: var(--panel-2); color: var(--ink); border-color: var(--border-strong); }
    .planner-card.primary .muted, .planner-card.primary .label { color: var(--muted); }
    .banner-warn { background: rgba(245, 168, 42, 0.08); color: var(--ink); border: 1px solid rgba(245, 168, 42, 0.3); border-radius: var(--radius); padding: 12px 16px; margin: 16px 0 24px; font-weight: 620; }
    .chart-grid { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(320px, .9fr); gap: 12px; }
    .today-charts { margin-top: 16px; }
    .chart-card canvas { width: 100%; height: 280px; display: block; }
    .chart-card.compact canvas { height: 220px; }
    .legend { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 12px; color: var(--muted); font-size: 13px; }
    .legend span::before { content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: -1px; background: var(--c); }
    .table-wrap { overflow-x: auto; border-radius: var(--radius); border: 1px solid var(--border); background: var(--panel); margin-top: 12px; }
    table { width: 100%; border-collapse: collapse; box-shadow: none; border: 0; min-width: 640px; }
    th, td { padding: 12px 14px; border-bottom: 1px solid var(--border); text-align: left; font-size: 14px; vertical-align: top; color: var(--ink); }
    th { background: var(--panel-2); color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; font-weight: 680; }
    tr:last-child td { border-bottom: 0; }
    .status-ok { color: var(--ink); font-weight: 680; }
    .status-skip { color: var(--ink); font-weight: 680; }
    .status-replace { color: var(--ink); font-weight: 680; }
    .details-stack { display: grid; gap: 10px; margin-top: 16px; }
    .detail-panel { padding: 0; overflow: hidden; }
    .detail-panel summary {
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 16px;
      font-weight: 680;
      color: var(--ink);
      list-style: none;
    }
    .detail-panel summary::-webkit-details-marker { display: none; }
    .detail-panel summary::after { content: "+"; color: var(--soft); font-weight: 720; }
    .detail-panel[open] summary { border-bottom: 1px solid var(--border); }
    .detail-panel[open] summary::after { content: "-"; }
    .detail-panel .table-wrap { border: 0; border-radius: 0; margin-top: 0; }
    .detail-panel .card { border: 0; border-radius: 0; }
    .summary-meta { color: var(--muted); font-size: 12px; font-weight: 560; white-space: nowrap; }
    .summary-copy { display: block; margin-top: 3px; color: var(--muted); font-size: 13px; font-weight: 520; line-height: 1.35; text-transform: none; letter-spacing: 0; }
    .reserve-details { margin: 0 0 16px; background: var(--panel); border: 1px solid var(--border); }
    .reserve-details summary { align-items: center; }
    .reserve-details summary > span:first-child { min-width: 0; }
    .reserve-details .reserve-badges { flex: 1 1 auto; margin-left: auto; padding-right: 6px; }
    .reserve-body { padding: 14px; }
    .reserve-details .battery-stats { margin: 0; }
    .status-list { display: grid; gap: 10px; margin-top: 14px; }
    .status-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 10px 0; border-bottom: 1px solid var(--border); }
    .status-row:last-child { border-bottom: 0; padding-bottom: 0; }
    .activity-list { list-style: none; padding: 0; margin: 14px 0 0; display: grid; gap: 10px; }
    .activity-item { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 10px 0; border-bottom: 1px solid var(--border); }
    .activity-item:last-child { border-bottom: 0; padding-bottom: 0; }
    .activity-item strong { display: block; font-size: 14px; font-weight: 680; color: var(--ink); }
    .activity-item span { display: block; margin-top: 3px; color: var(--muted); font-size: 12px; }
    .timeline-card { margin-top: 12px; }
    .timeline-list { list-style: none; padding: 0; margin: 14px 0 0; display: grid; gap: 0; }
    .timeline-item {
      position: relative;
      display: grid;
      grid-template-columns: 18px minmax(0, 1fr) auto;
      gap: 12px;
      align-items: start;
      padding: 0 0 16px;
    }
    .timeline-item::before {
      content: "";
      position: absolute;
      left: 5px;
      top: 16px;
      bottom: 0;
      width: 1px;
      background: var(--border);
    }
    .timeline-item:last-child { padding-bottom: 0; }
    .timeline-item:last-child::before { display: none; }
    .timeline-marker { width: 11px; height: 11px; margin-top: 4px; border-radius: 999px; border: 2px solid var(--accent); background: var(--panel); }
    .timeline-passed .timeline-marker, .timeline-skipped .timeline-marker, .timeline-replaced .timeline-marker { border-color: var(--border-strong); }
    .timeline-main { min-width: 0; }
    .timeline-main strong { display: block; font-size: 14px; font-weight: 700; overflow-wrap: anywhere; color: var(--ink); }
    .timeline-main span { display: block; margin-top: 3px; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
    .dashboard-night { display: none; }
    .layout-night .dashboard-current { display: none; }
    .layout-night .dashboard-night { display: block; }
    .layout-toggle-active { color: var(--ink); border-color: var(--border-strong); }
    .night-console { display: grid; gap: 16px; }
    .night-context-strip { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; margin-bottom: 2px; }
    .night-hero-grid { display: grid; grid-template-columns: minmax(280px, 0.9fr) minmax(320px, 1fr) minmax(280px, 0.9fr); gap: 12px; align-items: stretch; }
    .night-panel, .night-flow, .night-totals { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: 0 1px 3px rgba(0,0,0,0.28), 0 0 0 1px rgba(255,255,255,0.05); }
    .night-panel { min-width: 0; padding: 18px; display: grid; gap: 16px; align-content: start; }
    .night-panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
    .night-panel-title { margin-top: 6px; font-size: clamp(28px, 3vw, 40px); line-height: 1; font-weight: 780; font-variant-numeric: tabular-nums; color: var(--ink); overflow-wrap: anywhere; }
    .night-battery { border-top: 2px solid var(--battery); }
    .night-solar { border-top: 2px solid var(--solar); }
    .night-risk { border-top: 2px solid var(--warn); }
    .night-battery-main { display: grid; grid-template-columns: 150px minmax(0, 1fr); gap: 14px; align-items: center; }
    .night-soc-ring { width: min(150px, 100%); }
    .night-metric-stack, .night-subgrid, .night-solar-grid, .night-risk-score { display: grid; gap: 10px; }
    .night-metric-stack div, .night-subgrid div, .night-solar-grid div, .night-risk-score div, .night-next, .night-total-item { min-width: 0; padding: 11px 12px; border-radius: 8px; background: var(--panel-2); border: 1px solid var(--border); }
    .night-metric-stack span, .night-subgrid span, .night-solar-grid span, .night-risk-score span, .night-next span, .night-total-item span, .night-flow-node span { color: var(--muted); font-size: 11px; font-weight: 720; letter-spacing: 0.06em; text-transform: uppercase; }
    .night-metric-stack strong, .night-subgrid strong, .night-solar-grid strong, .night-risk-score strong, .night-next strong, .night-total-item strong, .night-flow-node strong { display: block; margin-top: 4px; color: var(--ink); font-size: 18px; line-height: 1.1; font-weight: 740; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
    .night-metric-stack em, .night-solar-grid em, .night-next em, .night-total-item em, .night-flow-node em { display: block; margin-top: 5px; color: var(--muted); font-size: 12px; line-height: 1.35; font-style: normal; overflow-wrap: anywhere; }
    .night-subgrid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .night-solar-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .night-primary-stat { grid-column: 1 / -1; }
    .night-primary-stat strong { color: var(--solar); font-size: 30px; }
    .night-risk-score { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .night-risk-note { color: var(--muted); font-size: 13px; line-height: 1.5; }
    .night-spark { display: flex; align-items: end; gap: 8px; height: 72px; padding: 12px; border-radius: 8px; background: rgba(245, 168, 42, 0.08); border: 1px solid rgba(245, 168, 42, 0.2); }
    .night-spark span { flex: 1; min-width: 10px; border-radius: 999px 999px 2px 2px; background: var(--solar); opacity: 0.82; }
    .night-flow { display: grid; grid-template-columns: minmax(140px, 1fr) 28px minmax(160px, 1fr) 28px minmax(140px, 1fr) minmax(140px, 1fr) minmax(140px, 1fr); gap: 10px; align-items: stretch; padding: 14px; }
    .night-flow-node { min-width: 0; padding: 12px; border-radius: 8px; background: var(--panel-2); border: 1px solid var(--border); }
    .night-flow-node.solar strong { color: var(--solar); }
    .night-flow-node.battery strong { color: var(--battery); }
    .night-flow-node.grid-source strong { color: var(--grid-c); }
    .night-flow-node.load strong { color: var(--load-c); }
    .night-flow-arrow { align-self: center; justify-self: center; color: var(--border-strong); font-weight: 760; }
    .night-totals { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; padding: 14px; }
    @media (max-width: 1180px) {
      .night-hero-grid { grid-template-columns: 1fr; }
      .night-flow { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .night-flow-arrow { display: none; }
      .night-totals { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .glance-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 1040px) {
      .app-shell { grid-template-columns: 1fr; }
      .sidebar {
        position: static;
        height: auto;
        border-right: 0;
        border-bottom: 1px solid var(--border);
      }
      .sidebar-brand { margin-bottom: 18px; }
      .sidebar-nav { grid-template-columns: repeat(auto-fit, minmax(128px, 1fr)); }
      .sidebar-status { margin-top: 18px; }
      .glance-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .chart-grid, .planner-grid, .status-activity-grid, .mix-grid { grid-template-columns: 1fr; }
      .flow-map { grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); column-gap: 10px; }
      .flow-chain { grid-template-columns: 1fr; }
      .flow-main-row { grid-template-columns: repeat(3, minmax(160px, 1fr)); gap: 10px; }
      .flow-support-row { max-width: none; }
      .connector { display: none; }
      .energy-map { min-height: 420px; }
      .energy-node { width: min(220px, 42%); }
      .energy-node.grid-source { left: 12px; }
      .energy-node.load { right: 12px; }
    }
    @media (max-width: 720px) {
      .sidebar { padding: 18px 14px; }
      main { padding: 20px 14px 36px; }
      .topbar, .section-head, .flow-head { align-items: flex-start; flex-direction: column; }
      .top-actions { justify-content: flex-start; }
      .battery-overview, .flow-stage { padding: 16px; }
      .reserve-body { padding: 12px; }
      .reserve-details summary { align-items: flex-start; flex-direction: column; }
      .reserve-details .reserve-badges { margin-left: 0; padding-right: 0; justify-content: flex-start; }
      .battery-panel-head { flex-direction: column; }
      .reserve-badges { justify-content: flex-start; }
      .glance-grid, .glance-stats, .battery-stats, .battery-outlook, .flow-main-row, .flow-support-row { grid-template-columns: 1fr; }
      .energy-map { min-height: auto; display: grid; gap: 10px; padding: 0; border: 0; background: transparent; }
      .energy-lines { display: none; }
      .energy-node, .energy-node.solar, .energy-node.inverter, .energy-node.battery, .energy-node.grid-source, .energy-node.load {
        position: relative;
        inset: auto;
        width: 100%;
        transform: none;
      }
      .energy-node:hover, .energy-node.solar:hover, .energy-node.inverter:hover, .energy-node.battery:hover, .energy-node.grid-source:hover, .energy-node.load:hover {
        transform: none;
      }
      .soc-command { grid-template-columns: 1fr; gap: 16px; margin-top: 18px; }
      .soc-command.battery-command { grid-template-columns: 1fr; gap: 16px; margin-top: 0; }
      .soc-ring { width: min(220px, 100%); max-width: 220px; height: auto; min-height: 0; aspect-ratio: 1; justify-self: center; }
      .mode-stack { gap: 9px; }
      .mode-value { font-size: 20px; }
      table { min-width: 560px; }
      .night-context-strip { justify-content: flex-start; }
      .night-battery-main, .night-subgrid, .night-solar-grid, .night-risk-score, .night-flow, .night-totals { grid-template-columns: 1fr; }
    }
'''

DASHBOARD_JS = r'''
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
      try { localStorage.setItem('dash-layout', night ? 'night' : 'current'); } catch(e) {}
      if (btn) {
        btn.textContent = night ? 'Dashboard' : 'Night ops';
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
        setDashLayout(localStorage.getItem('dash-layout') === 'night' ? 'night' : 'current');
        if (localStorage.getItem('dash-theme') === 'light') {
          document.documentElement.classList.add('theme-light');
          const btn = document.getElementById('theme-toggle-btn');
          if (btn) btn.textContent = 'Dark';
        }
      } catch(e) {}
    })();
'''

