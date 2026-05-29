# Zone 5 Model Registry

This directory is for sanitized production model metadata only. It must not
contain model weights, production pointers, runtime environment files, API keys,
or copied contents from `model/`.

Model binaries live in the external artifact store and on the production web
server under `model/runs/<run_id>/`. The live app switches production models by
reading the server-local `model/production_run.txt` pointer.

Expected tracked files:

- `zone5-production-latest.json`: latest promoted production model summary.
- `promotions/<run_id>.json`: immutable summary for a promoted run.

Registry summaries for current production runs should report
`model_contract_version: zone5_missingness_decoupled_v1`. The current contract
does not include mmWave-recency feature columns.
