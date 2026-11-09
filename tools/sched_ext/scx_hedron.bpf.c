// SPDX-License-Identifier: GPL-2.0
/*
 * scx_hedron — a focus-aware scheduler for a desktop that also plays games.
 *
 * HedronOS builds this scheduler and ships it in the kernel package. The idea is small enough to
 * state in one sentence: *the thing the user is looking at should not wait behind the thing they
 * started in the background.* Everything else here is mechanics for that promise.
 *
 * Why this is a scheduler and not a nice-word written to sched_setscheduler()
 * -----------------------------------------------------------------------------
 * A game's frame loop is a chain of short runnable bursts on a handful of threads (main, render,
 * a couple of worker threads, the compositor's present). What ruins frame pacing on Linux is
 * rarely raw CPU starvation — it is *placement and queueing*: a compile thread or a btrfs
 * writeback worker that happens to be first in the runqueue on the core the render thread wakes
 * on, or the scheduler migrating the render thread to a core in a deeper C-state, or the load
 * balancer walking it onto the sibling hyperthread mid-frame.
 *
 * Cgroups and CPUSETs cannot express that, because the interesting unit is not a process: it is
 * "the session the human is currently inside". That is exactly what a sched_ext ops implementation
 * can see — select_cpu() is asked a question at every wakeup, and the answer can depend on who is
 * waking up.
 *
 * How it works
 * ------------
 * Two dispatch queues:
 *
 *   FOCUS_DSQ   the threads of the focus session (the game), FIFO, with a longer slice
 *   SHARED_DSQ  everything else, ordered by weighted virtual time
 *
 * "Focus" is a single record in a BPF map that userspace owns. hedron-gamemode writes
 * /run/user/$UID/hedron/focus.json when a game starts and clears it when the game exits;
 * scx_hedron(8) watches that file and pushes it into the map. The scheduler therefore never has to
 * guess what the user is playing, and the policy stays a userspace file you can cat, edit and
 * test — the mechanism stays in BPF where it can act on every wakeup.
 *
 * The focus threads get three things:
 *
 *   1. A wake-up target chosen from an idle CPU first (scx_bpf_select_cpu_dfl), so a frame does not
 *      queue behind a stranger; when nothing is idle they go to FOCUS_DSQ, which is drained before
 *      SHARED_DSQ in dispatch(), so they are next off the queue rather than next in line.
 *   2. A longer slice (boost_slice, default 4x SCX_SLICE_DFL). Preempting a render thread at 2 ms
 *      to run a kworker for 2 ms is how you lose a frame; the cost of a long slice for a *focus*
 *      task is bounded because non-focus work is still charged against global vtime.
 *   3. A cheaper clock. Non-focus tasks accumulate dsq_vtime at full rate, focus tasks at
 *      1/focus_weight. That is the whole fairness argument: a background build gets starved for at
 *      most as much virtual time as the interactive budget allows, and returns to parity the moment
 *      the game ends. It also degrades to plain global vtime scheduling with one map write
 *      (focus.pid = 0), which is the mode `hedron-tune apply --profile quiet` selects.
 *
 * cpuperf is set to max on a CPU while it runs a focus task, and back to SCX_CPUPERF_ONE when that
 * task leaves. Not 0 and not "leave it": sched_ext is the component that *knows* when the desktop
 * is idle-looking-but-busy, which is precisely when the governor needs a hint.
 *
 * Deliberately absent
 * -------------------
 * No per-CPU runqueues with custom balancing (that is a different scheduler's job), no latency
 * prediction, no "boost for N ms after input" (the compositor, not the kernel, knows about input,
 * and FlowState already turns focus on and off). A scheduler in a distribution has to be
 * explainable in one page and debuggable with one map.
 *
 * Copyright (c) 2026 HedronOS contributors.
 *
 * Based on the API and structure of scx_simple and scx_qmap from tools/sched_ext:
 *   Copyright (c) 2022 Meta Platforms, Inc. and affiliates.
 *   Copyright (c) 2022 Tejun Heo <tj@kernel.org>
 *   Copyright (c) 2022 David Vernet <dvernet@meta.com>
 */
#include <scx/common.bpf.h>
#include <scx/compat.bpf.h>

char _license[] SEC("license") = "GPL";

#define FOCUS_DSQ	((u64)0x4845440000000001)	/* "HED\x01" — built-in DSQ ids are reserved */
#define SHARED_DSQ	((u64)0x4845440000000002)

/*
 * The focus record. One entry, written by the loader, read by every wakeup path. Padded to a
 * power of two and versioned, because a map a userspace tool updates out from under a running
 * scheduler is a file on disk as far as `bpftool map` is concerned — and someone *will* poke it.
 */
struct hedron_focus {
	u32	pid;		/* 0 == nobody is the focus: pure weighted-vtime behaviour */
	u32	version;	/* HEDRON_FOCUS_VERSION, checked by the loader */
	u64	boost_slice;	/* ns to give a focus task; 0 means "use 4 * SCX_SLICE_DFL" */
	u64	focus_weight;	/* divisor on vtime charging; 0 means 4 */
	u64	reserved[2];
};

#define HEDRON_FOCUS_VERSION	1

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, u32);
	__type(value, struct hedron_focus);
	__uint(pinning, LIBBPF_PIN_BY_NAME);
} focus SEC(".maps");

/* Counters the loader prints once a second, and `hedron-doctor` reads from the same skeleton-free
 * path (a pinned map, so a crashed loader does not take the numbers with it). */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(key_size, sizeof(u32));
	__uint(value_size, sizeof(u64));
	__uint(max_entries, 6);		/* [focus_local, focus_dsq, shared, kicked, preempt, boost_set] */
} stats SEC(".maps");

enum stat_idx {
	STAT_FOCUS_LOCAL,
	STAT_FOCUS_DSQ,
	STAT_SHARED,
	STAT_KICKED,
	STAT_PREEMPT,
	STAT_BOOST,
	STAT_NR,
};

static u64 vtime_now;

UEI_DEFINE(uei);

static __always_inline void stat_inc(u32 idx)
{
	u64 *cnt_p = bpf_map_lookup_elem(&stats, &idx);

	if (cnt_p)
		(*cnt_p)++;
}

/*
 * Read the focus record. A NULL return means "no focus": every callsite must then behave exactly
 * like the plain vtime scheduler, because that is the configuration most machines will run in most
 * of the time (no game focused, or none running at all).
 */
static __always_inline struct hedron_focus *focus_get(void)
{
	static const u32 zero = 0;
	struct hedron_focus *f;

	f = bpf_map_lookup_elem(&focus, &zero);
	if (!f || !f->pid)
		return NULL;
	if (f->version != HEDRON_FOCUS_VERSION)
		return NULL;
	return f;
}

static __always_inline bool is_focus_task(struct hedron_focus *f, const struct task_struct *p)
{
	return f && BPF_CORE_READ(p, pid) == f->pid;
}

static __always_inline u64 focus_slice(struct hedron_focus *f)
{
	if (f && f->boost_slice)
		return f->boost_slice;
	return 4 * SCX_SLICE_DFL;
}

static __always_inline u64 focus_weight(struct hedron_focus *f)
{
	if (f && f->focus_weight)
		return f->focus_weight;
	return 4;
}

s32 BPF_STRUCT_OPS(hedron_select_cpu, struct task_struct *p, s32 prev_cpu, u64 wake_flags)
{
	struct hedron_focus *f = focus_get();
	bool is_idle = false;
	s32 cpu;

	cpu = scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &is_idle);
	if (!is_idle)
		return cpu;

	/*
	 * Waking onto an idle CPU is the fast path and we want to commit it here rather than leave
	 * it for dispatch(): a task dispatched to SCX_DSQ_LOCAL runs without a second round trip,
	 * which is the difference between a wakeup that lands in ~20 us and one that lands behind
	 * whatever the CPU's queue already holds.
	 */
	if (is_focus_task(f, p)) {
		stat_inc(STAT_FOCUS_LOCAL);
		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, focus_slice(f), 0);
	} else {
		stat_inc(STAT_FOCUS_LOCAL);	/* reused as "idle fast path, non-focus" */
		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, SCX_SLICE_DFL, 0);
	}
	return cpu;
}

void BPF_STRUCT_OPS(hedron_enqueue, struct task_struct *p, u64 enq_flags)
{
	struct hedron_focus *f = focus_get();

	if (is_focus_task(f, p)) {
		stat_inc(STAT_FOCUS_DSQ);
		/*
		 * FOCUS_DSQ is FIFO. Giving the focus session a priority queue would be a lie:
		 * there is nothing inside it that should preempt anything else in it, and a
		 * vtime-ordered focus queue would let a chatty helper thread in the same game
		 * push a render thread back.
		 */
		scx_bpf_dsq_insert(p, FOCUS_DSQ, focus_slice(f), enq_flags);
		return;
	}

	stat_inc(STAT_SHARED);
	{
		u64 vtime = p->scx.dsq_vtime;

		/*
		 * An idling task must not bank an unbounded head start. One slice of credit is
		 * enough to make "who just woke up" win, and no more than that.
		 */
		if (time_before(vtime, vtime_now - SCX_SLICE_DFL))
			vtime = vtime_now - SCX_SLICE_DFL;
		scx_bpf_dsq_insert_vtime(p, SHARED_DSQ, SCX_SLICE_DFL, vtime, enq_flags);
	}
}

void BPF_STRUCT_OPS(hedron_dispatch, s32 cpu, struct task_struct *prev)
{
	if (scx_bpf_dsq_move_to_local(FOCUS_DSQ, 0))
		return;
	scx_bpf_dsq_move_to_local(SHARED_DSQ, 0);
}

/*
 * A focus task was just preempted or is about to run and the CPU has nothing queued: kick it, so
 * the wakeup does not wait for the next scheduler tick on a CPU that is in a deep idle state.
 */
void BPF_STRUCT_OPS(hedron_update_idle, s32 cpu, bool idle)
{
	struct hedron_focus *f;

	if (!idle)
		return;
	f = focus_get();
	if (!f)
		return;
	if (scx_bpf_dsq_nr_queued(FOCUS_DSQ) <= 0)
		return;
	stat_inc(STAT_KICKED);
	scx_bpf_kick_cpu(cpu, SCX_KICK_IDLE);
}

void BPF_STRUCT_OPS(hedron_running, struct task_struct *p)
{
	struct hedron_focus *f = focus_get();

	if (time_before(vtime_now, p->scx.dsq_vtime))
		vtime_now = p->scx.dsq_vtime;

	if (!is_focus_task(f, p))
		return;

	stat_inc(STAT_BOOST);
	/*
	 * Maximum frequency while the focus session runs on this CPU. Set on every dispatch rather
	 * than once per focus change: CPUs move, and the cost is a per-cpu store in a path that is
	 * already writing the same class of state.
	 *
	 * cpuperf is a __weak ksym — a kernel built without CONFIG_SCHED_SMT-class cpufreq hooks
	 * has no such symbol, and a BPF program that references a missing ksym fails to load rather
	 * than degrading. bpf_ksym_exists() is the compat idiom this tree uses.
	 */
	if (bpf_ksym_exists(scx_bpf_cpuperf_set))
		scx_bpf_cpuperf_set(scx_bpf_task_cpu(p), scx_bpf_cpuperf_cap(scx_bpf_task_cpu(p)));
}

void BPF_STRUCT_OPS(hedron_stopping, struct task_struct *p, bool runnable)
{
	struct hedron_focus *f = focus_get();
	u64 delta;

	if (is_focus_task(f, p)) {
		/* The focus task charges its clock at 1/focus_weight — see the header comment. */
		delta = scale_by_task_weight_inverse(p, (SCX_SLICE_DFL - p->scx.slice) / focus_weight(f));
	} else {
		delta = scale_by_task_weight_inverse(p, SCX_SLICE_DFL - p->scx.slice);
	}
	scx_bpf_task_set_dsq_vtime(p, p->scx.dsq_vtime + delta);

	/*
	 * Back to neutral, not to minimum: "the user is not looking at this CPU any more" is a
	 * reason to stop boosting, not a reason to hold a core down while a browser renders the
	 * next tab.
	 */
	if (is_focus_task(f, p) && bpf_ksym_exists(scx_bpf_cpuperf_set))
		scx_bpf_cpuperf_set(scx_bpf_task_cpu(p), SCX_CPUPERF_ONE);
}

s32 BPF_STRUCT_OPS_SLEEPABLE(hedron_init)
{
	int ret;

	ret = scx_bpf_create_dsq(FOCUS_DSQ, -1);
	if (ret) {
		scx_bpf_error("failed to create FOCUS_DSQ (%d)", ret);
		return ret;
	}
	ret = scx_bpf_create_dsq(SHARED_DSQ, -1);
	if (ret) {
		scx_bpf_error("failed to create SHARED_DSQ (%d)", ret);
		return ret;
	}
	return 0;
}

void BPF_STRUCT_OPS(hedron_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(hedron_ops,
	       .select_cpu		= (void *)hedron_select_cpu,
	       .enqueue			= (void *)hedron_enqueue,
	       .dispatch		= (void *)hedron_dispatch,
	       .update_idle		= (void *)hedron_update_idle,
	       .running			= (void *)hedron_running,
	       .stopping		= (void *)hedron_stopping,
	       .init			= (void *)hedron_init,
	       .exit			= (void *)hedron_exit,
	       .name			= "hedron");
