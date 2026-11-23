/*
 * gdb's gcore loses the stacks of all threads but the main one with
 * glibc 2.42+ on Linux 6.13+ (docs/upstream-gdb-gcore-guard-pages.md).
 *
 *   cc -O0 -g -pthread -o gcore-guard-pages gcore-guard-pages.c
 *   ./gcore-guard-pages > out & sleep 1
 *   gdb -batch -p "$(awk '/READY/ {print $2}' out)" -ex 'gcore core.test'
 *   grep -a -o 'STACK-MARK-T[1-4]' core.test | sort -u  # 4 if kept, none if lost
 *   gdb -batch ./gcore-guard-pages core.test -ex 'thread apply all bt'
 *
 * Each of the four threads keeps a marker in a buffer on its stack.
 */
#define _GNU_SOURCE
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <sys/prctl.h>
#include <unistd.h>

static pthread_barrier_t ready;

static void *
thread_f(void *arg)
{
	volatile char buf[16384];
	snprintf((char *)buf, sizeof(buf), "STACK-MARK-T%ld", (long)arg);
	printf("T%ld buf=%p\n", (long)arg, (void *)buf);
	fflush(stdout);
	pthread_barrier_wait(&ready);
	for (;;)
		pause();
}

int
main(void)
{
	pthread_t t;
	/* Let gdb attach without root under Yama's ptrace_scope 1. */
	prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY, 0, 0, 0);
	pthread_barrier_init(&ready, NULL, 5);
	for (long i = 1; i <= 4; i++)
		pthread_create(&t, NULL, thread_f, (void *)i);
	pthread_barrier_wait(&ready);
	printf("READY %d\n", getpid());
	fflush(stdout);
	for (;;)
		pause();
}
