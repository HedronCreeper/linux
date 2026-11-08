# HedronOS FlowState — native helper build. SPDX-License-Identifier: MIT
#
# mkhedronpkg runs `[build].command` from this directory, so `make` here has to work with
# nothing but a C compiler. It is deliberately optional: if CC is missing the package still
# builds (the Python side is the product) and `hedron-latency` is simply not installed.
CC      ?= cc
CFLAGS  ?= -O2 -g -pipe -fno-plt -fexceptions -Wp,-D_FORTIFY_SOURCE=3 -Wformat -Werror=format-security
LDFLAGS ?= -Wl,-O1,--sort-common,--as-needed

BIN     := src/usr/bin/hedron-latency
SRC     := src/usr/src/hedron-latency.c

all: $(BIN)

$(BIN): $(SRC)
	@command -v $(CC) >/dev/null 2>&1 || { echo "  CC unavailable: skipping $(BIN)"; exit 0; }
	mkdir -p $$(dirname $@)
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $<

# The self-test compiles and runs 200 samples: cheap enough for `make test`, strict enough
# to catch a broken build (a garbage binary would exit 2 on the unknown-arg path).
test: $(BIN)
	@test -x $(BIN) || { echo "  hedron-latency not built (no compiler) — skipping"; exit 0; }
	$(BIN) -n 200 -p 20000 --quiet && echo "  hedron-latency: ran ok"

clean:
	rm -f $(BIN)

.PHONY: all test clean
