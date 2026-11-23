/*
 * lldb cannot evaluate an expression that needs memory in a process with
 * a pending signal (docs/upstream-lldb-pending-signal.md).
 *
 *   cc -O0 -g -o lldb-pending-signal lldb-pending-signal.c
 *   lldb -b -o 'breakpoint set -n stop_here' -o run \
 *        -o 'expression -- (int)stop_here(41)' ./lldb-pending-signal
 *
 * A 10 ms ITIMER_REAL, as Tarantool's low-resolution clock has, keeps a
 * SIGALRM pending while the process is stopped.  Built with -DNO_TIMER
 * the same expression works.
 */
#include <signal.h>
#include <stdio.h>
#include <sys/time.h>
#include <unistd.h>

static volatile long ticks;

static void
on_alrm(int sig)
{
	(void)sig;
	ticks++;
}

int
stop_here(int x)
{
	return x + (int)ticks;
}

int
main(void)
{
#ifndef NO_TIMER
	struct sigaction sa = {0};
	sa.sa_handler = on_alrm;
	sigaction(SIGALRM, &sa, NULL);
	struct itimerval it = {{0, 10000}, {0, 10000}};
	setitimer(ITIMER_REAL, &it, NULL);
	usleep(50000);
#endif
	printf("%d\n", stop_here(1));
	return 0;
}
