/* hedron-latency — measure scheduler latency the way a game feels it.
 *
 * SPDX-License-Identifier: MIT
 *
 * Why this exists: every "gaming tuning guide" ends with "now verify". On Linux the
 * usual verification is `cyclictest`, which needs RT priority, tells you nothing about
 * *your* game's thread class, and prints numbers most people cannot interpret.
 *
 * hedron-latency instead measures what a game main thread actually experiences: a
 * SCHED_OTHER (or optionally FIFO) thread that sleeps for one frame period, wakes, does
 * a tiny amount of work, and records the overshoot. Then it reports the distribution in
 * frame-time terms — "p99.9 = 0.42 ms = 5 % of a 16.7 ms frame at 60 Hz" — and an exit
 * code that a script (or `hedron-doctor`) can assert on. Run it before and after a
 * profile change and you have proof the change did something.
 *
 * It never sets anything itself; it only measures. Keeping the observer side-effect-free
 * is the whole point.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MAX_SAMPLES 2000000

struct opts {
	long iters;
	long period_us;
	int policy;		/* SCHED_OTHER or SCHED_FIFO */
	int prio;
	int cpu;		/* -1 = don't pin */
	int workload_ns;
	int quiet;
	const char *csv;
};

static inline uint64_t now_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static int cmp_u64(const void *a, const void *b)
{
	uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;

	return x < y ? -1 : (x > y ? 1 : 0);
}

static void period_ns_to_ts(long ns, struct timespec *ts)
{
	ts->tv_sec = ns / 1000000000L;
	ts->tv_nsec = ns % 1000000000L;
}

static void parse_args(int argc, char **argv, struct opts *o)
{
	o->iters = 3000;
	o->period_us = 16667;	/* 60 Hz */
	o->policy = SCHED_OTHER;
	o->prio = 1;
	o->cpu = -1;
	o->workload_ns = 2000;
	o->quiet = 0;
	o->csv = NULL;

	for (int i = 1; i < argc; i++) {
		const char *a = argv[i];

		if (!strcmp(a, "--help") || !strcmp(a, "-h")) {
			printf("usage: hedron-latency [-p us] [-n iters] [--rt [prio]] [--cpu N]\n"
			       "                  [--workload ns] [--quiet] [--json] [--csv FILE]\n\n"
			       "Measures wake-up latency as a game thread experiences it.\n"
			       "  -p, --period us   target frame period (default 16667 = 60 Hz)\n"
			       "  -n, --iters N       samples (default 3000)\n"
			       "  -r, --rt [PRIO]     use SCHED_FIFO instead of SCHED_OTHER\n"
			       "  -c, --cpu N         pin to CPU N\n"
			       "  -w, --workload NS     busy work per frame (default 2000 ns)\n"
			       "      --csv FILE        raw samples to FILE\n"
			       "Exit code: 0 if p99.9 <= period/4, 1 if worse, 2 on error.\n");
			exit(0);
		} else if (!strcmp(a, "--quiet") || !strcmp(a, "-q")) {
			o->quiet = 1;
		} else if (!strcmp(a, "--json")) {
			o->policy |= 1 << 20;	/* reuse: json flag bit */
		} else if (!strcmp(a, "-p") || !strcmp(a, "--period")) {
			if (++i >= argc) { fprintf(stderr, "missing value for %s\n", a); exit(2); }
			o->period_us = atol(argv[i]);
		} else if (!strcmp(a, "-n") || !strcmp(a, "--iters")) {
			if (++i >= argc) { fprintf(stderr, "missing value for %s\n", a); exit(2); }
			o->iters = atol(argv[i]);
		} else if (!strcmp(a, "--cpu") || !strcmp(a, "-c")) {
			if (++i >= argc) { fprintf(stderr, "missing value for %s\n", a); exit(2); }
			o->cpu = atoi(argv[i]);
		} else if (!strcmp(a, "--workload") || !strcmp(a, "-w")) {
			if (++i >= argc) { fprintf(stderr, "missing value for %s\n", a); exit(2); }
			o->workload_ns = atol(argv[i]);
		} else if (!strcmp(a, "--csv")) {
			if (++i >= argc) { fprintf(stderr, "missing value for %s\n", a); exit(2); }
			o->csv = argv[i];
		} else if (!strcmp(a, "-r") || !strcmp(a, "--rt")) {
			o->policy = SCHED_FIFO;
			if (i + 1 < argc && argv[i + 1][0] >= '0' && argv[i + 1][0] <= '9')
				o->prio = atoi(argv[++i]);
		} else {
			fprintf(stderr, "unknown argument: %s\n", a);
			exit(2);
		}
	}
	if (o->iters < 10)
		o->iters = 10;
	if (o->iters > MAX_SAMPLES)
		o->iters = MAX_SAMPLES;
	if (o->period_us < 200)
		o->period_us = 200;
}

int main(int argc, char **argv)
{
	struct opts o;
	uint64_t *s;
	uint64_t period_ns;
	struct timespec ts;
	int json = 0;

	parse_args(argc, argv, &o);
	if (o.policy & (1 << 20)) {
		json = 1;
		o.policy &= ~(1 << 20);
		if (o.policy != SCHED_FIFO)
			o.policy = SCHED_OTHER;
	}

	s = malloc(sizeof(*s) * (size_t)o.iters);
	if (!s) {
		fprintf(stderr, "hedron-latency: out of memory\n");
		return 2;
	}
	period_ns = (uint64_t)o.period_us * 1000ULL;

	if (o.cpu >= 0) {
		cpu_set_t set;

		CPU_ZERO(&set);
		CPU_SET(o.cpu, &set);
		if (sched_setaffinity(0, sizeof(set), &set))
			fprintf(stderr, "hedron-latency: pinning to cpu%d failed: %s\n",
				o.cpu, strerror(errno));
	}
	if (o.policy == SCHED_FIFO) {
		struct sched_param sp = { .sched_priority = o.prio };

		if (sched_setscheduler(0, SCHED_FIFO, &sp))
			fprintf(stderr,
				"hedron-latency: SCHED_FIFO refused (%s) — measuring as SCHED_OTHER.\n"
				"  run as root, or check /proc/sys/kernel/sched_rt_runtime_us\n",
				strerror(errno));
	}

	/* Warm up: first iterations include page faults and CPU frequency ramp. */
	for (int i = 0; i < 50; i++) {
		period_ns_to_ts((long)period_ns, &ts);
		nanosleep(&ts, NULL);
	}

	uint64_t start = now_ns();

	for (long i = 0; i < o.iters; i++) {
		uint64_t due = start + period_ns * (uint64_t)(i + 1);
		uint64_t busy_start, busy_end;
		volatile uint64_t sink = 0;

		busy_start = now_ns();
		while (now_ns() - busy_start < (uint64_t)o.workload_ns)
			sink += i;	/* a tiny, honest amount of work */
		busy_end = now_ns();

		long sleep_ns = (long)(due - busy_end);

		if (sleep_ns <= 0) {
			s[i] = 0;	/* we are ahead of schedule; overshoot 0 */
			continue;
		}
		period_ns_to_ts(sleep_ns, &ts);
		if (nanosleep(&ts, NULL) && errno != EINTR) {
			fprintf(stderr, "hedron-latency: nanosleep: %s\n", strerror(errno));
			return 2;
		}
		s[i] = now_ns() - due;	/* overshoot past the deadline */
	}
	uint64_t elapsed = now_ns() - start;

	qsort(s, (size_t)o.iters, sizeof(*s), cmp_u64);

	uint64_t total = 0, maxv = s[o.iters - 1];

	for (long i = 0; i < o.iters; i++)
		total += s[i];

	/* Percentiles in ns; everything downstream reports ms and compares against a
	 * fraction of the frame period, so keep one unit and convert at print time. */
#define PCTL_NS(p) ((double)s[(size_t)((double)o.iters * (p)) < (size_t)o.iters ? \
			       (size_t)((double)o.iters * (p)) : (size_t)o.iters - 1])
#define NS_TO_MS(v) ((v) / 1e6)
	double p50 = NS_TO_MS(PCTL_NS(0.50)), p90 = NS_TO_MS(PCTL_NS(0.90));
	double p99 = NS_TO_MS(PCTL_NS(0.99)), p999 = NS_TO_MS(PCTL_NS(0.999));
	double mx = NS_TO_MS((double)maxv);
	double avg = NS_TO_MS((double)total / (double)o.iters);
	double period_ms = (double)o.period_us / 1e3;
	int ok = p999 <= period_ms / 4.0;

	if (o.csv) {
		FILE *f = fopen(o.csv, "w");

		if (f) {
			for (long i = 0; i < o.iters; i++)
				fprintf(f, "%llu\n", (unsigned long long)(s[i] / 1000ULL));
			fclose(f);
		} else {
			fprintf(stderr, "hedron-latency: cannot write %s: %s\n", o.csv, strerror(errno));
		}
	}

	if (json) {
		printf("{\"period_us\":%ld,\"iters\":%ld,\"policy\":\"%s\",\"cpu\":%d,"
		       "\"avg_us\":%.3f,\"p50_us\":%.3f,\"p90_us\":%.3f,\"p99_us\":%.3f,"
		       "\"p999_us\":%.3f,\"max_us\":%.3f,\"elapsed_ms\":%.1f,"
		       "\"over_period_pct\":%.2f,\"verdict\":\"%s\"}\n",
		       o.period_us, o.iters, o.policy == SCHED_FIFO ? "SCHED_FIFO" : "SCHED_OTHER",
		       o.cpu, avg * 1e3, p50 * 1e3, p90 * 1e3, p99 * 1e3, p999 * 1e3, mx * 1e3,
		       elapsed / 1e6, p999 / period_ms * 100.0, ok ? "good" : "bad");
		return ok ? 0 : 1;
	}

	if (!o.quiet) {
		if (o.cpu >= 0)
			printf("hedron-latency: %ld samples, %.2f ms period, policy %s, pinned to cpu%d\n",
			       o.iters, period_ms, o.policy == SCHED_FIFO ? "SCHED_FIFO" : "SCHED_OTHER", o.cpu);
		else
			printf("hedron-latency: %ld samples, %.2f ms period, policy %s\n",
			       o.iters, period_ms, o.policy == SCHED_FIFO ? "SCHED_FIFO" : "SCHED_OTHER");
		printf("  overshoot past the wake-up deadline:\n");
		printf("    avg   %8.3f ms\n", avg);
		printf("    p50   %8.3f ms\n", p50);
		printf("    p90   %8.3f ms\n", p90);
		printf("    p99   %8.3f ms\n", p99);
		printf("    p99.9 %8.3f ms  (%.1f%% of a frame)\n", p999, p999 / period_ms * 100.0);
		printf("    max   %8.3f ms\n", mx);
		printf("  verdict: %s (target: p99.9 <= %.3f ms)\n",
		       ok ? "GOOD — the scheduler is not your bottleneck" :
			    "BAD — something is stealing frames; try `hedron-tune apply gaming` and close browsers",
		       period_ms / 4.0);
	}
	free(s);
	return ok ? 0 : 1;
}
