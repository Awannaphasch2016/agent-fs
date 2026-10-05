#!/usr/bin/env python3
"""S0 — Dagger Changesets. Isolated state, explicit merge.

Runs T1–T9 against Dagger v0.21 Changesets. Prints a JSON report on stdout
and writes the same document to --out.

This script does not contact S3, JuiceFS, lakeFS, Container Use, or
Open Orchestrator. Those stacks are later stages and are only justified if
S0 fails a requirement.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

import dagger


# The engine container cannot resolve Docker Hub from this VM. The Alpine
# image is loaded from a tarball the host already pulled.
ALPINE_TAR = os.environ.get("ALPINE_TAR", "/tmp/alpine-oci-plat.tar")
ALPINE_TAG = "docker.io/library/alpine:3.20"


_alpine: dagger.Container | None = None


def alpine(client: dagger.Client) -> dagger.Container:
    global _alpine
    if _alpine is None:
        _alpine = client.container().import_(client.host().file(ALPINE_TAR), tag=ALPINE_TAG)
    return _alpine


def now_ms() -> float:
    return time.perf_counter() * 1000


async def txt(directory: dagger.Directory, path: str) -> str:
    return await directory.file(path).contents()


async def stats(changeset: dagger.Changeset) -> list[dict]:
    rows = []
    for stat in await changeset.diff_stats():
        rows.append(
            {
                "path": await stat.path(),
                "kind": str(await stat.kind()),
                "added_lines": await stat.added_lines(),
                "removed_lines": await stat.removed_lines(),
            }
        )
    return rows


def err_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text[:1200]


async def merge_pair(
    base: dagger.Directory,
    left: dagger.Changeset,
    right: dagger.Changeset,
    strategy: dagger.ChangesetMergeConflict,
    sample: str = "counter.txt",
) -> dict:
    started = now_ms()
    try:
        merged = left.with_changeset(right, on_conflict=strategy)
        promoted = base.with_changes(merged)
        body = await txt(promoted, sample) if sample else ""
        patch = await merged.as_patch().contents()
        base_body = None
        if sample:
            try:
                base_body = await txt(base, sample)
            except dagger.DaggerError:
                # A newly added file is absent from the immutable base. That is expected.
                base_body = None
        return {
            "ok": True,
            "strategy": strategy.value,
            "ms": round(now_ms() - started, 1),
            "sample": body,
            "diff_stats": await stats(merged),
            "patch_bytes": len(patch.encode()),
            "base_unchanged_check": base_body,
        }
    except dagger.DaggerError as exc:
        return {
            "ok": False,
            "strategy": strategy.value,
            "ms": round(now_ms() - started, 1),
            "error": err_text(exc),
        }


async def agent_dir(
    client: dagger.Client,
    base: dagger.Directory,
    script: str,
) -> dagger.Directory:
    """Run script in its own container. The container sees only `base` mounted at /work.

    A failing script raises and does not return a directory, so a crashed
    agent cannot publish a partial tree.
    """
    return (
        alpine(client)
        .with_directory("/work", base)
        .with_workdir("/work")
        .with_exec(["sh", "-c", script])
        .directory("/work")
    )


async def t1_same_container(client: dagger.Client) -> dict:
    """Two writers, one container, one mutable file. Negative control."""
    script = r"""
set -eu
printf 'value = 1\n' > counter.txt
write_after_read() {
  cat counter.txt >/dev/null
  sleep 0.2
  printf '%s\n' "$1" > counter.txt
}
write_after_read 'value = 2' &
write_after_read 'value = 3' &
wait
printf 'FINAL=%s\n' "$(cat counter.txt)"
"""
    started = now_ms()
    out = await (
        alpine(client)
        .with_workdir("/work")
        .with_exec(["mkdir", "-p", "/work"])
        .with_exec(["sh", "-c", script])
        .stdout()
    )
    final = [line for line in out.splitlines() if line.startswith("FINAL=")]
    body = final[-1].split("=", 1)[1] if final else out
    silent = body in ("value = 2", "value = 3")
    return {
        "status": "fail",
        "verified": "experiment",
        "ms": round(now_ms() - started, 1),
        "final": body,
        "silent_lost_update": silent,
        "isolation": "none — both processes share the container filesystem",
        "note": (
            "S0 does not isolate two agents inside one writable container. "
            "The architecture puts each agent in its own container and merges Changesets."
        ),
    }


async def t2_separate_containers(client: dagger.Client, base: dagger.Directory) -> dict:
    started = now_ms()
    dir_a, dir_b = await asyncio.gather(
        agent_dir(client, base, "printf 'value = 2\\n' > counter.txt"),
        agent_dir(client, base, "printf 'value = 3\\n' > counter.txt"),
    )
    # Neither container is the canonical tree. Base is still v1.
    canon_during = await txt(base, "counter.txt")
    seen_a = await txt(dir_a, "counter.txt")
    seen_b = await txt(dir_b, "counter.txt")
    cs_a = dir_a.changes(base)
    cs_b = dir_b.changes(base)
    strategies = {}
    for strategy in (
        dagger.ChangesetMergeConflict.FAIL,
        dagger.ChangesetMergeConflict.FAIL_EARLY,
        dagger.ChangesetMergeConflict.LEAVE_CONFLICT_MARKERS,
        dagger.ChangesetMergeConflict.PREFER_OURS,
        dagger.ChangesetMergeConflict.PREFER_THEIRS,
    ):
        strategies[strategy.value] = await merge_pair(base, cs_a, cs_b, strategy)
    default = strategies["FAIL"]
    return {
        "status": "pass" if (not default["ok"] and canon_during == "value = 1\n") else "fail",
        "verified": "experiment",
        "ms": round(now_ms() - started, 1),
        "canonical_during_isolated_edits": canon_during,
        "agent_a_sees": seen_a,
        "agent_b_sees": seen_b,
        "agents_isolated": seen_a != seen_b and canon_during == "value = 1\n",
        "default_strategy_rejects": not default["ok"],
        "strategies": strategies,
    }


async def t3_patch_artifact(client: dagger.Client, base: dagger.Directory, out_dir: Path) -> dict:
    """Durable artifact is a git patch on the host, not an object bucket.

    S3/R2 is not configured. This checks whether leaving the container changes
    merge behavior. It does not claim object-storage semantics.
    """
    started = now_ms()
    dir_a = await agent_dir(client, base, "printf 'value = 2\\n' > counter.txt")
    dir_b = await agent_dir(client, base, "printf 'value = 3\\n' > counter.txt")
    patch_a = await dir_a.changes(base).as_patch().contents()
    patch_b = await dir_b.changes(base).as_patch().contents()
    host_a = out_dir / "t3-a.patch"
    host_b = out_dir / "t3-b.patch"
    host_a.write_text(patch_a)
    host_b.write_text(patch_b)
    # Rebuild changesets by applying each patch, re-read from disk, to a fresh
    # copy of v1. That is the cross-process handoff.
    v1 = client.directory().with_new_file("counter.txt", "value = 1\n")
    applied_a = v1.with_patch(host_a.read_text())
    applied_b = v1.with_patch(host_b.read_text())
    cs_a = applied_a.changes(v1)
    cs_b = applied_b.changes(v1)
    failed = await merge_pair(v1, cs_a, cs_b, dagger.ChangesetMergeConflict.FAIL)
    return {
        "status": "pass" if not failed["ok"] else "fail",
        "verified": "experiment",
        "s3_or_r2": "unsupported — no bucket credentials; not required for the isolated model",
        "ms": round(now_ms() - started, 1),
        "patch_a_bytes": host_a.stat().st_size,
        "patch_b_bytes": host_b.stat().st_size,
        "reapply_then_merge": failed,
    }


async def t4_no_shared_mount(client: dagger.Client, base: dagger.Directory) -> dict:
    """Two containers, no shared writable mount, no flock, no shared git dir.

    Physical two-host execution is not available in this environment.
    The only value that crosses the boundary is the Changeset.
    """
    started = now_ms()
    dir_a, dir_b = await asyncio.gather(
        agent_dir(client, base, "printf 'value = A\\n' > counter.txt && printf 'only-a\\n' > a-only.txt"),
        agent_dir(client, base, "printf 'value = B\\n' > counter.txt && printf 'only-b\\n' > b-only.txt"),
    )
    # A cannot see B's private file because it was never mounted into A.
    a_missing_b = False
    try:
        await txt(dir_a, "b-only.txt")
    except dagger.DaggerError:
        a_missing_b = True
    conflict = await merge_pair(
        base, dir_a.changes(base), dir_b.changes(base), dagger.ChangesetMergeConflict.FAIL
    )
    containers_ok = a_missing_b and not conflict["ok"]
    return {
        # Two containers on one engine share neither a writable mount nor a lock.
        # That is not two physical hosts. T4 stays partial until a second host exists.
        "status": "partial" if containers_ok else "fail",
        "verified": "experiment",
        "physical_hosts": "not run — this environment is one VM",
        "what_was_verified": (
            "two containers cannot read each other's files; the only value that "
            "crosses the boundary is the Changeset, and the merge still detects the conflict"
        ),
        "ms": round(now_ms() - started, 1),
        "agent_a_cannot_read_agent_b_file": a_missing_b,
        "merge": conflict,
        "shared_kernel_locks": "none used",
    }


def thirty_lines() -> str:
    return "".join(f"line {i} base\n" for i in range(1, 31))


async def t5_stale_writer(client: dagger.Client, base: dagger.Directory) -> dict:
    started = now_ms()
    # Both read v1. B publishes first. A then tries to publish its stale edit of the same line.
    dir_b = await agent_dir(client, base, "printf 'value = 2\\n' > counter.txt")
    v2 = base.with_changes(dir_b.changes(base))
    v2_body = await txt(v2, "counter.txt")
    dir_a = await agent_dir(client, base, "printf 'value = 9\\n' > counter.txt")
    stale = await merge_pair(
        base, dir_b.changes(base), dir_a.changes(base), dagger.ChangesetMergeConflict.FAIL
    )
    # Distant lines of a 30-line file produce non-overlapping hunks and must both survive.
    # A 3-line file does not: each hunk spans the whole file, so that fixture is not used.
    wider = client.directory().with_new_file("notes.txt", thirty_lines())
    b_notes = await agent_dir(client, wider, "sed -i '30s/base/FROM_B/' notes.txt")
    a_notes = await agent_dir(client, wider, "sed -i '1s/base/FROM_A/' notes.txt")
    clean = await merge_pair(
        wider,
        b_notes.changes(wider),
        a_notes.changes(wider),
        dagger.ChangesetMergeConflict.FAIL,
        sample="notes.txt",
    )
    merged = clean.get("sample") or ""
    distant_kept = merged.startswith("line 1 FROM_A\n") and merged.endswith("line 30 FROM_B\n")
    return {
        "status": "pass" if (v2_body == "value = 2\n" and not stale["ok"] and distant_kept) else "fail",
        "verified": "experiment",
        "ms": round(now_ms() - started, 1),
        "v2_after_b": v2_body,
        "stale_same_line_rejected": stale,
        "stale_distant_lines": {
            "ok": clean.get("ok"),
            "both_edits_kept": distant_kept,
            "head": "\n".join(merged.splitlines()[:2]),
            "tail": "\n".join(merged.splitlines()[-2:]),
            "error": clean.get("error"),
        },
    }


async def t6_crash(client: dagger.Client, base: dagger.Directory) -> dict:
    started = now_ms()
    crashed = (
        alpine(client)
        .with_directory("/work", base)
        .with_workdir("/work")
        .with_exec(["sh", "-c", "printf 'value = partial\\n' > counter.txt; exit 1"])
    )
    publish_error = None
    try:
        await crashed.directory("/work").file("counter.txt").contents()
        published = True
    except dagger.DaggerError as exc:
        published = False
        publish_error = err_text(exc)
    canon = await txt(base, "counter.txt")
    # B proceeds from the untouched base and promotes its own change.
    dir_b = await agent_dir(client, base, "printf 'value = from B\\n' > counter.txt")
    promoted = base.with_changes(dir_b.changes(base))
    b_body = await txt(promoted, "counter.txt")
    return {
        "status": "pass" if (not published and canon == "value = 1\n" and b_body == "value = from B\n") else "fail",
        "verified": "experiment",
        "ms": round(now_ms() - started, 1),
        "failed_exec_published": published,
        "publish_error": publish_error,
        "canonical_after_crash": canon,
        "agent_b_promoted": b_body,
        "locks": "no lock was held; nothing to reclaim",
        "machine_loss": "not simulated — there is no remote lock or mount",
    }


async def t7_independent(client: dagger.Client) -> dict:
    started = now_ms()
    files_base = client.directory().with_new_file("readme.txt", "project\n")
    dir_a = await agent_dir(client, files_base, "printf 'from A\\n' > a.txt")
    dir_b = await agent_dir(client, files_base, "printf 'from B\\n' > b.txt")
    files = await merge_pair(
        files_base,
        dir_a.changes(files_base),
        dir_b.changes(files_base),
        dagger.ChangesetMergeConflict.FAIL,
        sample="a.txt",
    )
    btxt = None
    if files["ok"]:
        tree = files_base.with_changes(
            dir_a.changes(files_base).with_changeset(dir_b.changes(files_base))
        )
        btxt = await txt(tree, "b.txt")

    wide = client.directory().with_new_file("notes.txt", thirty_lines())
    far_a = await agent_dir(client, wide, "sed -i '1s/base/FROM_A/' notes.txt")
    far_b = await agent_dir(client, wide, "sed -i '30s/base/FROM_B/' notes.txt")
    distant = await merge_pair(
        wide,
        far_a.changes(wide),
        far_b.changes(wide),
        dagger.ChangesetMergeConflict.FAIL,
        sample="notes.txt",
    )
    distant_body = distant.get("sample") or ""
    distant_kept = distant_body.startswith("line 1 FROM_A\n") and distant_body.endswith(
        "line 30 FROM_B\n"
    )

    # Default git hunk context is 3 lines, so edits on lines 10 and 11 overlap and conflict.
    adj_a = await agent_dir(client, wide, "sed -i '10s/base/FROM_A/' notes.txt")
    adj_b = await agent_dir(client, wide, "sed -i '11s/base/FROM_B/' notes.txt")
    adjacent = await merge_pair(
        wide,
        adj_a.changes(wide),
        adj_b.changes(wide),
        dagger.ChangesetMergeConflict.FAIL,
        sample="notes.txt",
    )
    same_a = await agent_dir(client, wide, "sed -i '1s/base/FROM_A/' notes.txt")
    same_b = await agent_dir(client, wide, "sed -i '1s/base/FROM_B/' notes.txt")
    same = await merge_pair(
        wide,
        same_a.changes(wide),
        same_b.changes(wide),
        dagger.ChangesetMergeConflict.FAIL,
        sample="notes.txt",
    )
    ok = (
        files["ok"]
        and files.get("sample") == "from A\n"
        and btxt == "from B\n"
        and distant_kept
        and not adjacent["ok"]
        and not same["ok"]
    )
    return {
        "status": "pass" if ok else "fail",
        "verified": "experiment",
        "ms": round(now_ms() - started, 1),
        "disjoint_files": {
            "merge_ok": files["ok"],
            "a_txt": files.get("sample"),
            "b_txt": btxt,
            "error": files.get("error"),
        },
        "distant_lines": {
            "both_edits_kept": distant_kept,
            "head": "\n".join(distant_body.splitlines()[:1]),
            "tail": "\n".join(distant_body.splitlines()[-1:]),
            "error": distant.get("error"),
        },
        "adjacent_lines_conflict": {"ok": adjacent["ok"], "error": adjacent.get("error")},
        "same_line_conflict": {"ok": same["ok"], "error": same.get("error")},
        "granularity": (
            "git hunk, not semantic and not a pure line. "
            "Different files merge. Lines far enough apart to form separate hunks merge. "
            "Adjacent lines conflict because the default 3-line hunk context overlaps. "
            "The same line conflicts."
        ),
    }


async def t8_promote(client: dagger.Client, base: dagger.Directory) -> dict:
    started = now_ms()
    dir_a = await agent_dir(
        client,
        base,
        "printf 'value = 2\\n' > counter.txt; printf 'spec from A\\n' > spec.txt",
    )
    dir_b = await agent_dir(
        client,
        base,
        "printf 'notes from B\\n' > notes.txt",
    )
    canon_during = await txt(base, "counter.txt")
    cs_a = dir_a.changes(base)
    cs_b = dir_b.changes(base)
    patch_a = await cs_a.as_patch().contents()
    merged = cs_a.with_changeset(cs_b, on_conflict=dagger.ChangesetMergeConflict.FAIL)
    promoted = base.with_changes(merged)
    # Rollback is discarding `promoted`. `base` is immutable.
    rolled_back = await txt(base, "counter.txt")
    body = await txt(promoted, "counter.txt")
    spec = await txt(promoted, "spec.txt")
    notes = await txt(promoted, "notes.txt")
    # A conflicting promotion does not replace the canonical object.
    clash = await agent_dir(client, base, "printf 'value = 9\\n' > counter.txt")
    rejected = await merge_pair(
        base, cs_a, clash.changes(base), dagger.ChangesetMergeConflict.FAIL
    )
    still = await txt(base, "counter.txt")
    ok = (
        canon_during == "value = 1\n"
        and body == "value = 2\n"
        and spec == "spec from A\n"
        and notes == "notes from B\n"
        and rolled_back == "value = 1\n"
        and not rejected["ok"]
        and still == "value = 1\n"
    )
    return {
        "status": "pass" if ok else "fail",
        "verified": "experiment",
        "ms": round(now_ms() - started, 1),
        "canonical_during": canon_during,
        "promoted": {"counter.txt": body, "spec.txt": spec, "notes.txt": notes},
        "rollback_base_still": rolled_back,
        "patch_a_head": "\n".join(patch_a.splitlines()[:12]),
        "diff_stats": await stats(merged),
        "conflicting_promotion_rejected": rejected,
        "canonical_after_rejected_promotion": still,
        "provenance": "patch text and diff stats; no agent_id or run_id field on a Changeset",
    }


async def t9_four_agents(client: dagger.Client) -> dict:
    started = now_ms()
    base = (
        client.directory()
        .with_new_file("a.txt", "a0\n")
        .with_new_file("b.txt", "b0\n")
        .with_new_file("c.txt", "c0\n")
        .with_new_file("d.txt", "d0\n")
        .with_new_file("shared.txt", "s0\n")
    )
    edits = {
        "A": "printf 'a1\\n' > a.txt",
        "B": "printf 'b1\\n' > b.txt",
        "C": "printf 'c1\\n' > c.txt",
        "D": "printf 'd1\\n' > d.txt",
    }
    dirs = await asyncio.gather(
        *[agent_dir(client, base, script) for script in edits.values()]
    )
    changes = [d.changes(base) for d in dirs]
    octopus_started = now_ms()
    try:
        merged = changes[0].with_changesets(
            changes[1:], on_conflict=dagger.ChangesetsMergeConflict.FAIL
        )
        tree = base.with_changes(merged)
        bodies = {name: await txt(tree, name) for name in ("a.txt", "b.txt", "c.txt", "d.txt")}
        independent = {"ok": True, "ms": round(now_ms() - octopus_started, 1), "files": bodies}
    except dagger.DaggerError as exc:
        independent = {"ok": False, "ms": round(now_ms() - octopus_started, 1), "error": err_text(exc)}

    # One conflicting pair (C and D both rewrite shared.txt). A and B stay independent.
    conflict_dirs = await asyncio.gather(
        agent_dir(client, base, "printf 'a1\\n' > a.txt"),
        agent_dir(client, base, "printf 'b1\\n' > b.txt"),
        agent_dir(client, base, "printf 'from C\\n' > shared.txt"),
        agent_dir(client, base, "printf 'from D\\n' > shared.txt"),
    )
    conflict_cs = [d.changes(base) for d in conflict_dirs]
    try:
        bad = conflict_cs[0].with_changesets(
            conflict_cs[1:], on_conflict=dagger.ChangesetsMergeConflict.FAIL
        )
        await bad.sync()
        octopus_conflict = {"ok": True}
    except dagger.DaggerError as exc:
        octopus_conflict = {"ok": False, "error": err_text(exc)}
    # Drop D. A, B, and C must still promote.
    try:
        kept = conflict_cs[0].with_changesets(
            conflict_cs[1:3], on_conflict=dagger.ChangesetsMergeConflict.FAIL
        )
        tree = base.with_changes(kept)
        preserved = {
            "ok": True,
            "a.txt": await txt(tree, "a.txt"),
            "b.txt": await txt(tree, "b.txt"),
            "shared.txt": await txt(tree, "shared.txt"),
            "d.txt": await txt(tree, "d.txt"),
        }
    except dagger.DaggerError as exc:
        preserved = {"ok": False, "error": err_text(exc)}
    ok = (
        independent.get("ok")
        and independent.get("files") == {"a.txt": "a1\n", "b.txt": "b1\n", "c.txt": "c1\n", "d.txt": "d1\n"}
        and not octopus_conflict.get("ok")
        and preserved.get("ok")
        and preserved.get("a.txt") == "a1\n"
        and preserved.get("b.txt") == "b1\n"
        and preserved.get("shared.txt") == "from C\n"
        and preserved.get("d.txt") == "d0\n"
    )
    return {
        "status": "pass" if ok else "fail",
        "verified": "experiment",
        "ms": round(now_ms() - started, 1),
        "independent_octopus": independent,
        "octopus_with_one_conflicting_pair": octopus_conflict,
        "independent_agents_preserved_after_dropping_conflict": preserved,
        "granularity": "git octopus merge of Changesets; file-disjoint edits combine, a file touched by two heads fails the whole octopus",
    }


async def run(out_path: Path) -> dict:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report: dict = {
        "stack": "S0 Dagger Changesets",
        "dagger_cli": "v0.21.10",
        "sdk": "dagger-io==0.21.10",
        "image": f"{ALPINE_TAG} imported from {ALPINE_TAR}",
        "host": "single linux x86_64 VM, no second physical host",
        "tests": {},
    }
    engine_log = Path("/tmp/s0-dagger-engine.log")
    async with dagger.connection(dagger.Config(log_output=open(engine_log, "w"))):
        client = dagger.dag
        base = client.directory().with_new_file("counter.txt", "value = 1\n")
        # Materialize the base once so later timings are not dominated by the first pull.
        await txt(base, "counter.txt")
        report["tests"]["T1"] = await t1_same_container(client)
        report["tests"]["T2"] = await t2_separate_containers(client, base)
        report["tests"]["T3"] = await t3_patch_artifact(client, base, out_path.parent)
        report["tests"]["T4"] = await t4_no_shared_mount(client, base)
        report["tests"]["T5"] = await t5_stale_writer(client, base)
        report["tests"]["T6"] = await t6_crash(client, base)
        report["tests"]["T7"] = await t7_independent(client)
        report["tests"]["T8"] = await t8_promote(client, base)
        report["tests"]["T9"] = await t9_four_agents(client)
    report["summary"] = {
        test: result["status"] for test, result in report["tests"].items()
    }
    out_path.write_text(json.dumps(report, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).with_name("s0-results.json"),
    )
    args = parser.parse_args()
    report = asyncio.run(run(args.out))
    print(json.dumps(report["summary"], indent=2))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
