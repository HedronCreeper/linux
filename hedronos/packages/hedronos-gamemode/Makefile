# HedronOS GameMode — native daemon/client. SPDX-License-Identifier: MIT
#
# No dependencies on purpose: this builds with a bare cc, which is what lets the ISO build
# run inside a Debian container where systemd dev headers may be absent.
CC      ?= cc
CFLAGS  ?= -O2 -g -pipe -Wall -Wextra -fno-plt -fexceptions -Wp,-D_FORTIFY_SOURCE=3 \
           -Wformat -Werror=format-security -fstack-clash-protection -fcf-protection=full
LDFLAGS ?= -Wl,-O1,--sort-common,--as-needed

BIN := src/usr/bin/hgm
SRC := src/usr/src/hedron-gamemode.c

all: $(BIN)

$(BIN): $(SRC)
	@command -v $(CC) >/dev/null 2>&1 || { echo "  CC unavailable: skipping $(BIN)"; exit 0; }
	mkdir -p $$(dirname $@)
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $<

# A real functional test, not a smoke test: start the daemon on a private socket, claim,
# query, kill the claimer, and require the claim to have expired by itself.
test: $(BIN)
	@test -x $(BIN) || { echo "  hgm not built (no compiler) — skipping"; exit 0; }
	@sock=$$(mktemp -u /tmp/hgm-selftest-XXXX.sock); \
	 export HEDRON_GAMEMODE_SOCK=$$sock; \
	 $(BIN) --daemon >/tmp/hgm-selftest.log 2>&1 & daemon=$$!; \
	 for i in 1 2 3 4 5 10 15 20; do test -S $$sock && break; sleep 0.05; done; \
	 sleep 300 & claimer=$$!; \
	 $(BIN) start $$claimer selftest | grep -q '"active":true' || { echo "  FAIL: claim not recorded"; \
	   kill $$daemon 2>/dev/null; rm -f $$sock; exit 1; }; \
	 $(BIN) status | grep -q selftest || { echo "  FAIL: status missing the claim"; \
	   kill $$daemon $$claimer 2>/dev/null; rm -f $$sock; exit 1; }; \
	 kill $$claimer 2>/dev/null; sleep 0.2; \
	 out=$$($(BIN) status); \
	 kill $$daemon 2>/dev/null; rm -f $$sock; \
	 case "$$out" in *'"clients":0'*) echo "  hgm: claim auto-expired with the process";; \
	 *) echo "  FAIL: stale claim survived: $$out"; exit 1;; esac

clean:
	rm -f $(BIN)

.PHONY: all test clean
