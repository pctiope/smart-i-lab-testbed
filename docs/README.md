# Smart i-LAB testbed — docs index

If you just arrived and don't know where to start: read this file, then read
`STATUS.md`, then act on whatever's marked `[ ]` open.

## What this folder contains

Seven docs covering the IoT1 security remediation that landed on 2026-05-11 / 12.
None of them are required to *run* the stack -- but together they explain why
the code is shaped the way it is and what's still on the operator's plate.

| File | Read it when... |
|---|---|
| **`STATUS.md`** | Always. One-page snapshot of what's done and what's pending. |
| `IOT1_PRE_DEPLOYMENT.md` | Before applying migrations or restarting containers against the live DB. Captures live state + safe-sequence. |
| `IOT1_DEPLOY_DIFF.md` | When you need to explain to a stakeholder (API consumer, CV team, ops) what changes after the cutover. |
| `IOT1_CHANGELOG.md` | When you want the rollup of every fix grouped by release (v0.0 quick wins -> v0.5 forward-compat). |
| `IOT1_AUDIT.md` | When you want the original 46-finding security audit. Forensic / why-it-was-broken context. |
| `IOT1_REMEDIATION.md` | When you're an agent picking up where the previous agent left off. Patterns, pitfalls, troubleshooting recipes. |
| `IOT1_CV_COMPATIBILITY.md` | When you need to verify the air1/zone5 CV consumer packages survive the cutover. |
| `sync_iot1_to_testbed.ps1` | When the upstream CARE-SSL/IoT1 changes and you want to mirror the changes here. Dry-runs by default. |

## If you are an agent, start with

1. `STATUS.md` -- check what's done vs. pending.
2. `IOT1_REMEDIATION.md` §10 ("Pointers for an agent making related changes") -- for guidance by user-intent.
3. Whatever section of `STATUS.md` is open -- act on that.

## If you are an operator (human), start with

1. `STATUS.md` -- find the next action item.
2. `IOT1_PRE_DEPLOYMENT.md` -- only relevant pieces (e.g. "Step 4 -- Deploy" once steps 1-3 are done).
3. `IOT1_DEPLOY_DIFF.md` if you need to brief stakeholders before cutting over.

## If you are a security reviewer, start with

1. `IOT1_AUDIT.md` -- the 46-finding audit with CWE refs.
2. `IOT1_CHANGELOG.md` -- per-finding remediation status (Released vs. Open).

## Conventions in these docs

- File paths are absolute Windows paths because the existing testbed was on a Windows host.
  Translate to your own deployment as needed.
- "Old code" / "running code" / "deployed" = whatever's currently behind
  `http://10.158.66.30:80` (the pre-audit code, as of 2026-05-12).
- "Hardened code" / "new code" = whatever's in `smart-i-lab-testbed/` after the
  sync from CARE-SSL/IoT1.
- Lab credentials are not duplicated here -- see `SPOILER_image.png` in the
  CARE-SSL workspace, or the populated `.env` files (gitignored).

## Where the source lives

Updates flow upstream-to-downstream:

```
CARE-SSL/IoT1/   <-- where remediation happens (authoritative)
        |
        | sync_iot1_to_testbed.ps1
        v
smart-i-lab-testbed/   <-- this folder (deploys from here)
        |
        v
http://10.158.66.30:80   <-- the live API
```

The CV consumer packages (`air1_all_zones_cv_time_features_package/`,
`zone5_cv_time_features_package/`) live only in the testbed and are
preserved by the sync script (never overwritten).
