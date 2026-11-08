/* hedron-gamemode — the claim daemon, its client, and a shim library.
 *
 * SPDX-License-Identifier: MIT
 *
 * Protocol (one line per request, newline-terminated, ASCII):
 *   request   = verb SP [arg] LF
 *   verb      = "claim" | "release" | "query" | "list" | "status" | "ping" | "register"
 *   reply     = "ok" SP json LF | "err" SP message LF
 *
 * Why a socket and not D-Bus: the claim must work from a Proton launch script running in a
 * Steam Linux Runtime container, where the session bus is namespaced away but $XDG_RUNTIME_DIR
 * is bind-mounted. A file-system socket survives that; a bus name does not.
 *
 * The daemon holds a claim only while the claiming PID lives: it registers for
 * PR_SET_PDEATHSIG on the client connection, and additionally polls /proc/<pid>/stat's
 * start-time so a PID reuse cannot leave a stale claim pinned to the wrong process.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#define MAX_CLAIMS 64
#define SOCK_PATH_FMT "/run/user/%u/hedron/gamemode.sock"
#define SOCK_ENV "HEDRON_GAMEMODE_SOCK"
#define SOCK_MAX 100
#define LOG(...) do { fprintf(stderr, "gamemoded: " __VA_ARGS__); fputc('\n', stderr); } while (0)

struct claim {
	pid_t pid;
	unsigned long long start;	/* /proc/<pid>/stat field 22, to defeat PID reuse */
	int fd;			/* connection we answer release on (may be -1) */
	char app[128];
	long long when_ns;
	int refcount;
	int active;
};

static struct claim claims[MAX_CLAIMS];
static int verbose;

static long long now_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (long long)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

/* Start time of a process in the same units the kernel reports, so a recycled PID with a
 * different start time is obviously a different process. */
static unsigned long long proc_start(pid_t pid)
{
	char path[64], buf[4096];
	FILE *f;
	size_t n;

	snprintf(path, sizeof(path), "/proc/%d/stat", pid);
	f = fopen(path, "r");
	if (!f)
		return 0;
	n = fread(buf, 1, sizeof(buf) - 1, f);
	fclose(f);
	buf[n] = '\0';
	/* comm may contain spaces and parentheses: skip to the last ')'. */
	char *close = strrchr(buf, ')');

	if (!close)
		return 0;
	unsigned long long fields[30] = {0};
	int count = 0;
	char *p = close + 1;

	while (*p && count < 30) {
		while (*p == ' ')
			p++;
		fields[count++] = strtoull(p, &p, 10);
	}
	return fields[19];	/* starttime */
}

/* A zombie still answers kill(pid,0) and still has /proc/<pid>, but it is over: the game
 * process exited and only its parent's reap is pending. Counting it as alive would pin
 * "gaming" tuning to a machine that is back on the desktop. */
static int proc_alive(pid_t pid, unsigned long long expect_start)
{
	char path[64], buf[512];
	FILE *f;
	int alive;

	errno = 0;
	alive = (kill(pid, 0) == 0 || errno == EPERM);
	if (!alive)
		return 0;
	snprintf(path, sizeof(path), "/proc/%d/stat", pid);
	f = fopen(path, "r");
	if (!f)
		return 0;
	if (!fgets(buf, sizeof(buf), f)) {
		fclose(f);
		return 0;
	}
	fclose(f);
	char *close_paren = strrchr(buf, ')');

	if (close_paren && close_paren[1] == ' ' && close_paren[2] == 'Z')
		return 0;
	if (expect_start && proc_start(pid) != expect_start)
		return 0;
	return 1;
}

static struct claim *claim_find(pid_t pid)
{
	for (int i = 0; i < MAX_CLAIMS; i++)
		if (claims[i].active && claims[i].pid == pid)
			return &claims[i];
	return NULL;
}

static struct claim *claim_free_slot(void)
{
	for (int i = 0; i < MAX_CLAIMS; i++)
		if (!claims[i].active)
			return &claims[i];
	return NULL;
}

static int claim_purge_dead(void)
{
	int purged = 0;

	for (int i = 0; i < MAX_CLAIMS; i++) {
		struct claim *c = &claims[i];

		if (!c->active)
			continue;
		if (proc_alive(c->pid, c->start))
			continue;
		if (verbose)
			LOG("claim for pid %d (%s) expired", c->pid, c->app);
		memset(c, 0, sizeof(*c));
		purged++;
	}
	return purged;
}

static void json_escape(const char *in, char *out, size_t n)
{
	size_t j = 0;

	for (size_t i = 0; in[i] && j + 2 < n; i++) {
		if (in[i] == '"' || in[i] == '\\')
			out[j++] = '\\';
		out[j++] = (in[i] >= 32 && in[i] < 127) ? in[i] : ' ';
	}
	out[j] = '\0';
}

static int count_active(void);

/* Replies are always one JSON object per line: trivially parseable from shell (`jq -r`),
 * from Python (json.loads) and from the QML overlay, with no framing to get out of sync. */
static void reply_ok(int fd, struct claim *c)
{
	char app[256] = "";
	char buf[512];
	int n;

	if (c)
		json_escape(c->app, app, sizeof(app));
	if (c)
		n = snprintf(buf, sizeof(buf),
			     "{\"ok\":true,\"active\":true,\"clients\":%d,\"pid\":%d,\"app\":\"%s\","
			     "\"held_ms\":%lld}\n",
			     count_active(), (int)c->pid, app,
			     (long long)((now_ns() - c->when_ns) / 1000000LL));
	else
		n = snprintf(buf, sizeof(buf), "{\"ok\":true,\"active\":%s,\"clients\":%d}\n",
			     count_active() ? "true" : "false", count_active());
	if (n > 0)
		(void)!write(fd, buf, (size_t)n);
}

static void reply_err(int fd, const char *msg)
{
	char buf[256];
	int n = snprintf(buf, sizeof(buf), "{\"ok\":false,\"error\":\"%s\"}\n", msg);

	if (n > 0)
		(void)!write(fd, buf, (size_t)n);
}

static int count_active(void)
{
	int n = 0;

	for (int i = 0; i < MAX_CLAIMS; i++)
		if (claims[i].active)
			n++;
	return n;
}

static void handle_line(int fd, char *line)
{
	char verb[32] = "", arg[160] = "";
	pid_t pid;
	struct claim *c;

	if (sscanf(line, "%31s %159[^\n]", verb, arg) < 1) {
		reply_err(fd, "malformed");
		return;
	}
	pid = getpid();
	/* Read verbs must reflect reality *now*: a launcher that asks "is anyone claiming?"
	 * right after a game died should get 0, not a stale entry until the next request. */
	if (!strcmp(verb, "status") || !strcmp(verb, "query") || !strcmp(verb, "list"))
		claim_purge_dead();

	if (!strcmp(verb, "ping")) {
		char buf[96];
		int n = snprintf(buf, sizeof(buf), "{\"ok\":true,\"pong\":true,\"clients\":%d}\n", count_active());

		if (n > 0)
			(void)!write(fd, buf, (size_t)n);
		return;
	}
	if (!strcmp(verb, "status")) {
		char buf[512];
		int off = snprintf(buf, sizeof(buf), "{\"ok\":true,\"clients\":%d,\"entries\":[", count_active());

		for (int i = 0; i < MAX_CLAIMS && off < (int)sizeof(buf) - 80; i++) {
			if (!claims[i].active)
				continue;
			char app[256];

			json_escape(claims[i].app, app, sizeof(app));
			off += snprintf(buf + off, sizeof(buf) - off, "%s{\"pid\":%d,\"app\":\"%s\"}",
					off > 20 && buf[off - 1] != '[' ? "," : "",
					(int)claims[i].pid, app);
		}
		if (off < (int)sizeof(buf) - 3)
			off += snprintf(buf + off, sizeof(buf) - off, "]}\n");
		(void)!write(fd, buf, (size_t)off);
		return;
	}
	if (!strcmp(verb, "list") || !strcmp(verb, "query")) {
		claim_purge_dead();
		reply_ok(fd, claim_find(pid));
		return;
	}
	if (!strcmp(verb, "claim")) {
		pid_t client = pid;
		char app[128] = "unknown";

		/* `claim <pid> <app>` from the client, which knows its own pid; the daemon
		 * cannot see it over a dgram socket and SO_PEERCRED on seqpacket needs the
		 * accepted peer, which is the same process anyway. */
		int given = 0;

		if (arg[0])
			given = sscanf(arg, "%d %127s", (int *)&client, app);
		if (given < 1 || client <= 0)
			client = pid;
		c = claim_find(client);
		if (!c) {
			c = claim_free_slot();
			if (!c) {
				reply_err(fd, "too many claims");
				return;
			}
			c->active = 1;
			c->pid = client;
			c->start = proc_start(client);
			c->when_ns = now_ns();
			c->refcount = 0;
			snprintf(c->app, sizeof(c->app), "%s", app);
		}
		c->refcount++;
		if (verbose)
			LOG("claim by pid %d (%s), refcount %d", (int)client, app, c->refcount);
		/* Tell the supervisor it can now do the slow parts (GPU clocks, scheduler). */
		{
			char cmd[256];

			snprintf(cmd, sizeof(cmd),
				 "systemctl --user start hedron-flowstate-engage@%d.service 2>/dev/null || true",
				 (int)client);
			if (system(cmd) != 0 && verbose)
				LOG("engage helper unavailable (fine: the daemon polls too)");
		}
		reply_ok(fd, c);
		return;
	}
	if (!strcmp(verb, "release")) {
		int given = 0;
		pid_t client = pid;

		if (arg[0])
			given = sscanf(arg, "%d", (int *)&client);
		if (given < 1)
			client = pid;
		c = claim_find(client);
		if (!c) {
			reply_err(fd, "no claim for that pid");
			return;
		}
		if (--c->refcount <= 0) {
			if (verbose)
				LOG("releasing pid %d (%s)", (int)client, c->app);
			memset(c, 0, sizeof(*c));
		}
		reply_ok(fd, c->active ? c : NULL);
		return;
	}
	reply_err(fd, "unknown verb");
}

/* ------------------------------------------------------------------ daemon mode */
/* Resolve the socket: $HEDRON_GAMEMODE_SOCK wins, else $XDG_RUNTIME_DIR/hedron/gamemode.sock,
 * else /run/user/<uid>/… A plain default that fails in a weird container is worse than an
 * override we can point a test at. */
static void sock_path(char *out, size_t n)
{
	const char *env = getenv(SOCK_ENV);
	const char *xdg = getenv("XDG_RUNTIME_DIR");

	if (env && *env) {
		snprintf(out, n, "%s", env);
		return;
	}
	if (xdg && *xdg)
		snprintf(out, n, "%s/hedron/gamemode.sock", xdg);
	else
		snprintf(out, n, SOCK_PATH_FMT, (unsigned)getuid());
}

static int daemon_run(void)
{
	int fd;
	struct sockaddr_un addr = {0};
	char path[SOCK_MAX], dir[SOCK_MAX];

	sock_path(path, sizeof(path));
	snprintf(dir, sizeof(dir), "%s", path);
	char *slash = strrchr(dir, '/');

	if (slash) {
		*slash = '\0';
		if (mkdir(dir, 0700) && errno != EEXIST) {
			LOG("cannot create %s: %s", dir, strerror(errno));
			return 1;
		}
	}
	fd = socket(AF_UNIX, SOCK_SEQPACKET, 0);
	if (fd < 0) {
		LOG("socket: %s", strerror(errno));
		return 1;
	}
	addr.sun_family = AF_UNIX;
	snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", path);
	unlink(path);
	if (bind(fd, (struct sockaddr *)&addr, sizeof(addr))) {
		LOG("bind %s: %s", path, strerror(errno));
		return 1;
	}
	if (chmod(path, 0600))
		LOG("chmod %s: %s", path, strerror(errno));
	listen(fd, 32);
	LOG("listening on %s (pid %d)", path, (int)getpid());

	/* sd_notify is best-effort: this daemon is normally socket-activated, but a user can
	 * also run it by hand from a shell to debug something. */
	if (getenv("NOTIFY_SOCKET"))
		(void)!system("systemd-notify --ready 2>/dev/null || true");

	for (;;) {
		char buf[512];
		ssize_t n;
		int cfd = accept(fd, NULL, NULL);

		if (cfd < 0) {
			if (errno == EINTR)
				continue;
			LOG("accept: %s", strerror(errno));
			return 1;
		}
		struct timeval tv = { .tv_sec = 5, .tv_usec = 0 };

		setsockopt(cfd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
		n = recv(cfd, buf, sizeof(buf) - 1, 0);
		if (n > 0) {
			buf[n] = '\0';
			char *save = NULL;

			for (char *line = strtok_r(buf, "\n", &save); line;
			     line = strtok_r(NULL, "\n", &save))
				handle_line(cfd, line);
		}
		close(cfd);
		claim_purge_dead();
	}
	return 0;
}

/* ------------------------------------------------------------------ client mode */
static int send_line(const char *line)
{
	int fd;
	struct sockaddr_un addr = {0};
	char path[SOCK_MAX];
	char buf[4096];
	ssize_t n;

	sock_path(path, sizeof(path));
	fd = socket(AF_UNIX, SOCK_SEQPACKET, 0);
	if (fd < 0)
		return 1;
	addr.sun_family = AF_UNIX;
	snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", path);
	if (connect(fd, (struct sockaddr *)&addr, sizeof(addr))) {
		fprintf(stderr, "hgm: no gamemode daemon at %s (%s)\n"
			"      start it with: systemctl --user start hedron-gamemoded.service\n",
			path, strerror(errno));
		close(fd);
		return 1;
	}
	struct timeval tv = { .tv_sec = 10, .tv_usec = 0 };

	setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
	setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
	if (send(fd, line, strlen(line), 0) < 0) {
		perror("hgm: send");
		close(fd);
		return 1;
	}
	n = recv(fd, buf, sizeof(buf) - 1, 0);
	close(fd);
	if (n <= 0) {
		fprintf(stderr, "hgm: daemon gave no reply\n");
		return 1;
	}
	buf[n] = '\0';
	fputs(buf, stdout);
	return 0;
}

static int client(int argc, char **argv)
{
	char line[256];
	const char *verb = argc > 1 ? argv[1] : "status";

	if (!strcmp(verb, "query"))
		return send_line("query\n");
	if (!strcmp(verb, "status"))
		return send_line("status\n");
	if (!strcmp(verb, "ping"))
		return send_line("ping\n");
	if (!strcmp(verb, "start") || !strcmp(verb, "claim")) {
		snprintf(line, sizeof(line), "claim %d %s\n", (int)(argc > 2 ? atoi(argv[2]) : 0),
			 argc > 3 ? argv[3] : "cli");
		if (argc <= 2)
			snprintf(line, sizeof(line), "claim %d cli\n", (int)getpid());
		return send_line(line);
	}
	if (!strcmp(verb, "end") || !strcmp(verb, "release")) {
		snprintf(line, sizeof(line), "release %d\n", argc > 2 ? atoi(argv[2]) : (int)getpid());
		return send_line(line);
	}
	fprintf(stderr, "usage: hgm status|query|ping | hgm start PID APP | hgm end [PID]\n");
	return 2;
}

int main(int argc, char **argv)
{
	if (argc > 1 && (!strcmp(argv[1], "--daemon") || !strcmp(argv[1], "-d"))) {
		verbose = getenv("HEDRON_GAMEMODE_VERBOSE") != NULL;
		return daemon_run();
	}
	if (argc > 1 && !strcmp(argv[1], "--verbose")) {
		verbose = 1;
		memmove(argv + 1, argv + 2, sizeof(char *) * (size_t)(argc - 2));
		argc--;
		if (argc > 1 && !strcmp(argv[1], "--daemon"))
			return daemon_run();
	}
	return client(argc, argv);
}
