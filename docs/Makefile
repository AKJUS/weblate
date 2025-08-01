# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Minimal makefile for Sphinx documentation
#

# You can set these variables from the command-line, and also
# from the environment for the first two.
SPHINXOPTS    ?=
SPHINXBUILD   ?= sphinx-build
SOURCEDIR     = .
BUILDDIR      = _build

# Put it first so that "make" without argument is like "make help".
help:
	@$(SPHINXBUILD) -M help "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

.PHONY: help Makefile update-po update-screenshots update-schemas update-openapi run-selenium

run-selenium:
	@rm -rf ../data-test ../test-images
	@cd .. && \
		. scripts/test-database.sh && \
		pytest -n 3 weblate/trans/tests/test_selenium.py

SCREENSHOTS = $(wildcard screenshots/*.webp)

../test-images/%.png: run-selenium
	@true

screenshots/%.webp: ../test-images/%.png run-selenium
	@echo "Processing $@"
	@convert $< $@

update-screenshots: $(SCREENSHOTS)
	@echo $(SCREENSHOTS)

update-schemas:
	@cp $$(python -c "import importlib.resources; print(importlib.resources.files('weblate_schemas'))")/schemas/* specs/schemas/

update-openapi:
	@WEBLATE_HIDE_VERSION=1 uv run --no-sync ../manage.py spectacular --skip-checks --fail-on-warn --validate --urlconf="weblate.api.urls" > specs/openapi.yaml

update-po: $(wildcard locales/*/LC_MESSAGES/docs.po)

locales/%/LC_MESSAGES/docs.po: locales/docs.pot
	@if ! test -h locales/$* ; then msgmerge -U $@ -C ../weblate/locale/$*/LC_MESSAGES/django.po --previous $< ; fi

locales/docs.pot: gettext
	@cp _build/gettext/docs.pot $@
	@../scripts/filter-docs-po.py $@


# Catch-all target: route all unknown targets to Sphinx using the new
# "make mode" option.  $(O) is meant as a shortcut for $(SPHINXOPTS).
%: Makefile
	@$(SPHINXBUILD) -M $@ "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)
