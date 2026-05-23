# Smart i-LAB Testbed Technical Presentation

This directory contains a Metropolis Beamer technical-review deck for the
combined `pctiope/smart-i-lab-testbed` repository.

## Files

- `smart-ilab-testbed-overview.tex` -- editable LaTeX source.
- `smart-ilab-testbed-overview.pdf` -- compiled presentation PDF.
- `Makefile` -- helper target for local rebuilds.

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
