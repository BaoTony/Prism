"""
Git Merge 深度诊断工具 — 纯 Python，零 PS 依赖。
版本: v2.0  |  更新: 2026-10-23

───────────────────────────────────────────────────
  用途：分析 merge 后为什么产生  D / D  文件，定位根因并修复。
        配合 sync-branches.py 使用——merge 后若 worktree 有
        残留删除标记，本工具诊断根因并自动恢复。

───────────────────────────────────────────────────
  用法:
    python scripts/merge-diagnose.py                         # 全部分支诊断
    python scripts/merge-diagnose.py --branch feat/visualization
    python scripts/merge-diagnose.py --fix                   # 诊断 + 自动修复
    python scripts/merge-diagnose.py --phase 1               # 仅 Phase 1 条目级诊断

───────────────────────────────────────────────────
  输出阶段:
    Phase 1: 条目级诊断  — git status --porcelain + 三方比对定位根因
    Phase 2: 合并行为模拟 — 变更统计 + 分支独有文件风险预判
    Phase 3: 修复         — restore --staged + checkout HEAD 恢复误删文件

───────────────────────────────────────────────────
  内置保护:
    - 修复前备份原始分支，修复后自动切回
    - 区分"误删"与"前次合并已删"——避免错误恢复已正确删除的文件
    - 修复后再次 git status 验证，确保工作区干净
"""
import subprocess
import os
import sys
import json
import time
from datetime import datetime
from pathlib import Path

REPO = Path(r"D:\dev\store\jia-skills\wpf-prism-docs")
BRANCHES = ["feat/communication", "feat/visualization", "feat/multi-module-verify"]
MASTER = "master"
os.chdir(str(REPO))

# ── git 底层调用 ──────────────────────────────────────
# 只在独立 conhost（系统 PowerShell）隐藏子进程窗口，
# VS Code 集成终端是 node 内嵌的 pseudo-conhost，
# CREATE_NO_WINDOW 会干扰其渲染管线导致终端整体变白/空白。
_is_vscode_terminal = os.environ.get("VSCODE_PID") or os.environ.get("TERM_PROGRAM") == "vscode"
if os.name == "nt" and not _is_vscode_terminal:
    _STARTUP = subprocess.STARTUPINFO()
    _STARTUP.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _STARTUP.wShowWindow = subprocess.SW_HIDE
    _CREATEFLAGS = subprocess.CREATE_NO_WINDOW
else:
    _STARTUP = None
    _CREATEFLAGS = 0


def git(*args, timeout=60):
    try:
        r = subprocess.run(
            ["git", "-c", "core.quotepath=false"] + list(args),
            capture_output=True, text=True, timeout=timeout,
            cwd=str(REPO), encoding="utf-8", errors="replace",
            creationflags=_CREATEFLAGS, startupinfo=_STARTUP,
            stdin=subprocess.DEVNULL
        )
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT", -1
    except Exception as e:
        return "", str(e), -2


def sep(title):
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print("=" * 70)


def step(title):
    print(f"\n  -- {title} --")


# ============================================================
# Phase 1: 逐条目诊断
# ============================================================
def phase1_diagnose(branch):
    sep(f"Phase 1: [{branch}] 条目级诊断")

    # 切换到目标分支
    git("checkout", branch, timeout=10)
    time.sleep(0.2)

    # 获取 porcelain 状态（-z 零分隔符避免编码问题）
    out, _, _ = git("status", "--porcelain", "-z", timeout=10)
    entries = [x for x in out.split("\x00") if len(x) > 3]

    if not entries:
        print("  [OK] 工作区完全干净")
        return

    print(f"  异常条目: {len(entries)}")
    for e in entries:
        xy = e[:2]
        path = e[3:]
        disk_exists = os.path.exists(REPO / path)
        print(f"    [{xy}] {path}  disk={'Y' if disk_exists else 'N'}")

    # 深入分析每个  D 条目
    deleted_entries = [e for e in entries if e[:2] in (" D", "D ")]
    if not deleted_entries:
        print("\n  无删除条目，无需深入分析")
        return

    step(f"深入分析 {len(deleted_entries)} 个删除条目")

    # 获取 merge-base 和关键 commits
    mb, _, _ = git("merge-base", MASTER, branch, timeout=10)
    head_hash, _, _ = git("rev-parse", "--short", "HEAD", timeout=5)

    # 找到最新的 merge commit
    merge_log, _, _ = git("log", "-1", "--merges", "--format=%H %s", branch, timeout=10)
    merge_hash = merge_log.split()[0] if merge_log else "N/A"
    parent1, _, _ = git("log", "-1", "--format=%H", f"{merge_hash}^1", timeout=5) if merge_hash != "N/A" else ("?", "?", 1)
    parent2, _, _ = git("log", "-1", "--format=%H", f"{merge_hash}^2", timeout=5) if merge_hash != "N/A" else ("?", "?", 1)

    print(f"  merge-base: {mb[:12]}...")
    print(f"  最新 merge: {merge_hash[:12]}...  ({merge_log[41:80] if len(merge_log)>41 else merge_log})")
    print(f"     parent1:  {parent1[:12]}... (本分支侧)")
    print(f"     parent2:  {parent2[:12]}... (master 侧)")

    # 统计
    stats = {
        "p1_has_disk_gone": 0,   # parent1 有、磁盘没了 → 合并过程误删
        "p1_none": 0,             # parent1 就没有 → 早就被删了
        "p2_added": 0,            # parent2 新增但被暂存删除
        "other": 0
    }

    for e in deleted_entries:
        xy = e[:2]
        path = e[3:]
        disk_exists = os.path.exists(REPO / path)

        # 三个关键检查
        in_p1 = subprocess.run(["git", "cat-file", "-e", f"{parent1}:{path}"],
                               capture_output=True, cwd=str(REPO),
                               creationflags=_CREATEFLAGS, startupinfo=_STARTUP,
                               stdin=subprocess.DEVNULL).returncode == 0
        in_p2 = subprocess.run(["git", "cat-file", "-e", f"{parent2}:{path}"],
                               capture_output=True, cwd=str(REPO),
                               creationflags=_CREATEFLAGS, startupinfo=_STARTUP,
                               stdin=subprocess.DEVNULL).returncode == 0
        in_mb = subprocess.run(["git", "cat-file", "-e", f"{mb}:{path}"],
                               capture_output=True, cwd=str(REPO),
                               creationflags=_CREATEFLAGS, startupinfo=_STARTUP,
                               stdin=subprocess.DEVNULL).returncode == 0
        in_head = subprocess.run(["git", "cat-file", "-e", f"HEAD:{path}"],
                                 capture_output=True, cwd=str(REPO),
                                 creationflags=_CREATEFLAGS, startupinfo=_STARTUP,
                                 stdin=subprocess.DEVNULL).returncode == 0
        in_idx = subprocess.run(["git", "cat-file", "-e", f":{path}"],
                                capture_output=True, cwd=str(REPO),
                                creationflags=_CREATEFLAGS, startupinfo=_STARTUP,
                                stdin=subprocess.DEVNULL).returncode == 0

        # 判定
        reason = ""
        if in_head and not disk_exists:
            # HEAD 有、磁盘无 → 合并后磁盘被误删
            if xy == " D":
                # 暂存区与 HEAD 一致（有），但磁盘没了
                reason = "HEAD 有文件、工作区被删（合并进程误删磁盘文件）"
                stats["p1_has_disk_gone"] += 1
            elif xy == "D ":
                # 暂存区标注为删除，说明合并判定为"删除此文件"
                if in_p1 and not in_p2:
                    reason = "合并判定: 分支侧有 + master侧无 → 三方合并视为master意图删除"
                elif not in_p1 and in_p2:
                    reason = "合并判定: 分支侧无 + master侧有 → master新增但被暂存删除(异常)"
                else:
                    reason = f"暂存删除(原因不明) p1={in_p1} p2={in_p2}"
                stats["other"] += 1
        elif not in_head and not disk_exists:
            reason = "HEAD 中已不存在（前次合并已删除），磁盘同步缺失"
            stats["p1_none"] += 1
        elif not in_head and disk_exists:
            reason = "HEAD 已删但磁盘仍有（未跟踪残留文件）"
            stats["other"] += 1
        else:
            reason = f"其他: head={in_head} disk={disk_exists} idx={in_idx}"
            stats["other"] += 1

        print(f"\n    文件: {path}")
        print(f"      状态: [{xy}]  HEAD:{'有' if in_head else '无'}  P1:{'有' if in_p1 else '无'}  P2:{'有' if in_p2 else '无'}  MB:{'有' if in_mb else '无'}  磁盘:{'Y' if disk_exists else 'N'}")
        print(f"      判定: {reason}")

    print(f"\n  统计: p1_has_disk_gone={stats['p1_has_disk_gone']}  p1_none={stats['p1_none']}  p2_added={stats['p2_added']}  other={stats['other']}")
    return entries, stats


# ============================================================
# Phase 2: 合并行为模拟
# ============================================================
def phase2_simulate_merge(branch):
    sep(f"Phase 2: [{branch}] 合并行为模拟")

    mb, _, _ = git("merge-base", MASTER, branch, timeout=10)
    if not mb:
        print("  无法获取 merge-base")
        return

    # master 侧从 merge-base 到现在的变更
    step("master 侧变更类型统计")
    m_files, _, _ = git("diff", "--name-status", f"{mb}..{MASTER}", timeout=30)
    m_add = m_del = m_mod = 0
    for line in m_files.split("\n"):
        if line.startswith("A"):
            m_add += 1
        elif line.startswith("D"):
            m_del += 1
        elif line.startswith("M"):
            m_mod += 1
    print(f"  master: +{m_add}  -{m_del}  ~{m_mod}")

    # 分支侧变更
    step("分支侧变更类型统计")
    b_files, _, _ = git("diff", "--name-status", f"{mb}..{branch}", timeout=30)
    b_add = b_del = b_mod = 0
    for line in b_files.split("\n"):
        if line.startswith("A"):
            b_add += 1
        elif line.startswith("D"):
            b_del += 1
        elif line.startswith("M"):
            b_mod += 1
    print(f"  分支: +{b_add}  -{b_del}  ~{b_mod}")

    # 合并命中文件（双方都修改的）
    step("双方修改文件（冲突热点）")
    both, _, _ = git("diff", "--name-only", "--diff-filter=U", f"{mb}...{MASTER}", timeout=10)
    # 更好的方式：用 git merge-tree
    both_files, _, _ = git("diff", "--name-only", f"{mb}...{MASTER}", timeout=30)
    both_list = [f for f in both_files.split("\n") if f]
    if both_list:
        print(f"  {len(both_list)} 个文件双方都修改:")
        for f in both_list[:10]:
            print(f"    - {f}")
        if len(both_list) > 10:
            print(f"    ... 还有 {len(both_list) - 10} 个")
    else:
        print(f"  无双方修改文件")

    # 分支独有但 master 没有的文件（最易被误删）
    step("分支独有文件（master 侧不存在的）")
    branch_only, _, _ = git("diff", "--name-only", "--diff-filter=A", f"{mb}..{branch}", timeout=30)
    branch_only_list = [f for f in branch_only.split("\n") if f]
    orphaned = []
    for f in branch_only_list:
        r = subprocess.run(["git", "cat-file", "-e", f"{MASTER}:{f}"],
                          capture_output=True, cwd=str(REPO),
                          creationflags=_CREATEFLAGS, startupinfo=_STARTUP,
                          stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            orphaned.append(f)
    if orphaned:
        print(f"  {len(orphaned)} 个分支独有文件（master 完全不包含）:")
        for f in orphaned[:10]:
            print(f"    - {f}")
        if len(orphaned) > 10:
            print(f"    ... 还有 {len(orphaned) - 10} 个")


# ============================================================
# Phase 3: Fix
# ============================================================
def phase3_fix(branch):
    sep(f"Phase 3: [{branch}] 修复")

    out, _, _ = git("status", "--porcelain", "-z", timeout=10)
    entries = [x for x in out.split("\x00") if len(x) > 3]
    if not entries:
        print("  [OK] 无需修复")
        return

    # ── 修复前备份 ──────────────────────────────────────
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = branch.replace("/", "-")
    backup_ref = f"backup/{safe_name}/{ts}"
    head_short, _, _ = git("rev-parse", "--short", "HEAD", timeout=5)
    git("branch", backup_ref, "HEAD", timeout=10)
    print(f"  [BACKUP] {backup_ref}  ← {head_short}")
    backups_created = [backup_ref]

    # 分两类修复
    staged_del   = [e[3:] for e in entries if e[:2] == "D "]
    unstaged_del = [e[3:] for e in entries if e[:2] == " D"]

    print(f"  暂存删除: {len(staged_del)}  工作区删除: {len(unstaged_del)}")

    if staged_del:
        print("  取消暂存删除...")
        for f in staged_del:
            git("restore", "--staged", f, timeout=5)
            print(f"    ✓ unstaged: {f}")

    if unstaged_del:
        # 先检查 HEAD 中是否真的有这些文件
        really_deleted = []
        already_ok = []
        for f in unstaged_del:
            r = subprocess.run(["git", "cat-file", "-e", f"HEAD:{f}"],
                              capture_output=True, cwd=str(REPO),
                              creationflags=_CREATEFLAGS, startupinfo=_STARTUP,
                              stdin=subprocess.DEVNULL)
            if r.returncode == 0:
                really_deleted.append(f)
            else:
                already_ok.append(f)

        if really_deleted:
            print(f"  从 HEAD 恢复 {len(really_deleted)} 个文件...")
            git("checkout", "HEAD", "--", *really_deleted, timeout=30)
            for f in really_deleted:
                print(f"    ✓ 已恢复: {f}")

        if already_ok:
            print(f"  ⚠ {len(already_ok)} 个文件在 HEAD 中不存在（前次合并已删），跳过:")
            for f in already_ok:
                print(f"    ~ {f}")

    # 验证
    out, _, _ = git("status", "--porcelain", timeout=10)
    remaining = len([l for l in out.split("\n") if l.strip()])
    if remaining == 0:
        print("  [OK] 修复后工作区完全干净")
    else:
        print(f"  ⚠ 仍有 {remaining} 行异常")
        for l in out.split("\n")[:5]:
            if l.strip():
                print(f"    {l}")

    if backups_created:
        names = "\n      ".join(backups_created)
        print(f"  备份分支（如需回滚）:\n      {names}\n"
              f"      git checkout <分支> && git reset --hard {backups_created[0]}")


# ═══════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════

def main():
    import argparse
    p = argparse.ArgumentParser(
        description="Git Merge 深度诊断工具 — 纯 Python，零 PS 依赖")
    p.add_argument("--fix", action="store_true", help="自动修复")
    p.add_argument("--branch", default="", help="指定单个分支")
    p.add_argument("--phase", choices=["1","2","3","all"], default="all")
    args = p.parse_args()

    branches = [args.branch] if args.branch else BRANCHES
    orig_branch, _, _ = git("branch", "--show-current", timeout=5)

    for branch in branches:
        if args.phase in ("1", "all"):
            phase1_diagnose(branch)
        if args.phase in ("2", "all"):
            phase2_simulate_merge(branch)
        if args.fix and args.phase in ("3", "all"):
            phase3_fix(branch)

    # 回到原始分支
    if orig_branch and orig_branch != git("branch", "--show-current", timeout=5)[0]:
        git("checkout", orig_branch, timeout=10)

    print(f"\n{'=' * 70}")
    print("  诊断完毕")
    print("=" * 70)


if __name__ == "__main__":
    main()