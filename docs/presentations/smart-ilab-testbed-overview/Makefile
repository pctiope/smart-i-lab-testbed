TECTONIC ?= tectonic

ZONE5_TEX := smart-ilab-zone5-overview.tex
AIR1_TEX := smart-ilab-air1-overview.tex
SHARED_TEX := smart-ilab-presentation-shared.tex
ZONE5_PDF := smart-ilab-zone5-overview.pdf
AIR1_PDF := smart-ilab-air1-overview.pdf
PDFS := $(ZONE5_PDF) $(AIR1_PDF)

.PHONY: pdf zone5 air1 clean

pdf: $(PDFS)

zone5: $(ZONE5_PDF)

air1: $(AIR1_PDF)

$(ZONE5_PDF): $(ZONE5_TEX) $(SHARED_TEX)
	$(TECTONIC) $(ZONE5_TEX)

$(AIR1_PDF): $(AIR1_TEX) $(SHARED_TEX)
	$(TECTONIC) $(AIR1_TEX)

clean:
	rm -f $(PDFS)
