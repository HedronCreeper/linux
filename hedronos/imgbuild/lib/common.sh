# HedronOS image build library — shared plumbing. SPDX-License-Identifier: MIT
#
# Everything the stages need: logging, a dry-run-aware command runner, the manifest recorder that
# makes a rehearsal inspectable, and the profile loader. No stage logic lives here — if a
# `case "$PROFILE"` appears in this file it belongs in imgbuild/profiles/ instead.
#
# The directory is called imgbuild/, not build/: snapshot and cache policies in most tooling
# (including the one this repo is developed under) skip any directory named `build`, and a stage
# library that silently vanishes from a checkout is a far worse fate than an ugly name.

HEDRON_LIB_COMMON=1
: "${HEDRON_SRC:?imgbuild/lib/common.sh must be sourced from build.sh}"

SUDO=""
DRY_RUN_LOG="${DRY_RUN_LOG:-${WORK_ROOT:-work}/dry-run.log}"
STAGE_NAME=""
STAGE_STARTED=0
STAGE_TIMES=()
PYTHON_BIN="${PYTHON_BIN:-}"
export HEDRON_BUILD_LIB=1

# ------------------------------------------------------------------------ logging
_colour() { [ "${VERBOSE:-1}" = "1" ] && [ -t 2 ] && printf '\033[%sm' "$1" || printf ''; }

log() {
	local level="${1:-info}" msg="${2:-}"
	shift 2 2>/dev/null || true
	case "$level" in
	info) printf '%s[%s]%s %s\n' "$(_colour 36)" "$(date -u +%H:%M:%S 2>/dev/null || echo --:--)" "$(_colour 0)" "$msg" >&2 ;;
	ok) printf '%s[%s] ✓%s %s\n' "$(_colour 32)" "$(date -u +%H:%M:%S 2>/dev/null || echo --:--)" "$(_colour 0)" "$msg" >&2 ;;
	warn) printf '%s[%s] !%s %s\n' "$(_colour 33)" "$(date -u +%H:%M:%S 2>/dev/null || echo --:--)" "$(_colour 0)" "$msg" >&2 ;;
	error|err) printf '%s[%s] ✗%s %s\n' "$(_colour 31)" "$(date -u +%H:%M:%S 2>/dev/null || echo --:--)" "$(_colour 0)" "$msg" >&2 ;;
	verbose) [ "${VERBOSE:-1}" = "1" ] && printf '[..] %s\n' "$msg" >&2 ;;
	*) printf '[%s] %s\n' "$level" "$msg" >&2 ;;
	esac
	[ -n "$DRY_RUN_LOG" ] && printf '%s %s\n' "$level" "$msg" >>"$DRY_RUN_LOG" 2>/dev/null || true
}

die() { log error "$*"; exit 1; }
warn() { log warn "$*"; }

stage_begin() {
	STAGE_NAME="$1"
	STAGE_STARTED="$SECONDS"
	printf '\n\033[1m── %s\033[0m\n' "$1" >&2
	log info "stage: $1"
}

stage_end() {
	local took=$((SECONDS - STAGE_STARTED))
	STAGE_TIMES+=("$1=${took}s")
	log ok "$1 finished in ${took}s"
	record stage-times "$1 $took"
}

stage_fail() {
	log error "stage '$1' failed after $((SECONDS - STAGE_STARTED))s"
	[ "${DRY_RUN:-0}" = "1" ] && log error "this was a dry run: the failure is real and the rehearsal should not have hit it"
	exit 1
}

# ------------------------------------------------------------------- environment
have() { command -v "$1" >/dev/null 2>&1; }

require_cmd() {
	local c
	for c in "$@"; do
		have "$c" || { [ "${DRY_RUN:-0}" = "1" ] && { log warn "missing '$c' (dry run: continuing, a real build needs it)"; return 0; }; die "required command '$c' is not installed"; }
	done
	return 0
}

sudoify() {
	if [ "$(id -u)" = "0" ] || [ "${DRY_RUN:-0}" = "1" ]; then
		SUDO=""
	else
		SUDO="${SUDO:-sudo}"
		have sudo || die "this step needs root and sudo is not installed"
	fi
	printf '%s' "$SUDO"
}

# run <argv...> : the only place a command is executed. In dry-run it is printed, logged and
# skipped, which is what makes `--dry-run` a rehearsal of the real decisions rather than a mock.
run() {
	local printable
	printable="$(printf '%q ' "$@")"
	log verbose "run: ${printable% }"
	if [ "${DRY_RUN:-0}" = "1" ]; then
		printf 'EXEC %s\n' "${printable% }" >>"$DRY_RUN_LOG" 2>/dev/null || true
		return 0
	fi
	"$@"
}

# run_local <argv...> : execute even in a dry run. Only for steps that read the source tree and
# write inside WORK_ROOT (or the generated files next to it) — the rehearsal is worth nothing if
# it does not compute the real answers, and a python step over our own data is neither privileged
# nor destructive. Never wrap a package install, a mount, or a network fetch in this.
run_local() {
	log verbose "run (local): $(printf '%q ' "$@")"
	if [ "${DRY_RUN:-0}" = "1" ]; then
		printf 'LOC  %s\n' "$(printf '%q ' "$@" | sed 's/ $//')" >>"$DRY_RUN_LOG" 2>/dev/null || true
	fi
	"$@"
}

# runsh <snippet> : for the few things that genuinely need a shell (redirections, env prefixes).
runsh() {
	local snippet="$1"
	log verbose "runsh: $snippet"
	if [ "${DRY_RUN:-0}" = "1" ]; then
		printf 'SH   %s\n' "$snippet" >>"$DRY_RUN_LOG" 2>/dev/null || true
		return 0
	fi
	sh -c "$snippet"
}

# capture <argv...> : run and return stdout (works in dry-run too: prints the empty string).
capture() {
	if [ "${DRY_RUN:-0}" = "1" ]; then return 1; fi
	"$@" 2>/dev/null
}

install_file() {
	local src="$1" dst="$2" mode="${3:-0644}"
	[ -r "$src" ] || { log warn "install_file: missing source $src"; return 1; }
	if [ "${DRY_RUN:-0}" = "1" ]; then
		printf 'INST %s -> %s (%s)\n' "$src" "$dst" "$mode" >>"$DRY_RUN_LOG" 2>/dev/null || true
		return 0
	fi
	mkdir -p "$(dirname -- "$dst")"
	install -m "$mode" -- "$src" "$dst"
}

copy_tree() {
	local src="$1" dst="$2"
	[ -d "$src" ] || { log warn "copy_tree: no such directory $src"; return 1; }
	if [ "${DRY_RUN:-0}" = "1" ]; then
		printf 'TREE %s -> %s\n' "$src" "$dst" >>"$DRY_RUN_LOG" 2>/dev/null || true
		return 0
	fi
	mkdir -p "$dst"
	cp -a -- "$src/." "$dst/"
}

# ------------------------------------------------------------------ manifests
manifest_dir() {
	local d="${WORK_ROOT:-work}/manifest"
	[ "${DRY_RUN:-0}" = "1" ] && mkdir -p "$d" 2>/dev/null || install -d "$d" 2>/dev/null || mkdir -p "$d"
	printf '%s' "$d"
}

record() {
	# record <file> <line...>  — append one line to a manifest file. Everything a stage decided
	# goes here, because the only way to review a 20-minute build is to read what it wrote down.
	local file="$1"; shift
	local dir; dir="$(manifest_dir)"
	printf '%s\n' "$*" >>"${dir}/${file}" 2>/dev/null || log warn "cannot record into ${dir}/${file}"
}

record_json() {
	# record_json <file> <json> : validate before storing, so a broken generator cannot poison a
	# later stage that reads the file.
	local file="$1" json="$2" dir; dir="$(manifest_dir)"
	if ! printf '%s' "$json" | "${PYTHON_BIN:-python3}" -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null; then
		log error "record_json: refusing to write invalid JSON to ${file}"
		return 1
	fi
	printf '%s\n' "$json" >"${dir}/${file}" 2>/dev/null || { log warn "cannot write ${dir}/${file}"; return 1; }
}

human_size() {
	local bytes="${1:-0}"
	"${PYTHON_BIN:-python3}" - "$bytes" <<'PY' 2>/dev/null || printf '%s B\n' "$bytes"
import sys
n = float(sys.argv[1])
for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
    if abs(n) < 1024 or unit == "TiB":
        print("%.1f %s" % (n, unit) if unit != "B" else "%d B" % n)
        break
    n /= 1024
PY
}

# ------------------------------------------------------------------ python glue
py() {
	# py <code...> with PYTHONPATH set to hedronos/tools: the stages share the same library the
	# shipped tools use, so a decision is implemented once (hedronlib) and not re-typed in bash.
	( cd "$HEDRON_SRC" && PYTHONPATH="$HEDRON_SRC/tools:${PYTHONPATH:-}" "${PYTHON_BIN:-python3}" - "$@" )
}

# parse_conf <file> <key> [default]
parse_conf() {
	local file="$1" key="$2" def="${3:-}"
	[ -r "$file" ] || { printf '%s' "$def"; return 0; }
	py "$file" "$key" "$def" <<'PY'
import sys
from hedronlib.util import parse_keyvalue
path, key, default = sys.argv[1], sys.argv[2], sys.argv[3]
data = parse_keyvalue(path)   # util.parse_keyvalue takes a path, not the text
print(data.get(key, default))
PY
}

# profile_conf <profile> : the path of the profile file for the current build
profile_conf() {
	printf '%s/imgbuild/profiles/%s.conf' "$HEDRON_SRC" "${1:-$PROFILE}"
}

common_init() {
	if [ -z "$PYTHON_BIN" ]; then
		for cand in python3 python; do
			have "$cand" && { PYTHON_BIN="$(command -v "$cand")"; break; }
		done
	fi
	[ -n "$PYTHON_BIN" ] || die "no python3 on PATH — the build tooling is Python (stdlib only)"
	export PYTHON_BIN

	local conf; conf="$(profile_conf "${PROFILE:-}")"
	if [ ! -r "$conf" ]; then
		die "no profile at $conf (expected imgbuild/profiles/${PROFILE}.conf)"
	fi
	log info "profile: $conf"
	# The profile file is key = value; export everything it sets, prefixed HEDRON_P_, so stages
	# read one namespace and a stray shell variable in the environment cannot override a profile.
	local line key value
	while IFS= read -r line; do
		case "$line" in ''|\#*) continue ;; esac
		key="${line%%=*}"; value="${line#*=}"
		key="$(printf '%s' "$key" | tr -d '[:space:]')"
		value="$(printf '%s' "$value" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"'// -e 's/"$//')"
		[ -n "$key" ] || continue
		printf -v "HEDRON_P_$key" '%s' "$value"
		export "HEDRON_P_$key"
	done <"$conf"

	mkdir -p "${WORK_ROOT}/manifest" "${WORK_ROOT}/rootfs" "${WORK_ROOT}/packages" "${WORK_ROOT}/efiboot" "${WORK_ROOT}/iso" "$OUT" 2>/dev/null || true
	: >"${WORK_ROOT}/manifest/packages.txt" 2>/dev/null || log warn "manifest dir is not writable: ${WORK_ROOT}/manifest"
	rm -f "${WORK_ROOT}"/manifest/*.txt "${WORK_ROOT}"/manifest/*.json 2>/dev/null || true
	STAGE_TIMES=()
	log info "work: ${WORK_ROOT}   out: ${OUT}   jobs: ${JOBS}   dry-run: ${DRY_RUN:-0}"
	[ "${DRY_RUN:-0}" = "1" ] && log warn "dry run: no privileged or external command will be executed (logged to ${DRY_RUN_LOG})"
	return 0
}

build_summary() {
	local total=0 t
	for t in "${STAGE_TIMES[@]:-}"; do
		[ -n "$t" ] || continue
		total=$((total + ${t%s*} 2>/dev/null || 0))
	done
	printf 'stages: %s (total %ss)\n' "${STAGE_TIMES[*]:-}" "$total" >&2
}
