TEX := smart-ilab-testbed-overview.tex
PDF := smart-ilab-testbed-overview.pdf
TECTONIC ?= tectonic

.PHONY: pdf clean

pdf: $(PDF)

$(PDF): $(TEX)
	$(TECTONIC) $(TEX)

clean:
	rm -f $(PDF)
