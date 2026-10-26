#!/usr/bin/env python3
"""只读审计 Git 历史的终态、父提交顺序与时间来源，不代替构建或测试。"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def git(repo, *args):
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, encoding="utf-8", errors="replace"
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def increasing(values):
    return all(left < right for left, right in zip(values, values[1:]))


def audit(repo, source, candidate, policy, no_deletions, started_at=None):
    source_sha = git(repo, "rev-parse", "--verify", f"{source}^{{commit}}")
    candidate_sha = git(repo, "rev-parse", "--verify", f"{candidate}^{{commit}}")
    source_tree = git(repo, "rev-parse", f"{source_sha}^{{tree}}")
    candidate_tree = git(repo, "rev-parse", f"{candidate_sha}^{{tree}}")
    time_sources = {}
    for row in git(repo, "log", source_sha, "--format=%H%x00%aI%x00%cI").splitlines():
        sha, author_date, committer_date = row.split("\0")
        for value in {instant(author_date), instant(committer_date)}:
            time_sources.setdefault(value, []).append(sha)

    rows = git(
        repo, "log", "--reverse", "--topo-order", candidate_sha,
        "--format=%H%x00%T%x00%P%x00%aI%x00%cI%x00%s",
    ).splitlines()
    commits = []
    deletions = []
    linear = True
    previous = None
    for number, row in enumerate(rows, 1):
        sha, tree, parents, author_date, committer_date, subject = row.split("\0", 5)
        parent_list = parents.split()
        if parent_list != ([] if previous is None else [previous]):
            linear = False
        previous = sha
        commits.append({
            "number": number, "commit": sha, "tree": tree, "parents": parent_list,
            "subject": subject, "authorDate": author_date, "committerDate": committer_date,
            "authorTimeSources": time_sources.get(instant(author_date), []),
            "committerTimeSources": time_sources.get(instant(committer_date), []),
        })
        changes = git(repo, "diff-tree", "--root", "--no-commit-id", "--name-status", "-r", sha)
        deletions.extend(
            {"commit": sha, "path": change.split("\t", 1)[1]}
            for change in changes.splitlines() if change.startswith("D\t")
        )

    checks = {"finalTreeEqual": source_tree == candidate_tree, "linear": linear}
    if policy in {"original-both", "original-author"}:
        checks["authorDatesFromSource"] = all(c["authorTimeSources"] for c in commits)
        checks["strictAuthorDates"] = increasing([instant(c["authorDate"]) for c in commits])
    if policy == "original-both":
        checks["committerDatesFromSource"] = all(c["committerTimeSources"] for c in commits)
        checks["strictCommitterDates"] = increasing([instant(c["committerDate"]) for c in commits])
    if policy == "actual":
        now = datetime.now(timezone.utc)
        author_dates = [instant(c["authorDate"]) for c in commits]
        committer_dates = [instant(c["committerDate"]) for c in commits]
        checks["datesNotFuture"] = all(date <= now for date in author_dates + committer_dates)
        checks["authorDatesNondecreasing"] = all(a <= b for a, b in zip(author_dates, author_dates[1:]))
        checks["committerDatesNondecreasing"] = all(a <= b for a, b in zip(committer_dates, committer_dates[1:]))
        if started_at:
            start = instant(started_at).replace(microsecond=0)
            checks["datesWithinExecutionWindow"] = all(date >= start for date in author_dates + committer_dates)
    if no_deletions:
        checks["noDeletions"] = not deletions
    return {
        "schemaVersion": 1, "auditedAt": datetime.now(timezone.utc).isoformat(),
        "source": {"commit": source_sha, "tree": source_tree},
        "candidate": {"commit": candidate_sha, "tree": candidate_tree},
        "timePolicy": policy, "startedAt": started_at, "checks": checks, "passed": all(checks.values()),
        "commits": commits, "deletions": deletions,
        "scope": "只检查 Git 记录；节点的构建、测试和运行时入口需要另行验证。",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="仓库路径")
    parser.add_argument("--source", required=True, help="开始时固定的原提交 SHA 或恢复引用")
    parser.add_argument("--candidate", required=True, help="候选引用或 SHA")
    parser.add_argument(
        "--time-policy", choices=["actual", "original-both", "original-author", "none"],
        default="actual",
        help="actual 检查时间顺序及执行窗口；original-both 检查原作者与提交者时间；original-author 只检查原作者时间；none 不检查时间策略。",
    )
    parser.add_argument("--started-at", help="actual 策略的实际执行开始时间（ISO），可检查是否错误回填旧日期")
    parser.add_argument("--no-deletions", action="store_true", help="仅当任务要求无删除提交时启用")
    parser.add_argument("--report", help="可选 JSON 输出文件，必须位于源仓库之外")
    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    report_path = Path(args.report).resolve() if args.report else None
    try:
        if report_path:
            # 裸仓库没有工作树，使用调用者给出的仓库目录作为边界。
            if git(repo, "rev-parse", "--is-bare-repository") == "true":
                root = repo
            else:
                root = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
            if report_path == root or report_path.is_relative_to(root):
                raise ValueError("审计记录必须写在源仓库之外，不能覆盖源文件。")
        result = audit(repo, args.source, args.candidate, args.time_policy, args.no_deletions, args.started_at)
        content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if report_path:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(content, encoding="utf-8")
        print(content, end="")
        return 0 if result["passed"] else 1
    except (ValueError, OSError) as error:
        print(f"审计未完成：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
