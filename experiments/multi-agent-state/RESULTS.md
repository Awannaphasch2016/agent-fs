# Multi-agent filesystem experiment

Recommended stack: **S0 — Dagger Changesets**

S0 is the smallest stack that kept agents off a shared mutable filesystem, rejected conflicting publishes, merged independent work, and promoted a new canonical directory without touching the previous one. Container Use, Open Orchestrator, JuiceFS, and lakeFS were not installed. No later test showed a requirement they would satisfy that S0 missed.

## Environment

Verified on this run:

| Piece | Version / fact |
| --- | --- |
| Host | Linux `cursor` 6.12.94+ x86_64, one VM |
| Dagger CLI | 0.21.10 |
| Dagger engine | `registry.dagger.io/engine:v0.21.10`, container `dagger-engine-v0.21.10` |
| Python SDK | `dagger-io==0.21.10` |
| Container runtime | Podman 4.9.3, rootful (`sudo`) |
| Runner | `_EXPERIMENTAL_DAGGER_RUNNER_HOST=container+podman://dagger-engine-v0.21.10` |
| Agent image | `docker.io/library/alpine:3.20`, imported from `/tmp/alpine-oci-plat.tar` |
| Second physical host | unavailable |
| S3 / R2 credentials | none |

The engine container's resolver (`10.87.0.1`) timed out looking up Docker Hub. Alpine was pulled on the host, saved as an OCI archive, given an explicit `linux/amd64` platform on the index, and imported with `Container.import_`. That constraint is environmental. It does not change changeset merge behavior.

A Depot token was supplied with the task and left unused. Depot runs remote builders and agent sandboxes. It is not the Dagger Changeset API under test.

## Reproduce

```bash
export PATH="$HOME/.local/bin:$PATH"
export _EXPERIMENTAL_DAGGER_RUNNER_HOST='container+podman://dagger-engine-v0.21.10'
export DAGGER_NO_NAG=1
export ALPINE_TAR=/tmp/alpine-oci-plat.tar
dagger run --progress plain python3 experiments/multi-agent-state/s0_dagger.py \
  --out experiments/multi-agent-state/s0-results.json
```

The script is `experiments/multi-agent-state/s0_dagger.py`. Structured results are `s0-results.json`. Git's conflict text from the engine progress stream is `s0-engine-excerpt.log`. The two patches T3 wrote and re-read are `t3-a.patch` and `t3-b.patch`.

## State model

S0 is an isolated-state model. Each agent receives the same immutable `Directory` inside its own container. The container returns a new directory only if its exec exits 0. A `Changeset` is the diff from the base. Promotion is `base.with_changes(merged)`, which yields a new directory. The base object stays at V1.

```
Base V1 (immutable Directory)
├── Container A → Agent A → Directory A → Changeset A
└── Container B → Agent B → Directory B → Changeset B
                      Changeset A + Changeset B
                               ↓
                    with_changeset(on_conflict=…)
                               ↓
                         canonical V2
```

Conflict strategies exercised in T2:

| Strategy | What the run did |
| --- | --- |
| `FAIL` (default) | Git 3-way merge. Same line aborts. Canonical base stays `value = 1`. |
| `FAIL_EARLY` | Rejects before the git merge when the same path is modified in both changesets. |
| `LEAVE_CONFLICT_MARKERS` | Writes `<<<<<<< HEAD` / `=======` / `>>>>>>> theirs` into the merged file. |
| `PREFER_OURS` | Keeps the left changeset (`value = 2`). |
| `PREFER_THEIRS` | Keeps the right changeset (`value = 3`). |
| `with_changesets` | Octopus merge. Only `FAIL` and `FAIL_EARLY` exist on that API. |

`PREFER_OURS` and `PREFER_THEIRS` drop the other side only when the caller selects them. The default does not.

## Pass / fail

| Test | S0 | S1 Container Use | S2 Open Orchestrator | S3 JuiceFS + object storage | S4 lakeFS + S3 |
| --- | --- | --- | --- | --- | --- |
| T1 same container, two writers | fail | not run | not run | not run | not run |
| T2 separate containers | pass | not run | not run | not run | not run |
| T3 durable artifact outside the container | pass (git patch) | not run | not run | not run | not run |
| T4 separate machines | partial | not run | not run | not run | not run |
| T5 stale writer | pass | not run | not run | not run | not run |
| T6 crash during modification | pass | not run | not run | not run | not run |
| T7 independent concurrent edits | pass | not run | not run | not run | not run |
| T8 isolated work, explicit promotion | pass | not run | not run | not run | not run |
| T9 four-agent merge | pass | not run | not run | not run | not run |

S1–S4 are `not run` on purpose. After T2, T5, T6, T7, T8, and T9 passed, the next component had no failed requirement to repair.

## What each S0 test did

Wall times are from the connected session in `s0-results.json`. Several execs were cache hits, so these numbers are API time against a warm engine, not a cold image pull.

**T1 — fail, 14 ms.** Two background writers in one container both read `counter.txt` and then overwrite it. Final body was `value = 3`. The other write disappeared. Exit was success. This is a shared mutable filesystem. S0 does not claim isolation inside one container.

**T2 — pass, 272 ms.** Two containers started from `value = 1`. During the edits the base was still `value = 1`. Container A saw `value = 2`. Container B saw `value = 3`. `FAIL` and `FAIL_EARLY` both rejected the merge. Markers produced:

```
<<<<<<< HEAD
value = 2
=======
value = 3
>>>>>>> theirs
```

`FAIL_EARLY` returned in 7.7 ms with `conflict between changesets at path "counter.txt": path modified in both changesets`. The git `FAIL` path took 61 ms and reported `CONFLICT (content): Merge conflict in counter.txt`. After every strategy the original base still read `value = 1`.

**T3 — pass, 124 ms.** Each changeset was exported with `as_patch()`, written to the host (`t3-a.patch` is 139 bytes, `value = 1` → `value = 2`; `t3-b.patch` is the same shape for `value = 3`), read back from disk, applied with `with_patch` onto a fresh V1, and merged. `FAIL` still conflicted. S3/R2 was not configured. The durable handoff that was tested is a git patch.

**T4 — partial, 107 ms.** Two containers, no shared writable mount. Agent A received `stat b-only.txt: no such file or directory` when reading Agent B's private file. Merging the two changesets still conflicted on `counter.txt`. No flock and no shared git directory were used. Two physical hosts were not available, so this is not a distributed-coordination pass.

**T5 — pass, 215 ms.** B published `value = 2` first. A's later publish of `value = 9` from the same V1 was rejected (`CONFLICT (content)`). On a 30-line file, A's edit of line 1 and B's edit of line 30 both survived: the merged file starts `line 1 FROM_A` and ends `line 30 FROM_B`. A same-line stale publish was not applied over V2.

**T6 — pass, 122 ms.** An exec that wrote `value = partial` and then `exit 1` did not return a directory (`failed_exec_published: false`). The base stayed `value = 1`. Agent B then promoted `value = from B`. No lock was held. Killing a second machine was not simulated, because there is no remote lock or mount to reclaim.

**T7 — pass, 388 ms.** Creating `a.txt` and `b.txt` merged to both files. Line 1 and line 30 of a 30-line file merged and both edits remained. Lines 10 and 11 conflicted. The same line conflicted. Granularity is a git hunk with the default 3 lines of context. Adjacent lines overlap that context, so they conflict even though the edited line numbers differ. The granularity is not semantic.

**T8 — pass, 144 ms.** While A and B edited, canonical `counter.txt` stayed `value = 1`. Promotion produced `counter.txt = value = 2`, `spec.txt = spec from A`, `notes.txt = notes from B`. The base object still read `value = 1` (rollback is discarding the new directory). A second promotion that also rewrote `counter.txt` was rejected, and the base was still `value = 1`. Diff stats listed `counter.txt` modified and `spec.txt` / `notes.txt` added. Provenance is the patch text. A Changeset has no `agent_id` or `run_id`.

**T9 — pass, 398 ms.** Four changesets rewriting `a.txt`, `b.txt`, `c.txt`, and `d.txt` octopus-merged to `a1`, `b1`, `c1`, `d1`. A second octopus where C and D both rewrote `shared.txt` failed with `ERROR: content conflict in shared.txt`. Dropping D and merging A+B+C kept `a1`, `b1`, `shared.txt = from C`, and `d.txt = d0`.

## Measurements (S0)

| Dimension | Result | Evidence class |
| --- | --- | --- |
| Test passed | T2, T3, T5, T6, T7, T8, T9 pass. T1 fail. T4 partial. | verified |
| Agent isolation | One container per agent. Two processes in one container share the filesystem. | verified |
| Cross-container support | Yes. Containers do not see each other's private files. | verified |
| Cross-machine support | Mechanism is a patch/changeset, verified by re-reading patches from the host. Two hosts not run. | partial |
| Remote storage support | Git patch on the local disk. No bucket. | verified for patches; S3 unsupported |
| Conflict detection | Git content conflict, and `FAIL_EARLY` path overlap. | verified |
| Conflict resolution | Markers, or an explicit ours/theirs choice. Default refuses. | verified |
| Conflict granularity | Git hunk. Files and distant lines merge. Adjacent lines and the same line conflict. | verified |
| Lost-update protection | Same-line stale publish is rejected. Shared-container overwrite is not protected. | verified |
| Distributed locking | None. Not used. | verified absent |
| Stale-state detection | Merge against the published changeset detects it. | verified |
| Crash recovery | Failed exec publishes nothing. No lease to recover. | verified for exec failure |
| Atomic promotion | `with_changes` returns a new directory or the merge errors. Base unchanged. | verified |
| Rollback | Keep the previous Directory object. | verified |
| Provenance / history | `as_patch()` and `diff_stats()`. No agent identity on the changeset. | verified |
| N-agent support | Four changesets via `with_changesets`. One conflicting pair fails the octopus; the rest can be merged without it. | verified |
| Setup complexity | Dagger engine plus an imported image. | verified |
| Runtime services | One privileged engine container. | verified |
| Operational complexity | Engine lifecycle and image import. No metadata database, lock service, or bucket. | verified for S0 |
| Observed latency | About 8 ms for path-level `FAIL_EARLY`, about 30–60 ms for a git merge of one small file, under 400 ms for the four-agent case, warm engine. | verified |
| Major limitations | No same-container isolation. Hunk granularity. No physical two-host run. No agent id. Octopus has no ours/theirs. | verified / not run |

## Documented vs verified

| Claim | Class |
| --- | --- |
| `with_changeset` strategies FAIL, FAIL_EARLY, LEAVE_CONFLICT_MARKERS, PREFER_OURS, PREFER_THEIRS | verified, T2 |
| `with_changesets` octopus, FAIL only in this script | verified, T9 |
| Failed container exec does not yield a directory | verified, T6 |
| Base Directory is unchanged after `with_changes` | verified, T2 and T8 |
| Two containers on one engine do not share a writable mount | verified, T4 |
| Two physical hosts reconcile a conflict | not run |
| S3/R2 as the patch store | unsupported here |
| Container Use environments and git worktrees | not run in this stage |
| Open Orchestrator Conflict Guard | not run. Dagger docs and the `owt` design describe a file-overlap warning; S0 `FAIL_EARLY` was the file-overlap check actually executed |
| JuiceFS POSIX consistency, locks, crash behavior | not run in this stage. An earlier measurement on this same VM, stored outside the repo at `/tmp/fs-exp/results.json`, saw a shared JuiceFS mount lose an update (`final: AGENT_B`, `lost_update: true`) and a killed writer leave the prefix `PARTIAL_` |
| lakeFS branch merge and S3 promotion | not run. Documented product behavior was not re-checked against a server |
| Engine DNS to Docker Hub | verified failure in this VM; worked around by image import |

## Failure analysis

**T1.** Both agents shared `/work` in one container and used `>` truncation. The last `printf` won. Nothing in Dagger's changeset API runs unless each agent returns its own directory. Putting both agents in one writable container skips that API. Container Use would also give each agent a separate environment; it does not make two writers in one container safe. The fix is the S0 shape already tested in T2: one container, one changeset, then merge.

**T4.** The VM is one machine. Two containers demonstrate filesystem isolation and conflict detection. They do not demonstrate two kernels, two disks, or a network partition. Marking that as a pass would fake the test. S3 and S4 could not be given an honest two-host run here either: there is no second host and no bucket, and a JuiceFS metadata store on local SQLite is single-host.

**T7 adjacent lines.** This is a detected conflict, not a lost update. Git's default hunk context is 3 lines, so line 10 and line 11 overlap. Callers who need those edits to merge have to separate them further or resolve the markers. That is a granularity limit of the chosen merge, recorded in the passing T7 result.

## Operational comparison

| Stack | Services that would have to run | Why it was stopped |
| --- | --- | --- |
| S0 Dagger | Engine container, agent containers | Chosen. |
| S1 + Container Use | S0 plus Container Use's per-agent environments and git worktrees | Separate containers already exist in S0. Worktree merge repeats changeset merge. |
| S2 + Open Orchestrator | S1 plus the `owt` cockpit and Conflict Guard | `FAIL_EARLY` already reports same-path overlap before `git merge`. Guard reports; it does not resolve. The canonical directory is immutable until promotion, so a two-phase "merge into the feature branch first" step has no dirty canonical tree to protect. |
| S3 + JuiceFS + S3/R2 | Metadata database, object bucket, FUSE mount per machine, lock/lease behavior | Shared-head. T1 is that model, and it lost an update. T5 shows the isolated merge rejects the stale writer instead. |
| S4 + lakeFS + S3 | lakeFS server, object bucket, branch per agent | Same isolated branch/merge shape S0 already executed, with an extra service and a bucket this environment does not have. |

## Components eliminated

- **Container Use.** S0 already runs one agent per container and merges the resulting diffs. No test failed for lack of a worktree.
- **Open Orchestrator.** File-overlap detection was measured as `FAIL_EARLY`. Line conflicts were measured as git's content conflict. Resolution was measured as markers or an explicit ours/theirs strategy. The final merge was `with_changeset` / `with_changesets`.
- **JuiceFS and a shared POSIX mount.** The experiments that passed use isolated directories. A live shared filesystem would add locking, stale reads, and partial writes, which T5 and T6 did not need.
- **lakeFS.** Branch, commit, conflict, merge, and rollback were covered by Directory + Changeset + discarding the unpromoted object.
- **S3/R2.** The cross-process artifact was a 139-byte patch. No requirement forced that patch into an object store.

JuiceFS and lakeFS were not combined. Neither was required on its own.

## Known limitations of S0

- Two agents in one writable container can overwrite each other (T1).
- Conflict granularity is a git hunk. Adjacent lines conflict. There is no semantic merge.
- `PREFER_OURS` / `PREFER_THEIRS` discard a side when selected. The octopus API used here has no ours/theirs strategy; a conflicting pair fails the whole octopus until that pair is removed (T9).
- Changesets carry patch text and diff stats, not an agent or run id.
- There is no live shared mutable filesystem and no distributed lock. Agents coordinate by merging changesets.
- Two physical hosts were not executed. The handoff that would cross hosts is the patch from T3.
- A failed exec publishes nothing. A host crash of the engine process itself was not killed mid-merge.
- This VM's engine cannot pull from Docker Hub; images have to be imported.

## Evidence

| Conclusion | Where |
| --- | --- |
| Default merge rejects a same-line conflict and leaves V1 in place | `s0-results.json` T2 `strategies.FAIL`, `base_unchanged_check` |
| Path overlap is reported before git merge | T2 `strategies.FAIL_EARLY`; `s0-engine-excerpt.log` |
| Markers, ours, and theirs | T2 `LEAVE_CONFLICT_MARKERS`, `PREFER_OURS`, `PREFER_THEIRS` |
| A patch copied off the container still conflicts | `t3-a.patch`, `t3-b.patch`, T3 `reapply_then_merge` |
| Containers do not see each other's files | T4 `agent_a_cannot_read_agent_b_file` |
| Stale same-line publish is rejected; distant lines merge | T5 |
| Crashed exec does not publish; the other agent still promotes | T6 |
| Disjoint files and distant lines merge; adjacent and identical lines conflict | T7 |
| Canonical V1 stays clean until promotion; rejected promotion does not replace it | T8 |
| Four independent changesets merge; one conflicting pair can be dropped without losing the others | T9 |
