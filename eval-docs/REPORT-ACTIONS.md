# Report Actions, Approval, Verification, Hand-offs, Prevention

## Overview
The **F25a** feature expands the run-report schema with five new top-level sections that capture the operational lifecycle of an incident response:

| Section | Purpose | Key fields |
|---|---|---|
| **actions** | Chronological list of remediation steps taken by the agent or operator. | id, description, 	imestamp, status (queued/running/done/failed) |
| **approval** | Record of any human or automated approval required before an action is executed. | pprover, method (manual, policy, LLM), granted (bool), 	imestamp |
| **verification** | Evidence that the action achieved the intended effect. | ction_id, checks (list of metric checks), esult (pass/fail), details |
| **hand-offs** | Transfer of responsibility between subsystems or teams (e.g., from detection to mitigation). | rom, 	o, eason, 	imestamp |
| **prevention** | Recommendations or automated guardrails added to avoid repeat incidents. | 	ype (rule, config, alert), description, implemented (bool) |

These sections are optional for existing runs but **must** be present for any run that involves automated mitigation or manual intervention.

---

## Schema (JSON-Schema excerpt)
`json
{
   type: object,
  properties: {
    actions: {
      type: array,
      items: {
        type: object,
        required: [id, description, timestamp, status],
        properties: {
          id: {type: string},
          description: {type: string},
          timestamp: {type: string, format: date-time},
          status: {type: string, enum: [queued, running, done, failed]}
        }
      }
    },
    approval: {
      type: array,
      items: {
        type: object,
        required: [approver, method, granted, timestamp],
        properties: {
          approver: {type: string},
          method: {type: string},
          granted: {type: boolean},
          timestamp: {type: string, format: date-time}
        }
      }
    },
    verification: {
      type: array,
      items: {
        type: object,
        required: [action_id, checks, result],
        properties: {
          action_id: {type: string},
          checks: {type: array, items: {type: string}},
          result: {type: string, enum: [pass, fail]},
          details: {type: string}
        }
      }
    },
    hand_offs: {
      type: array,
      items: {
        type: object,
        required: [from, to, reason, timestamp],
        properties: {
          from: {type: string},
          to: {type: string},
          reason: {type: string},
          timestamp: {type: string, format: date-time}
        }
      }
    },
    prevention: {
      type: array,
      items: {
        type: object,
        required: [type, description, implemented],
        properties: {
          type: {type: string, enum: [rule, config, alert]},
          description: {type: string},
          implemented: {type: boolean}
        }
      }
    }
  }
}
`

---

## Integration points
* **oncall/report/schema.json** – extend the existing schema with the above excerpt (the file already exists, just add these properties).
* **oncall/report/output.py** – expose helper functions dd_action, ecord_approval, dd_verification, ecord_handoff, dd_prevention that mutate the running report dictionary.
* **Documentation** – this REPORT-ACTIONS.md explains the purpose and structure; it will be linked from the main README.md of the eval-docs package.

---

## Acceptance criteria
* The new sections appear in the generated JSON report when the corresponding helper functions are invoked.
* Unit tests in oncall/tests/report/test_actions.py verify schema validation passes for a minimal report that includes each section.
* The documentation builds without lint warnings and is included in the PR description.
`
---

*Implemented as a starter spec; further implementation of helper functions can be added in subsequent tickets.*
