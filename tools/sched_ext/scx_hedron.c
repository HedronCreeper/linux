/* SPDX-License-Identifier: GPL-2.0 */
/*
 * scx_hedron — loader for the HedronOS focus-aware sched_ext scheduler.
 *
 * The kernel side (scx_hedron.bpf.c) is policy-agnostic: it has one map and two dispatch queues.
 * This program is where the policy arrives: it watches the focus file that hedron-gamemode writes
 * when a game takes focus, mirrors it into that map, and clears it when the process goes away.
 *
 * That split is the reason a distribution can ship a custom scheduler at all. When something is
 * wrong at 2 a.m., you can read /run/user/$UID/hedron/focus.json, or `bpftool map dump` the kernel
 * side, or run this with -p <pid> by hand — and none of those require a rebuild.
 *
 * Copyright (c) 2026 HedronOS contributors.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
#include <assert.h>
#include <libgen.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <scx/common.h>
#include "scx_hedron.bpf.skel.h"

#define HEDRON_FOCUS_VERSION	1

struct hedron_focus {
	__u32	pid;
	__u32	version;
	__u64	boost_slice;
	__u64	focus_weight;
	__u64	reserved[2];
};

const char help_fmt[] =
"A focus-aware sched_ext scheduler for the HedronOS desktop.\n"
"\n"
"See the top-level comment in scx_hedron.bpf.c for the scheduling design.\n"
"\n"
"Usage: %s [options]\n"
"\n"
"  -p PID          treat PID as the focus session instead of watching the focus file\n"
"  -f FILE         focus file to watch (default: $XDG_RUNTIME_DIR/hedron/focus.json)\n"
"  -b NSEC         slice length in ns given to the focus session (default: 4x kernel default)\n"
"  -w WEIGHT       vtime charge divisor for the focus session (default: 4)\n"
"  -i SECS         stats interval, 0 to disable printing (default: 2)\n"
"  -P DIR          pin the maps under DIR so `bpftool map dump` works while this runs\n"
"  -t              test mode: load, print the plan, unload (what hedron-doctor runs)\n"
"  -v              print libbpf debug messages\n"
"  -h              display this help and exit\n"
"\n"
"The focus file is written by hedron-gamemode and looks like:\n"
"  { \"pid\": 41288, \"name\": \"steamwebhelper\", \"focused\": true }\n"
"A missing file, \"focused\": false, or a PID that no longer exists all mean the same thing:\n"
"no focus, and the scheduler is a plain weighted-vtime scheduler.\n";

static volatile int exit_req;
static int verbose;
static const char *stat_names[6] = {
	"focus_local", "focus_dsq", "shared", "kicked", "preempt", "boost",
};

static int libbpf_print_fn(enum libbpf_print_level level, const char *format, va_list args)
{
	if (level == LIBBPF_DEBUG && !verbose)
		return 0;
	return vfprintf(stderr, format, args);
}

static void sigint_handler(int sig)
{
	exit_req = 1;
}

/*
 * Read the focus file. Hand-rolled on purpose: the producer is our own hedron-gamemode, the object
 * is four scalars, and a scheduler that cannot start because libjson-c is not installed at 2 a.m.
 * during an upgrade is worse than twenty lines of strstr. Unknown keys are ignored, a malformed
 * file is "no focus" (fail toward the plain scheduler, never toward a boosted one).
 */
static int focus_file_read(const char *path, pid_t *pid_out, bool *focused_out)
{
	char buf[4096];
	int fd, n;
	char *p;
	long v;

	*pid_out = 0;
	*focused_out = false;

	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return -errno;
	n = read(fd, buf, sizeof(buf) - 1);
	close(fd);
	if (n <= 0)
		return -EIO;
	buf[n] = '\0';

	p = strstr(buf, "\"pid\"");
	if (!p)
		return 0;					/* no focus recorded: fine */
	p = strchr(p, ':');
	if (!p)
		return -EINVAL;
	v = strtol(p + 1, &p, 10);
	if (v <= 0 || v > 0x7ffffff0)
		return -EINVAL;
	*pid_out = (pid_t)v;

	/* "focused": false means the game is running but not in the foreground — no boost. */
	p = strstr(buf, "\"focused\"");
	*focused_out = true;
	if (p) {
		p = strchr(p, ':');
		if (p && *p == ':' && strstr(p, "false") - p < 8)
			*focused_out = false;
	}
	return 0;
}

static void focus_write(int map_fd, const struct hedron_focus *f)
{
	static const __u32 zero;

	if (bpf_map_update_elem(map_fd, &zero, f, BPF_ANY) < 0)
		fprintf(stderr, "scx_hedron: failed to update the focus map: %s\n", strerror(errno));
}

static void read_stats(struct scx_hedron *skel, __u64 *stats)
{
	int nr_cpus = libbpf_num_possible_cpus();
	__u64 cnts[6][nr_cpus];
	__u32 idx;

	memset(stats, 0, sizeof(stats[0]) * 6);
	if (nr_cpus <= 0)
		return;

	for (idx = 0; idx < 6; idx++) {
		int ret, cpu;

		ret = bpf_map_lookup_elem(bpf_map__fd(skel->maps.stats), &idx, cnts[idx]);
		if (ret < 0)
			continue;
		for (cpu = 0; cpu < nr_cpus; cpu++)
			stats[idx] += cnts[idx][cpu];
	}
}

int main(int argc, char **argv)
{
	struct scx_hedron *skel;
	struct bpf_link *link;
	char focus_path[PATH_MAX];
	struct hedron_focus f = {};
	__u64 stats[6];
	pid_t pinned_pid = 0, cur_pid = 0;
	bool focused = false, test_only = false;
	const char *pin_dir = NULL;
	__s32 opt, map_fd;
	__u64 ecode;
	int interval = 2;

	libbpf_set_print(libbpf_print_fn);
	signal(SIGINT, sigint_handler);
	signal(SIGTERM, sigint_handler);

	f.focus_weight = 4;
	pinned_pid = 0;
	snprintf(focus_path, sizeof(focus_path), "%s/hedron/focus.json",
		 getenv("XDG_RUNTIME_DIR") ? : "/run/user");

	while ((opt = getopt(argc, argv, "p:f:b:w:i:P:tvh")) != -1) {
		switch (opt) {
		case 'p':
			pinned_pid = atoi(optarg);
			if (pinned_pid <= 0) {
				fprintf(stderr, "scx_hedron: -p wants a positive pid\n");
				return 1;
			}
			break;
		case 'f':
			snprintf(focus_path, sizeof(focus_path), "%s", optarg);
			break;
		case 'b':
			f.boost_slice = strtoull(optarg, NULL, 10);
			break;
		case 'w':
			f.focus_weight = strtoull(optarg, NULL, 10);
			if (!f.focus_weight) {
				fprintf(stderr, "scx_hedron: -w must be >= 1 (0 would mean infinite boost)\n");
				return 1;
			}
			break;
		case 'i':
			interval = atoi(optarg);
			break;
		case 'P':
			pin_dir = optarg;
			break;
		case 't':
			test_only = true;
			interval = 0;
			break;
		case 'v':
			verbose = true;
			break;
		default:
			fprintf(stderr, help_fmt, basename(argv[0]));
			return opt != 'h';
		}
	}

	/*
	 * Not a bug you can fix by retrying: without cpufreq performance hints the boost policy is
	 * a no-op, and a silent no-op in a gaming scheduler is worse than a refusal. (The scheduler
	 * itself still works, which is why this is a warning and not an exit.)
	 */
	if (access("/sys/devices/system/cpu/cpufreq/boost", F_OK) != 0)
		fprintf(stderr, "scx_hedron: no cpufreq boost interface; frequency hints will be ignored\n");

restart:
	skel = SCX_OPS_OPEN(hedron_ops, scx_hedron);

	/* Load first: the map fds we need only exist after the skeleton is loaded. */
	SCX_OPS_LOAD(skel, hedron_ops, scx_hedron, uei);
	map_fd = bpf_map__fd(skel->maps.focus);

	if (pin_dir) {
		if (mkdir(pin_dir, 0700) && errno != EEXIST)
			fprintf(stderr, "scx_hedron: cannot create %s: %s (maps stay unpinned)\n",
				pin_dir, strerror(errno));
		else if (bpf_object__pin_maps(skel->obj, pin_dir) < 0)
			fprintf(stderr, "scx_hedron: cannot pin maps under %s (continuing unpinned)\n", pin_dir);
		else
			printf("scx_hedron: maps pinned at %s\n", pin_dir);
	}

	/* Seed the map before attaching: a scheduler that starts with an empty map would let the
	 * very first frames of an already-running game go unboosted, which is the case a user
	 * notices ("it was smooth until I alt-tabbed back"). */
	if (pinned_pid) {
		f.pid = pinned_pid;
		f.version = HEDRON_FOCUS_VERSION;
	} else {
		if (focus_file_read(focus_path, &cur_pid, &focused) == 0 && cur_pid && focused) {
			f.pid = cur_pid;
			f.version = HEDRON_FOCUS_VERSION;
		}
	}
	focus_write(map_fd, &f);

	link = SCX_OPS_ATTACH(skel, hedron_ops, scx_hedron);

	if (test_only) {
		printf("scx_hedron: loaded and attached; focus pid=%u weight=%llu slice=%llu ns\n",
		       f.pid, (unsigned long long)f.focus_weight, (unsigned long long)f.boost_slice);
		goto out;
	}

	while (!exit_req && !UEI_EXITED(skel, uei)) {
		if (interval) {
			int i;

			read_stats(skel, stats);
			printf("hedron:");
			for (i = 0; i < 6; i++)
				printf(" %s=%llu", stat_names[i], (unsigned long long)stats[i]);
			printf(" queued[focus=%d shared=%d]\n",
			       scx_bpf_dsq_nr_queued(0x4845440000000001ULL),
			       scx_bpf_dsq_nr_queued(0x4845440000000002ULL));
			fflush(stdout);
		}

		if (pinned_pid) {
			/* -p: a fixed pid, cleared when it dies. Used by hedron-tune and by anyone
			 * reproducing a scheduling decision by hand. */
			if (kill(pinned_pid, 0) != 0 && f.pid) {
				f.pid = 0;
				focus_write(map_fd, &f);
				printf("scx_hedron: focus pid %d is gone, boosting nothing\n", pinned_pid);
			}
		} else {
			pid_t next = 0;
			bool now = false;

			if (focus_file_read(focus_path, &next, &now) < 0) {
				/* A truncated file (the writer was mid-update) must not be read as
				 * "no focus" and then as "focus" a second later. Treat it as
				 * "unchanged" and try again on the next tick. */
				goto sleep;
			}
			if (next && now && kill(next, 0) != 0)
				next = 0;			/* stale file: the game exited */
			if (next != f.pid) {
				f.pid = next;
				f.version = HEDRON_FOCUS_VERSION;
				focus_write(map_fd, &f);
				if (next)
					printf("scx_hedron: focus is pid %d\n", next);
				else
					printf("scx_hedron: focus cleared\n");
			}
		}
sleep:
		sleep(interval ? interval : 1);
	}

out:
	bpf_link__destroy(link);
	ecode = UEI_REPORT(skel, uei);
	scx_hedron__destroy(skel);

	if (!exit_req && UEI_ECODE_RESTART(ecode))
		goto restart;
	return 0;
}
