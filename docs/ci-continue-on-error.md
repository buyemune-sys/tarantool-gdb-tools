# Dropping continue-on-error in CI (issue #27)

**Status: nothing dropped yet.** `continue-on-error` lets a workflow show "success" while some of its jobs fail. Every workflow got it while its jobs were new. Each one should lose it once its jobs are reliably green. The decision is in [#27](https://github.com/buyemune-sys/tarantool-gdb-tools/issues/27).

## Where it is now

| Workflow | `continue-on-error` on | Line |
|---|---|---|
| `test.yml` | the arm64 jobs | 37 |
| `dbg.yml` | the arm64 jobs | 38 |
| `dbg.yml` | two steps that run the extensions of the release, for the step summary | 84, 111 |
| `cores.yml` | every job | 29 |
| `lldb.yml` | every job | 37 |
| `source.yml` | every job | 32 |
| `manifest.yml` | nothing | — |

The amd64 jobs of `test.yml` and `dbg.yml` never had it, so a failure there turns the workflow red.

## State on master

| Workflow | 1cf27c2 | 2df44cb | Still failing, and why |
|---|---|---|---|
| `test.yml` | 11/14 (run 45178558742) | 12/14 (run 45182740347) | amd64: crash shapes in a plain Lua fiber ([#33](https://github.com/buyemune-sys/tarantool-gdb-tools/issues/33)) on 3.3.5 and 3.9.0 at 2df44cb, on 3.5.2 at 1cf27c2. 1cf27c2 also: TestTimer with JIT on ([#8](https://github.com/buyemune-sys/tarantool-gdb-tools/issues/8)) on 3.8.2 amd64 and 3.9.0 arm64 |
| `dbg.yml` | 20/20 (45178558700) | 20/20 (45182740802) | — |
| `cores.yml` | 7/7 (45178558682) | 7/7 (45182740764) | — |
| `lldb.yml` | 17/40 (45178558688) | 34/40 (45182740552) | arm64, Tarantool 3.7–3.9, lldb 18 and 22: no thread-local `tarantool_L` ([#30](https://github.com/buyemune-sys/tarantool-gdb-tools/issues/30)). Every amd64 job is green since 2df44cb |
| `source.yml` | 2/2 (45178558801) | 2/2 (45182740334) | — |
| `manifest.yml` | 1/1 | 1/1 | — |

Recent history of the arm64 jobs of `test.yml`:

| Commit | Run | arm64 |
|---|---|---|
| 2df44cb | 45182740347 | 7/7 |
| 1cf27c2 | 45178558742 | 6/7, #8 |
| 146e82b | 45162429950 | 6/7, #8 |
| fd49ba7 | 45154576804 | 0/7, the `[PAC]` mark of gdb (fixed in 6541b8a) |
| 50c6044, nightly | 45160121055 | 7/7 at the nightly levels (200 seeds, 500 stops) |

The scheduled runs start late: the only one so far, `test.yml`'s 02:23 UTC cron, ran at 10:53 UTC. The crons of `dbg.yml` (02:43), `lldb.yml` and `source.yml` (02:53) and `cores.yml` (03:13) have not run yet.

## A rule for dropping it

A workflow, or a part of its matrix, loses `continue-on-error` when all of these hold:
- its jobs are green on the last three push runs and on one scheduled run, which uses the full seeds and stops;
- it has no open `flaky` issue.

Dropping it is a one-line change per workflow, in its own commit, which names the runs that met the rule.

The two informational steps of `dbg.yml` keep it for good. They run the extension files of each release, which are expected to fail: that difference is the point of the universal files, and the step summary shows it.

## Recommendation

| Workflow | When to drop | What it waits for |
|---|---|---|
| `cores.yml` | after its first green scheduled run | one more green push run |
| `dbg.yml`, arm64 | after its first green scheduled run | one more green push run |
| `source.yml` | after its first green scheduled run | one more green push run; that run also checks that the build cache is hit |
| `test.yml`, arm64 | after #8 is fixed | three green push runs. #8 failed 2 of the last 4 runs on arm64, and also failed on amd64 |
| `test.yml`, amd64 | — (it has none) | #33 first: it makes master red now |
| `lldb.yml` | split the matrix: amd64 after the next two green push runs, arm64 after #30 | #31: the timeout did not recur, but its cause is unknown |

Fix #33 first, then #8: they are the only failures left in jobs without `continue-on-error`.
