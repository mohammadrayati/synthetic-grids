# synthetic-grids — reproducibility Makefile
#
# Standalone package (own venv, own git repo): the data-viewer webpage, self-contained - no
# dependency on the main synthetic-grids-claude-agents project. data/ (~1.3G) comes from Google
# Drive via `make download-data`, from the SAME shared archive impedance-estimation/ uses (see
# that project's Makefile and the main project's `make shared-data-archive`) - not a separate
# archive, since the two were found to be ~99% identical. webpage/ (code) is tracked in git.

VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

# Google Drive file id of the shared data archive - SAME value as impedance-estimation/Makefile's
# own DATA_ARCHIVE_GDRIVE_ID. Built by the main project's `make shared-data-archive`.
DATA_ARCHIVE_GDRIVE_ID := REPLACE_WITH_GDRIVE_FILE_ID

.DEFAULT_GOAL := help

.PHONY: help
help:
	@echo "synthetic-grids — available targets:"
	@echo "  make setup        - create .venv and install requirements.txt"
	@echo "  make download-data - fetch data/ (~1.3G) from Google Drive"
	@echo "  make webpage      - launch the Flask data-viewer server (:8811)"
	@echo "  make clean-venv   - remove .venv"

.PHONY: setup
setup:
	@if [ -x "$(PY)" ]; then echo "$(VENV) already present, skipping."; else python3 -m venv $(VENV); fi
	$(PIP) install -r requirements.txt

.PHONY: clean-venv
clean-venv:
	rm -rf $(VENV)

.PHONY: download-data
download-data: setup
	$(PY) download_data.py --file-id $(DATA_ARCHIVE_GDRIVE_ID)

.PHONY: download-data-force
download-data-force: setup
	$(PY) download_data.py --file-id $(DATA_ARCHIVE_GDRIVE_ID) --force

.PHONY: webpage
webpage: setup
	$(PY) webpage/app.py
