# Smart i-LAB Technical Presentations

This directory contains two focused Metropolis Beamer technical-review decks
for the `pctiope/smart-i-lab-testbed` repository. Both use the shared
navy/orange style in `smart-ilab-presentation-shared.tex` and repo-grounded
visuals without embedding runtime secrets or generated model artifacts.

## Files

- `smart-ilab-zone5-overview.tex` -- Zone 5 editable LaTeX source.
- `smart-ilab-zone5-overview.pdf` -- compiled Zone 5 presentation PDF.
- `smart-ilab-air1-overview.tex` -- AIR1 all-zones editable LaTeX source.
- `smart-ilab-air1-overview.pdf` -- compiled AIR1 all-zones presentation PDF.
- `smart-ilab-presentation-shared.tex` -- shared Beamer/TikZ style and helper macros.
- `Makefile` -- helper targets for local rebuilds.

## Structure

- Zone 5 deck: dashboard, shared API context, Zone 5 architecture, CI/CD,
  staging Compose versus production systemd, retrain/model delivery, labels,
  methodology, mmWave recency, deployment, verification, roadmap, and sources.
- AIR1 deck: all-zones dashboard, minimal shared API context, AIR1
  architecture, per-zone labels, long-form all-zones pipeline, verification,
  roadmap, and sources.

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
