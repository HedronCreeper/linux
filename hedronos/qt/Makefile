# SPDX-License-Identifier: MIT
# The QML shells are data; this is the one C++ piece they need. `make` with Qt6 present, or
# `make check` anywhere (it validates the sources structurally when Qt is not installed).
BUILD ?= build

.PHONY: all clean check configure

all: configure
	cmake --build $(BUILD) --parallel

configure: $(BUILD)/CMakeCache.txt

$(BUILD)/CMakeCache.txt:
	cmake -S . -B $(BUILD) -DCMAKE_BUILD_TYPE=Release

# Runs without Qt6 so the ISO build machine (and CI) can still prove the files are coherent.
check:
	@command -v qmake6 >/dev/null 2>&1 && echo "qmake6: found" || echo "qmake6: absent (Qt build skipped)"
	@for f in hedronfeed.cpp hedronstore_main.cpp hedronfeed.h; do \
	  test -s "$$f" || { echo "empty: $$f"; exit 1; }; \
	  awk 'BEGIN{o=0;c=0} {for(i=1;i<=length($$0);i++){ch=substr($$0,i,1); if(ch=="{")o++; if(ch=="}")c++}} END{if(o!=c){print "brace mismatch in '"$$f"' ("o" vs "c")"; exit 1}}' "$$f" || exit 1; \
	done
	@grep -q "local.hedron.feed" hedronstore_main.cpp || { echo "main.cpp does not register the feed module"; exit 1; }
	@echo "qt: sources coherent"

clean:
	rm -rf $(BUILD)
