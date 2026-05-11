# OAS Drift Report

_Generated 2026-05-11 against `oas_ssl_iot1.yaml` and `index.js` HEAD._

## Summary

The OpenAPI spec covers **9 of 35** Express routes. **Four entire device families are
undocumented**: `/air-1/*`, `/msr-2/*`, `/smart-plug-v2/*`, `/zigbee2mqtt/*`. The
companion file `SSL IoT-1.yaml` (26 KB) was not analyzed in this pass.

## Paths declared in spec vs. code

| Path | Spec | Code methods |
|---|---|---|
| `/access/{api_key}` | yes | `GET` |
| `/users` | yes | `GET` |
| `/users/{user_name}` | yes | `GET`, `POST`, `PUT`, `DELETE` |
| `/transactions` | yes | `GET` |
| `/air-1` | **MISSING** | `GET` |
| `/air-1/{id}` | **MISSING** | `GET` |
| `/air-1/{id}/light` | **MISSING** | `POST` |
| `/air-1/{id}/avg` | **MISSING** | `GET` |
| `/msr-2` | **MISSING** | `GET` |
| `/msr-2/{id}` | **MISSING** | `GET` |
| `/msr-2/{id}/light` | **MISSING** | `POST` |
| `/msr-2/{id}/buzzer` | **MISSING** | `POST` |
| `/msr-2/{id}/avg` | **MISSING** | `GET` |
| `/smart-plug-v2` | **MISSING** | `GET` |
| `/smart-plug-v2/{id}` | **MISSING** | `GET` |
| `/smart-plug-v2/{id}/relay` | **MISSING** | `POST` |
| `/smart-plug-v2/{id}/avg` | **MISSING** | `GET` |
| `/ag-one` | yes | `GET` |
| `/ag-one/{id}` | yes | `GET` |
| `/ag-one/{id}/light` | yes | `POST` |
| `/ag-one/{id}/avg` | yes | `GET` |
| `/zigbee2mqtt` | **MISSING** | `GET` |
| `/zigbee2mqtt/{id}` | **MISSING** | `GET`, `POST` |
| `/sensibo` | yes | `GET` |
| `/sensibo/{id}` | yes | `GET` |
| `/sensibo/{id}/hvac` | yes | `POST` |
| `/groups` | yes | `GET`, `POST`, `PUT`, `DELETE` |
| `/groups/{id}` | yes | `GET` |

## What this means for `air1` / `zone5` CV consumers

The two CV packages at the repo root almost certainly hit `/air-1/:id` and
`/air-1/:id/avg` — both of which are completely undocumented in the spec. Any
client generator or contract test pointed at the spec will not know these endpoints
exist.

## Remediation

This is a documentation task, not a code change. Two options:

1. **Regenerate the spec from code.** Use `express-openapi-validator` or
   `swagger-jsdoc` with route annotations. Estimate: 4–6h.
2. **Hand-extend the existing spec.** Add the four missing device families using
   the existing `/ag-one` block as a template. Estimate: 2–3h.

Either way, add an OAS contract test to CI (see `.github/workflows/audit.yml`):

```yaml
- name: OAS contract test
  run: |
    npx @stoplight/spectral-cli lint oas_ssl_iot1.yaml
    # optional: pact-style consumer test against a mock server
```

The `SSL IoT-1.yaml` file should be audited next to determine if it supersedes
`oas_ssl_iot1.yaml` or is a different spec version.
