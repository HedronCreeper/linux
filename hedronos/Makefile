# HedronOS — top-level convenience targets.
# SPDX-License-Identifier: MIT
#
# Everything here is a thin, predictable wrapper around ./build.sh and ./tools/,
# so you can read the script and skip this file entirely if you prefer.

SHELL := /bin/bash
PROFILE ?= gamerte
ARCH ?= x86_64
KERNEL ?= auto
JOBS ?= $(shell nproc 2>/dev/null || echo 4)
OUT ?= dist
DRY_RUN ?= 0
ROOT ?= $(abspath ..)
PY ?= python3

BUILD_FLAGS := --profile $(PROFILE) --arch $(ARCH) --kernel $(KERNEL) \
               --jobs $(JOBS) --out $(OUT)
ifeq ($(DRY_RUN),1)
BUILD_FLAGS += --dry-run --allow-offline
endif

.PHONY: help
help:
	@echo "HedronOS build entry points"
	@echo
	@echo "  make verify          static whole-system verification (offline, no root)"
	@echo "  make test            unit + golden tests (stdlib unittest, offline)"
	@echo "  make lint            shell/ini/toml/json lint of every shipped file"
	@echo "  make db              re-generate data/gpu-db from the kernel tree"
	@echo "  make iso             build the bootable image (needs an Arch-ish host or podman)"
	@echo "  make iso DRY_RUN=1   run the whole build with side effects stubbed"
	@echo "  make kernel          build only the linux-hedron package from $(ROOT)"
	@echo "  make packages        build only the hedronos-* packages"
	@echo "  make vm              boot the produced ISO headless for a smoke test"
	echo:=
	@echo "  make clean           remove work/, dist/, cache/"
	@echo "  make release         verify + test + iso + sign + checksum manifest"
	@echo
	@echo "  Variables: PROFILE=$(PROFILE) ARCH=$(ARCH) JOBS=$(JOBS) DRY_RUN=$(DRY_RUN) KERNEL=$(KERNEL)"

# ---------------------------------------------------------------- quality gates
.PHONY: verify
verify:
	@$(PY) tools/hedronos-verify --root . --kernel-root $(ROOT) --report verification-report.json
	@echo "verify: OK"

.PHONY: test
test:
	@bash tests/run_tests.sh

.PHONY: lint
lint:
	@bash tools/lint-shell.sh --root .
	@$(PY) tools/iso-manifest.py lint --root .
	@echo "lint: OK"

.PHONY: db
db:
	@$(PY) tools/gen-gpu-db.py --kernel $(ROOT) --out data/gpu-db
	@echo "db: regenerated from $(ROOT)"

.PHONY: check
check: lint test verify

# ---------------------------------------------------------------- artifacts
.PHONY: iso
iso:
	./build.sh $(BUILD_FLAGS)

.PHONY: kernel
kernel:
	./build.sh $(BUILD_FLAGS) --stage kernel

.PHONY: packages
packages:
	./build.sh $(BUILD_FLAGS) --stage packages

.PHONY: vm
vm:
	./build.sh $(BUILD_FLAGS) --stage vm --boot-test

.PHONY: release
release:
	$(MAKE) check
	./build.sh $(BUILD_FLAGS) --sign --release-notes
	@echo
	@echo "artifacts in $(OUT)/ — sha256: $$(cd $(OUT) && sha256sum *.iso 2>/dev/null | head -1 || echo 'n/a')"

# ---------------------------------------------------------------- housekeeping
.PHONY: clean
clean:
	@rm -rf work/ dist/ cache/ verification-report.json
	@echo "clean: removed work/ dist/ cache/"

.PHONY: distclean
distclean: clean
	@rm -rf data/gpu-db/generated/
	@echo "distclean: also removed generated data"
