# Incident Zero: AI On-Call Engineer — Final Presentation Script

> **Event**: Bosch "Incident Zero" AI Hackathon (PES University)  
> **Format**: 5-Minute Live Pitch & Demonstration + 3-Minute Technical Q&A Defense  
> **Project**: Autonomous On-Call Reliability Engineer for the OpenTelemetry Astronomy Shop  
> **Repository Target**: `docs/hackathon/presentation-script.md`

---

## Quick Reference Summary Table

| Time Window | Section | On-Screen Visual / Demo Action | Key Message / Deliverable |
| :--- | :--- | :--- | :--- |
| **0:00 – 0:45** | **The Hook & Problem** | Architecture slide / Production microservices topology | MTTR is dominated by the first hour of manual triage; alert fatigue leads to guesses under pressure. |
| **0:45 – 1:45** | **Architecture & Philosophy** | 3-Agent pipeline diagram (`Detective` $\to$ `Planner` $\to$ `Gate` $\to$ `Operator`) | "Safety in code, not in prompts." Honest escalation strictly beats an overconfident wrong fix. |
| **1:45 – 3:30** | **Live Demonstration** | Terminal (`python -m oncall.main run`) + Jaeger/Prometheus + Report | Autonomous triage, evidence-grounded diagnosis, approval gate, Action API fix, verified recovery. |
| **3:30 – 4:15** | **Technical Rigor & CI Rules** | GitHub Actions CI dashboard + `oncall-ci.yml` grep rules | Zero flag reads to diagnose; Planner strictly `tools=[]`; Action API isolation; verified evidence. |
| **4:15 – 5:00** | **Business Impact & Wrap-up** | JSON incident report + summary metrics | MTTR drops from 45 min to 35 seconds with zero unapproved blast radius. |
| **5:00 – 8:00** | **Judge Q&A Defense** | Code references (`action_api.py`, `orchestrator.py`, `evidence.py`) | Ironclad answers to safety, hallucination, flag isolation, and cascading failure questions. |

---

## Detailed Presentation Script

### 1. The Hook & The Problem (0:00 – 0:45)

**[Visual Cue]**: *Display the Astronomy Shop microservices architecture map (14+ polyglot services: frontend, cart, checkout, payment, recommendation, Kafka, Redis, Postgres).*

**[Speaker]**:
> "Good afternoon, judges and mentors. 
> 
> Imagine it is 2:15 AM on a Saturday. Your pager goes off. The astronomy shop's checkout conversion has collapsed by 40%. You open dashboards, grep through gigabytes of logs across 14 polyglot services, trace request IDs in Jaeger, and try to guess what broke while customer orders are failing.
> 
> In real production incidents, **over 70% of the Mean Time To Resolution (MTTR) is spent in this first hour: searching, correlating, and localising the fault.**
> 
> Today, we present **Incident Zero: An AI On-Call Engineer**. It receives raw alerts, autonomously queries metrics, traces, logs, and container states, correlates root causes with mathematical evidence, and executes safe, allowlisted fixes—**only after explicit human authorization**."

---

### 2. Architecture & Core Philosophy (0:45 – 1:45)

**[Visual Cue]**: *Switch to the 1-Page Architecture Diagram from `docs/hackathon/architecture.md` showing typed hand-offs: Detective $\to$ Planner $\to$ Human Gate $\to$ Operator.*

**[Speaker]**:
> "When designing an autonomous SRE, naive solutions fail because they either hallucinate root causes or accidentally execute destructive commands in production. 
> 
> We engineered our system around two foundational rules:
> 
> 1. **Safety is enforced in plain Python code, never in system prompts.** The LLM cannot bypass safety gates because it physically has no write access to Docker, Kubernetes, or shell APIs.
> 2. **An honest escalation strictly outscores an overconfident wrong fix.** If evidence is thin or ambiguous, our agent stops and escalates with findings.
> 
> To achieve this, we decoupled the agent into **Three Specialized Roles with Typed Hand-Offs**:
> 
> * **1. The Detective (LLM + Read-Only Telemetry)**: Ingests the alert and iteratively navigates Prometheus metrics, Jaeger traces, OpenSearch logs, and Docker container health. It never dumps raw data into prompts—it computes deltas and cites concrete evidence IDs, outputting an `Investigation`.
> * **2. The Planner (LLM with `tools=[]`)**: Operates with strictly **zero tools** to eliminate hallucinated tool calls. Its only job is to act as an adversarial reviewer—challenging hypotheses, enforcing confidence thresholds ($\ge 0.70$), and selecting a remediation strictly from an allowlisted category-to-action table. Output: `Plan`.
> * **3. The Human Gate & Operator (Deterministic Python)**: A hard barrier requiring explicit `'yes'` consent in the CLI or UI, backed by a sub-millisecond kill switch. Once approved, the deterministic Operator dispatches the mutation via our isolated Action API, polls telemetry to verify recovery, automatically triggers a rollback if unrecovered, and outputs an official JSON-schema report."

---

### 3. Live Demonstration (1:45 – 3:30)

**[Visual Cue]**: *Split screen: Left side shows terminal running the live demo; Right side shows the OpenTelemetry Demo frontend and Prometheus/Jaeger.*

#### Step 1: Autonomous Telemetry Investigation
**[Action]**: Run command in terminal:
```bash
python -m oncall.main run --description "cartservice high error rate and failing checkouts" --channel cli
```

**[Speaker]**:
> "Let's watch Incident Zero handle a real incident live. We inject a container failure in `cartservice`.
> 
> Notice the **Detective** waking up. Rather than stuffing 50 MB of raw logs into the context window, it calls `container_health`, localises the error in Jaeger traces, and cross-references OpenSearch logs. 
> 
> Every assertion links directly to an immutable evidence ID in our append-only run log: `container:cart@2026-10-24T10:00:00Z` and trace span `GetCart ERROR`. Our code verifies that cited evidence physically exists in the tool output before proceeding, completely preventing hallucinated evidence."

#### Step 2: The Adversarial Planner & Human Gate
**[Action]**: Terminal pauses at the approval prompt:
```text
[APPROVAL REQUEST] Run ID: run-20261024-1130
Component: cartservice | Category: container_down
Proposal: restart_service on cart
Reason: Remediate container_down on cart
Expected Effect: Restore cart service health and recover errors
Type 'yes' to approve action: 
```

**[Speaker]**:
> "Notice the hand-off! The **Planner** receives the investigation. With zero tools, it tests alternative hypotheses: *Could this be Redis cache starvation? Could this be upstream traffic surge?* Both are ruled out by the cited trace data.
> 
> The Planner proposes `restart_service` on `cart`. Now look at the terminal: **the agent has completely stopped.** No action is executed until I, the human engineer, type `'yes'`. 
> 
> If someone engages the kill switch—either setting `KILL_SWITCH=1` or touching `.killswitch`—all execution instantly aborts in Python."

#### Step 3: Execution, Verified Recovery & Report
**[Action]**: Type `yes` into the prompt. Terminal logs Action API execution, polling verification, and prints the report path.

**[Speaker]**:
> "I type `'yes'`. The deterministic **Operator** passes the request to the Action API. 
> 
> It doesn't just fire and forget: it enters a verification loop, querying Prometheus to confirm error rates fall below 5%. If verification failed, it would trigger an automated rollback using opaque byte backups and escalate. 
> 
> The system has generated a 100% compliant JSON-schema report with full timeline, root cause, evidence citations, and future architectural prevention."

---

### 4. Technical Rigor & Hackathon Compliance (3:30 – 4:15)

**[Visual Cue]**: *Display `.github/workflows/oncall-ci.yml` and GitHub Actions summary showing 292 passing tests and green `rules` job.*

**[Speaker]**:
> "Behind this demo is rigorous software engineering. We adhered strictly to every hackathon rule:
> 
> 1. **Zero Feature-Flag Diagnostic Cheating**: Our CI workflow enforces an automated grep rule across the entire codebase. No tool or prompt may read `flagd`, `demo.flagd.json`, or flag state to diagnose. Remediations use a static human runbook without inspecting live flag values.
> 2. **Action API Isolation**: Our CI check guarantees that the Action API is imported exclusively inside `oncall/safety/`. The agent core has zero direct access to mutation primitives.
> 3. **Context Budget Management**: We implemented progressive log/trace compaction, guaranteeing the agent never exceeds token limits or triggers rate limits on Groq or OpenAI.
> 4. **Test Coverage**: Our entire test suite contains **292 unit and integration tests**, covering every component from evidence validation to rollback mechanics."

---

### 5. Business Impact & Wrap-Up (4:15 – 5:00)

**[Visual Cue]**: *Display comparison slide: Manual On-Call vs Incident Zero.*

**[Speaker]**:
> "In summary:
> - **Traditional On-Call**: 45 minutes of manual correlation, alert fatigue, high stress, and risk of accidental human error in production.
> - **Incident Zero**: **35 seconds** from alert to an evidence-grounded diagnosis, a single allowlisted fix, explicit human approval, and automated verification.
> 
> We have built an agent that doesn't pretend to be an omniscient black box. It acts like a senior reliability engineer: thorough in investigation, humble when uncertain, and completely safe by design.
> 
> Thank you, and we welcome your questions!"

---

## 3-Minute Technical Q&A Defense Guide

Use these exact, evidence-backed answers when judges challenge the implementation.

### Q1: "How do you guarantee the agent didn't cheat by reading feature flags in flagd?"
> **Answer**:  
> "We enforced this through both architecture and continuous integration. First, none of our diagnostic tools (`get_metrics`, `search_traces`, `search_logs`, `container_health`) connect to flagd or parse flag files; all telemetry passes through an in-code scrubber that redacts `feature_flag` attributes. Second, our GitHub Actions CI pipeline executes an automated grep rule on every commit that immediately fails if any code outside `oncall/safety/` references `flagd`, `ofrep`, `/feature/`, or flag paths. The agent diagnoses purely through telemetry symptoms."

### Q2: "What prevents the LLM from hallucinating evidence or citing fake trace IDs?"
> **Answer**:  
> "In `oncall/agent/evidence.py`, we implement a deterministic validation pass. The Detective extracts candidate evidence, and our Python code verifies that every cited ID (e.g., `trace:<id>/span:<name>` or `log:<service>@<ts>`) physically exists in the in-memory index of the append-only `RunLog`. If an LLM invents an ID, `validate_evidence` catches it, strips it, downgrades confidence, and forces the decision to `escalate`."

### Q3: "What happens if the agent proposes a fix, the human approves it, but the fix does not resolve the incident?"
> **Answer**:  
> "The Operator does not assume success. After calling the Action API, it enters a verification phase, monitoring Prometheus error rates and Jaeger trace health over a verification window. If the error rate does not recover, the Operator immediately executes an automated rollback request—such as restoring the opaque byte backup of the flag configuration or rolling back load generator users—sets the incident status to `escalated`, and alerts human engineers."

### Q4: "Why split into three separate agents instead of using one big prompt?"
> **Answer**:  
> "A single-agent prompt combines investigation, judgment, and action, which causes catastrophic hallucination—agents rationalize their own guesses and make speculative tool calls. By separating into **Detective** (read-only tools), **Planner** (zero tools, adversarial review), and **Operator** (deterministic Python, zero LLM), we create strict separation of concerns. The Planner cannot make rogue tool calls because `tools=[]`. The Operator cannot invent actions because it executes only approved TypedDict proposals."

### Q5: "How does your system handle ambiguous or multi-component cascade failures?"
> **Answer**:  
> "In accordance with the hackathon's core scoring principle, our confidence engine in `oncall/agent/confidence.py` penalizes ambiguous scenarios. If multiple unrelated root causes appear, if trace error propagation is disjointed, or if confidence falls below 0.70, the agent explicitly chooses `decision='escalate'`. It generates the full investigation report and timeline for human SREs without taking speculative, destructive actions."

### Q6: "What is your kill switch mechanism and how fast is it?"
> **Answer**:  
> "The kill switch operates entirely in Python without waiting for any LLM response. It checks two mechanisms: an environment variable `KILL_SWITCH=1` and a file sentinel `.killswitch`. It is evaluated at two distinct boundaries: inside `HumanGate` before prompting the human, and inside `Operator` immediately prior to dispatching to the Action API. If engaged, it terminates in sub-millisecond execution time, logs an `aborted` event, and generates a report."

### Q7: "How do you stay within API rate limits and token budgets during a massive incident?"
> **Answer**:  
> "We implement a 3-tier compacting strategy in `oncall/agent/loop.py`. First, tools return condensed top-error summaries rather than raw JSON dumps. Second, if prompt size approaches our budget (`LLM_MAX_PROMPT_TOKENS=6000`), earlier tool responses are progressively compacted into short summaries and evidence IDs. Third, if an HTTP 413 or rate-limit error occurs, our client automatically shrinks older context turns and retries across an endpoint fallback chain."

### Q8: "How does your system output compare to the official hackathon schema?"
> **Answer**:  
> "Every single run—whether successful, escalated, or aborted—ends by calling `report_run` in `oncall/report/output.py`. The output is validated in-memory against the official JSON schema (`oncall/report/schema.json`) using the `jsonschema` library. It contains root cause, severity, confidence, cited evidence, timeline events, executed actions, verification results, limitations, and preventative measures."

---

## Technical Appendix: Demo Cheat Sheet & Commands

### 1. Incur Incident (faultctl)
```bash
# In separate terminal (human operator tool)
python -m scripts.faultctl.main inject cart-failure
```

### 2. Run Autonomous Remediation (CLI Approval)
```bash
python -m oncall.main run --description "cartservice high error rate and failing checkouts" --service cartservice --execute --channel cli
```

### 3. Run Dry-Run Mode (Simulation only)
```bash
python -m oncall.main run --description "payment service authorization timeout" --service paymentservice --channel cli
```

### 4. Engage Kill Switch
```powershell
# Set env var:
$env:KILL_SWITCH="1"
# Or create sentinel file:
New-Item -ItemType File -Path ".killswitch" -Force
```

### 5. Run Verification & Full Test Suite
```bash
pytest -q oncall/ scripts/faultctl/ eval/
ruff check oncall/ scripts/faultctl/ eval/
ruff format --check oncall/ scripts/faultctl/ eval/
```
