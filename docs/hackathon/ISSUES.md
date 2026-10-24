# Issue board (Incident Zero)

Each entry is one unit of work, one branch and one PR. The GitHub issue holds the
owner, reviewer and the full spec; this table is the index.

Branch: `<type>/<NN>-<slug>`, where NN is the two-digit number in the table.
Feature specs are in [FEATURES.md](FEATURES.md).

| NN | ID | Title | Module | Priority | Depends on | GitHub # | Status |
|---|---|---|---|---|---|---|---|
| 00 | F00 | CI, AGENTS.md override and feature spec | repo setup | P0 | none | PR #1 | merged |
| 01 | F01 | Package skeleton, contracts, config, tool registry | brain | P0 | F00 | [#2](https://github.com/rhrishi-99/opentelemetry-demo/issues/2) | merged |
| 02 | F02 | Run log | brain | P0 | F01 | [#3](https://github.com/rhrishi-99/opentelemetry-demo/issues/3) | merged (PR #14) |
| 03 | F03 | Telemetry backends: live + offline (record & replay) | tools | P0 | F01 | [#4](https://github.com/rhrishi-99/opentelemetry-demo/issues/4) | merged |
| 04 | F04 | Tool: get_metrics | tools | P0 | F01, F03 | [#5](https://github.com/rhrishi-99/opentelemetry-demo/issues/5) | merged |
| 05 | F05 | Tool: search_traces | tools | P0 | F01, F03 | [#6](https://github.com/rhrishi-99/opentelemetry-demo/issues/6) | merged |
| 06 | F06 | Tool: search_logs | tools | P0 | F01, F03 | [#7](https://github.com/rhrishi-99/opentelemetry-demo/issues/7) | merged (PR #24, #36) |
| 07 | F07 | Tool: container_health (read-only Docker) | platform | P0 | F01 | [#8](https://github.com/rhrishi-99/opentelemetry-demo/issues/8) | merged |
| 08 | F08 | Tool: deploy_history | platform | P1 | F01 | [#9](https://github.com/rhrishi-99/opentelemetry-demo/issues/9) | merged |
| 09 | F09 | Investigation loop (Eyes) + LLM client + CLI | brain | P0 | F01, F02 | [#10](https://github.com/rhrishi-99/opentelemetry-demo/issues/10) | merged |
| 11 | F11 | Scenario library: record practice incidents as fixtures | platform/eval | P0 | F03 | [#17](https://github.com/rhrishi-99/opentelemetry-demo/issues/17) | merged |
| 12 | F12 | Diagnosis step + taxonomy | brain | P0 | F09, F02 | [#18](https://github.com/rhrishi-99/opentelemetry-demo/issues/18) | merged |
| 13 | F13 | Evidence validator | brain | P0 | F02, F12* | [#19](https://github.com/rhrishi-99/opentelemetry-demo/issues/19) | merged |
| 14 | F14 | Alert detector (minimal) | platform | P0 | F03, F04, F07, F09 | [#20](https://github.com/rhrishi-99/opentelemetry-demo/issues/20) | open (`feat/14-alert-detector`) |
| 15 | F15 | Confidence and escalation rules | brain | P0 | F12, F13 | [#21](https://github.com/rhrishi-99/opentelemetry-demo/issues/21) | merged |
| 15b | F15b | Dependency-evidence tuning | brain | P0 | F13, F15 | [#39](https://github.com/rhrishi-99/opentelemetry-demo/issues/39) | open (`fix/15-dependency-evidence`) |
| 16 | F16 | Report output + JSON schema validation (provisional schema) | safety/report | P0 | F02, F12* | [#22](https://github.com/rhrishi-99/opentelemetry-demo/issues/22) | merged |
| 17 | F17 | Eval harness | eval | P0 | F11, F12, F16 | [#23](https://github.com/rhrishi-99/opentelemetry-demo/issues/23) | open |
| 18 | F18 | faultctl inject/reset (human test tool) | platform | P0 | none | [#30](https://github.com/rhrishi-99/opentelemetry-demo/issues/30) | merged |
| 19 | F19 | Action API (allowlist + log + dry run) | safety | P0 | F01 | [#31](https://github.com/rhrishi-99/opentelemetry-demo/issues/31) | in progress (`feat/19-action-api`) |
| 21 | F21 | Demo UI (Streamlit) | ui | P1 | F12 | — | merged (`feat/21-demo-ui`) |
| 22 | F22 | Fix proposer + Planner review | brain | P0 | F12, F15 | [#42](https://github.com/rhrishi-99/opentelemetry-demo/issues/42) | open (`feat/22-planner`) |
| 23 | F23 | Human gate: approval + kill switch + executor | safety | P0 | F19, F22 contracts | [#43](https://github.com/rhrishi-99/opentelemetry-demo/issues/43) | open (`feat/23-approval-executor`) |
| 24 | F24 | Verify recovery + rollback + escalate | safety/platform | P0 | F19 contract | [#44](https://github.com/rhrishi-99/opentelemetry-demo/issues/44) | open (`feat/24-verify-rollback`) |
| 25a | F25a | Report: actions, approval, verification, hand-offs, prevention | eval-docs | P0 | F16, contracts | [#45](https://github.com/rhrishi-99/opentelemetry-demo/issues/45) | open (`feat/25-report-actions`) |
| 26 | F25b | UI: agent stages + approval | ui | P1 | contracts | [#40](https://github.com/rhrishi-99/opentelemetry-demo/issues/40) | open (`feat/26-ui-approval`) |
| 28 | F28 | Three-agent orchestrator | brain/orchestrator | P0 | F19, F22, F23, F24 | [#41](https://github.com/rhrishi-99/opentelemetry-demo/issues/41) | open (`feat/28-orchestrator`) |
| 29 | D02 | One-page architecture sketch (three agents) | eval-docs | P0 | none | [#46](https://github.com/rhrishi-99/opentelemetry-demo/issues/46) | in progress (`docs/29-architecture-one-page`) |
| 91 | D01 | Architecture sketch | eval-docs | — | none | [#11](https://github.com/rhrishi-99/opentelemetry-demo/issues/11) | merged (PR #13) |
| 93 | D93 | Verified telemetry facts | eval-docs | P0 | none | PR #26 | merged |
| 95 | D95 | Wave 2 specs F11-F17 | eval-docs | P0 | none | PR #26 | merged |
| 96 | D96 | F18-F19 specs | eval-docs | P0 | none | — | merged |
| 97 | I97 | interface-change: Diagnosis contract (`Category`, `Alternative`, `Diagnosis` in models.py + interfaces.md) | brain | P0 | none | — | merged |

(* = needs only the Diagnosis contract from F12's first commit)
