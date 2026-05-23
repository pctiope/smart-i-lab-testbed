# Smart i-LAB Technical Presentations

This directory contains two expanded Metropolis Beamer technical-review decks
for the `pctiope/smart-i-lab-testbed` repository. Both use the shared
navy/orange style in `smart-ilab-presentation-shared.tex` and repo-grounded
visuals without embedding runtime secrets, generated logs, or generated model
artifacts.

## Files

- `smart-ilab-zone5-overview.tex` -- Zone 5 editable LaTeX source.
- `smart-ilab-zone5-overview.pdf` -- compiled Zone 5 presentation PDF.
- `smart-ilab-air1-overview.tex` -- AIR1 all-zones editable LaTeX source.
- `smart-ilab-air1-overview.pdf` -- compiled AIR1 all-zones presentation PDF.
- `smart-ilab-presentation-shared.tex` -- shared Beamer/TikZ style and helper macros.
- `Makefile` -- helper targets for local rebuilds.

## Structure

- Zone 5 deck: technical-review scope, problem framing, service ownership,
  package structure, runtime flow, person counter, CV labels, sensor
  acquisition, live collector, feature contract, mmWave recency, rolling
  windows, 1D CNN, Optuna training, validation, promotion, FastAPI endpoints,
  health/readiness, reproducibility boundaries, limitations, roadmap, and
  sources.
- AIR1 deck: technical-review scope, problem framing, service ownership,
  two-camera coverage, per-zone MQTT labels, long-form feature contract,
  AIR1-vs-Zone-5 contrast, production-pointer readiness, shared rolling
  windows, 1D CNN, Optuna training, split/coverage policy, validation,
  promotion, contract tests, FastAPI endpoints, health/readiness, operations,
  limitations, roadmap, and sources.

## Build

Both decks are built with Tectonic:

```bash
make pdf
```

Deck-specific targets:

```bash
make zone5
make air1
make clean
```

Equivalent direct commands:

```bash
tectonic smart-ilab-zone5-overview.tex
tectonic smart-ilab-air1-overview.tex
```

The sources intentionally avoid populated `.env` values, API keys, camera URLs,
MQTT credentials, generated runtime/model/log artifacts, and external assets.
