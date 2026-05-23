# Smart i-LAB Testbed Technical Presentation

This directory contains a Metropolis Beamer technical-review deck for the
combined `pctiope/smart-i-lab-testbed` repository. The deck uses a custom
navy/orange Metropolis style and repo-grounded visuals to explain the
frontend, backend, architecture, CI/CD, ML training, deployment, and roadmap
story without embedding runtime secrets or generated model artifacts.

## Files

- `smart-ilab-testbed-overview.tex` -- editable LaTeX source.
- `smart-ilab-testbed-overview.pdf` -- compiled presentation PDF.
- `Makefile` -- helper target for local rebuilds.

## Structure

- Frontend: Digital Twin, Zone 5 dashboard, AIR1 dashboard.
- Backend and architecture: IoT1 REST API, security/storage, user flow, and
  end-to-end data path.
- CI/CD and deployment: GitHub workflow ownership, staging Compose versus
  production systemd, and model delivery.
- ML training: CV labels, Zone 5 feature contract, mmWave recency, and AIR1
  all-zones contract.
- Verification and roadmap: current gates, remaining cutover work, and risks.

## Build

The deck is built with Tectonic:

```bash
make pdf
```

Equivalent direct command:

```bash
tectonic smart-ilab-testbed-overview.tex
```

The source intentionally avoids populated `.env` values, API keys, camera URLs,
MQTT credentials, and generated runtime/model/log artifacts.
