# Thermo-Nuclear Maintainability Audit

This ledger tracks structural code-quality work separately from product features in `ROADMAP.md`. The review applies to the entire `main` branch rather than one pull-request diff.

## Status Legend

- `OPEN`: confirmed issue; no implementation started.
- `IN PROGRESS`: implementation is actively being changed.
- `BLOCKED`: a prerequisite or design decision prevents safe progress.
- `VERIFY`: implementation is complete but full local verification is pending.
- `DONE`: implementation and required offline verification are complete.
- `ACCEPTED`: consciously retained with a documented reason.

## Verification Standard

Every completed production-code item must pass the commands in `AGENTS.md`. Live Growatt mode-changing commands are explicitly outside this audit workflow.

## Issue Ledger

| ID | Severity | Status | Area | Issue | Intended remedy |
|---|---|---|---|---|---|
| CQ-001 | Critical | DONE | Mode transitions | Automatic Utility entry could update the inverter before durable ownership state existed. | Auto-topup and preserve paths now persist recoverable intent before the cloud write. Local persistence failures roll back before any inverter call; ambiguous cloud outcomes retain intent; explicit SBU verification clears it. |
| CQ-002 | High | DONE | State model | `topup_active.json` and utility-hold state jointly described one operation. | Added typed `UtilityHold` with explicit `soc`/`time` completion policy. New auto and Discord top-ups write only `utility_hold.json`; legacy top-up files normalize at the read boundary and remain supported without new writes. |
| CQ-003 | High | DONE | Modes | `modes.py` mixed core mode control, top-up, alerts, summaries, and maintenance in 1,778 lines. | Split canonical owners into `modes.py` (740 lines), `topup.py` (655), `alerts.py` (333), and `reports.py` (131); the public shim imports each owner directly. |
| CQ-004 | High | DONE | Top-up completion | Completion interleaved ownership variants, file-shape legacy detection, duplicate parsing, telemetry reads, and SBU cleanup. | Canonical holds normalize owned/adopted and legacy inputs into explicit SOC/time policies. Shared helpers now own telemetry, parsing, elapsed time, and failure-safe SBU cleanup. |
| CQ-005 | High | DONE | Dashboard | JSON and HTML paths independently extracted metrics and recomputed policy during one refresh. | Production refresh now builds one dashboard payload and passes it to HTML rendering; direct renderer fallback remains only for compatibility/tests. |
| CQ-006 | High | DONE | Dashboard | `dashboard.py` owned extraction, history, projections, recommendations, rendering, refresh, alerts, and serving. | Decomposed into metrics, insights, planning, view-model, render-components, service, assets, and the remaining cohesive HTML renderer. |
| CQ-007 | Medium | DONE | Dashboard contracts | Dashboard helpers exchanged unbounded ad-hoc dictionaries. | Added the explicit `DashboardMetrics` typed record at the normalized telemetry boundary and focused JSON-shaped models by module ownership. |
| CQ-008 | Medium | DONE | Dashboard dead paths | `_build_dashboard_recommendations_legacy` preserved a second recommendation implementation without an active production caller. | Deleted the unreferenced implementation; all required offline checks pass. |
| CQ-009 | Medium | DONE | Dashboard assets | CSS/JavaScript lived in a 1,233-line Python string module. | Moved CSS and JavaScript to packaged asset files loaded through an 11-line deterministic resource boundary. |
| CQ-010 | Medium | DONE | Package boundary | Feature modules dynamically imported or discovered `growatt_power_guard`, reversing the intended dependency on the thin public shim. | Feature modules now import canonical owners directly. Only `cli.py` retains the lookup as the current composition seam for the `cli`/`modes` cycle; removing that cycle belongs to CQ-003. |
| CQ-011 | Medium | DONE | Scheduling | Execution, preserve expiry, calendar, JSON preview, and terminal preview reconstructed override semantics separately. | Added `EffectiveScheduleJob` and one resolver/daily collection. All listed paths consume it, and terminal preview renders from the JSON payload. |
| CQ-012 | Medium | DONE | Scheduling | `schedule.py` combined validation, lint, presentation, and override mutation in 1,022 lines. | Split canonical core (571 lines), `schedule_views.py` (235), and `schedule_overrides.py` (255), with CLI/shim imports pointing to each owner. |
| CQ-013 | Medium | DONE | Growatt boundary | `growatt_api.py` exceeded 1,000 lines and mixed recursive parsing with cloud orchestration. | Extracted pure telemetry normalization, estimates, and bypass detection to `growatt_telemetry.py`; transport/session/write orchestration is now 747 lines. |
| CQ-014 | Medium | ACCEPTED | State storage | State has domain-named wrappers over a shared JSON core. | Retained explicit patchable filenames and domain wrappers: `read_json_state`/`write_json_state`/`clear_state_file` already centralize metadata and atomicity, while `UtilityHold` owns the safety-critical typed model. A generic store object would be thin indirection and weaken test/configuration seams. |
| CQ-015 | Medium | ACCEPTED | Command effects | Commands repeat audit/notification/print/state effects in safety-sensitive branches. | Domain orchestration was decomposed into modes/topup/alerts/reports and transition helpers centralize ordering. A universal effect applicator was rejected because command-specific intent-before-cloud-write, verification, and cleanup order are safety invariants, not interchangeable effects. |
| CQ-016 | Medium | ACCEPTED | Error handling | Broad catches exist at network, daemon, diagnostic, and cleanup boundaries. | Audited all broad handlers: command failures re-raise after audit, daemon/health/diagnostic boundaries report explicit unavailable states, and best-effort weather/dashboard paths preserve automation. Retaining boundary catches is intentional; no policy decision silently treats unavailable telemetry as confirmed state. |
| CQ-017 | Medium | DONE | Configuration boundary | Core policy paths used `getattr(..., default)` on configs, allowing missing fields to change policy. | Core modes, Growatt session, dashboard service, PVOutput, and weather paths now require loaded `Config` fields directly; compatibility reflection remains only for external objects, argparse variants, and diagnostic presentation. |
| CQ-018 | Low | DONE | Test organization | Dashboard and Growatt API tests exceeded 1,000 lines. | Split dashboard policy tests and Growatt top-up/solar/history tests along the new production seams; every test module is now below 1,000 lines. |

## Work Log

### 2026-07-11

- Completed the initial whole-branch audit inventory.
- Baseline verification passed: compilation, 500 unit tests, schedule validation, schedule lint, and whitespace validation.
- Started CQ-010 with the `schedule.py` exception boundary.
- Completed CQ-008 by deleting 52 lines of unreferenced legacy recommendation policy.
- Re-ran all required checks successfully after both changes.
- Completed CQ-010 across schedule, config, PVOutput, weather, Growatt API, and dashboard; 500 tests still pass.
- Completed CQ-001 intent-before-effect ordering with rollback tests; the full suite now has 502 passing tests.
- Completed CQ-002 canonical hold consolidation and legacy migration; 505 tests and every required check pass.
- Completed CQ-004 by normalizing completion policies and centralizing parsing, telemetry, elapsed-time, and SBU cleanup effects; all gates pass.
- Completed CQ-003 by decomposing the 1,778-line command module into four focused owners; 505 tests and all gates pass.
- Completed CQ-011/CQ-012 with one effective-job model and focused core, view, and override modules; all gates pass.
- Completed dashboard decomposition (metrics, typed view-model inputs, insights, planning, components, service, and packaged assets), Growatt telemetry extraction, core config-boundary cleanup, and test-suite decomposition; 505 tests pass.
- Final gate passed: Python compilation, 505 offline unit tests, schedule validation/lint, wheel packaging with dashboard assets, public-value scan, and `git diff --check`. No live Growatt mode-changing command was run.
