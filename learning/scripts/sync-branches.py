"""
全 Python 分支提交与同步工作流 — 零 PS 依赖。
版本: v3.6  |  更新: 2026-10-22

───────────────────────────────────────────────────
  分支自动检测:
    master → 全流程（提交 → 合并到所有 feature → 推送 → 验证）
    其他   → 仅提交当前分支 + 推送

───────────────────────────────────────────────────
  完整用法:
    python scripts/sync-branches.py               # 自动检测分支
    python scripts/sync-branches.py --to master   # 先切到 master 再执行
    python scripts/sync-branches.py --to feat/comm # 先切到指定 feature
    python scripts/sync-branches.py -m "消息"     # 自定义提交信息
    python scripts/sync-branches.py --clean       # 仅清理所有分支删除残留
    python scripts/sync-branches.py --dry         # 预演（零副作用）

───────────────────────────────────────────────────
  内置保护:
    - safe_checkout: 工作区不干净拒绝切换，防止未提交修改被冲掉
    - 冲突自动解决: merge 冲突 → abort → -X theirs 重试（master 优先）
    - 合并三重清理: 3-pass 清理删除残留，末次 git checkout HEAD -- . 兜底
    - 清理后硬验证: 仍有残留则跳过推送，不传播脏状态
    - index.lock 自动清除: 启动时安全删除锁文件，不干扰 IDE
"""
import subprocess, os, sys, argparse
from pathlib import Path

REPO = Path(r"D:\dev\store\HandyControl")
FEATURE_BRANCHES = ["feat/communication", "feat/visualization", "feat/multi-module-verify"]
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

def git(*args, timeout=120):
    try:
        r = subprocess.run(["git"] + list(args), capture_output=True,
                           text=True, timeout=timeout, encoding="utf-8", errors="replace",
                           creationflags=_CREATEFLAGS, startupinfo=_STARTUP,
                           stdin=subprocess.DEVNULL)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT", -1
    except Exception as e:
        return "", str(e), -2


def lines(cmd, *args):
    out, _, _ = git(cmd, *args)
    return [l for l in out.split("\n") if l.strip()]


# ── 工作区查询 ─────────────────────────────────────────
def staged_del():
    return lines("diff", "--name-only", "--diff-filter=D", "--cached", "HEAD")

def unstaged_del():
    return lines("ls-files", "--deleted")

def modified():
    return lines("ls-files", "--modified")

def untracked():
    return lines("ls-files", "--others", "--exclude-standard")


# ── 输出 ───────────────────────────────────────────────
def ok(msg):
    print(f"  [OK] {msg}")
def fail(msg):
    print(f"  [FAIL] {msg}")
def info(msg):
    print(f"  [*] {msg}")
def hdr(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")
def show(label, items):
    if not items:
        return
    print(f"  [{label}] {len(items)} 个:")
    for i in items[:6]:
        print(f"      {i}")
    if len(items) > 6:
        print(f"      ... 还有 {len(items)-6} 个")


# ── 文件恢复 ───────────────────────────────────────────
def in_tree(ref, path):
    r = subprocess.run(["git", "cat-file", "-e", f"{ref}:{path}"],
                       capture_output=True, cwd=str(REPO),
                       creationflags=_CREATEFLAGS, startupinfo=_STARTUP, stdin=subprocess.DEVNULL)
    return r.returncode == 0


def restore(paths, dry=False):
    if dry:
        show("[dry] 将恢复", paths)
        return len(paths)
    n = 0
    for p in paths:
        if in_tree("HEAD", p):
            git("checkout", "HEAD", "--", p, timeout=10)
            n += 1
    return n


# ────────────────────────────────────────────────────────
# 清理删除残留
# ────────────────────────────────────────────────────────
def clean(dry=False):
    """清除暂存/未暂存删除，不触碰修改文件。返回处理数量。"""
    s, u = staged_del(), unstaged_del()
    if not s and not u:
        return 0
    show("staged_del", s)
    show("unstaged_del", u)
    if dry:
        return len(s) + len(u)
    for f in s:
        git("restore", "--staged", f, timeout=5)
    n = restore(u)
    if n:
        ok(f"恢复 {n} 个文件")
    left = staged_del() + unstaged_del()
    if left:
        for f in left:
            fail(f"残留: {f}")
    return n


# ────────────────────────────────────────────────────────
# 检查工作区是否干净（除删除外）
# ────────────────────────────────────────────────────────
def is_clean_workspace():
    """检查当前分支工作区是否干净（忽略未跟踪文件）。"""
    m, s, u = modified(), staged_del(), unstaged_del()
    return not m and not s and not u


# ────────────────────────────────────────────────────────
# 提交 + 推送（通用）
# ────────────────────────────────────────────────────────
def commit_and_push(branch, message=None, dry=False):
    """提交并推送。dry=True 时只打印计划。"""
    m, u, d = modified(), untracked(), staged_del() + unstaged_del()

    if dry:
        all_items = m + u + d
        show(f"[dry] {branch}: 待提交", all_items)
        if not all_items:
            info("[dry] 无待提交变更")
        return

    if d:
        info(f"恢复 {len(d)} 个被删文件")
        restore(d)

    to_add = modified() + untracked()
    if not to_add:
        info("无待提交变更")
        need = lines("rev-list", "--count", f"origin/{branch}..{branch}")
        if need and need[0] != "0":
            git("push", "origin", branch, timeout=60)
            ok(f"已推送 origin/{branch}")
        return

    show("待添加", to_add)
    git("add", *to_add, timeout=10)

    msg = message or f"chore: 更新 {', '.join(set(os.path.dirname(p) or p for p in to_add))[:60]}"
    out, err, rc = git("commit", "-m", msg, timeout=10)
    if rc == 0:
        ok(f"提交 [{branch}]: {msg}")
    elif "nothing" not in (out + err).lower():
        fail(f"提交失败: {err}")

    git("push", "origin", branch, timeout=60)
    ok(f"已推送 origin/{branch}")


# ────────────────────────────────────────────────────────
# 安全切换分支（仅当工作区干净时）
# ────────────────────────────────────────────────────────
def safe_checkout(target, dry=False):
    """切换到目标分支。若当前工作区不干净则中止。"""
    if not is_clean_workspace():
        m = modified()
        fail(f"工作区不干净 ({len(m)} modified)，先提交或撤销后再切换分支")
        sys.exit(1)
    git("checkout", target, timeout=10)


# ────────────────────────────────────────────────────────
# 合并 master → feature（含三重清理 + 兜底）
# ────────────────────────────────────────────────────────
def merge_to_features(dry=False):
    hdr("合并 master → feature")
    for branch in FEATURE_BRANCHES:
        print(f"\n  -- {branch} --")
        safe_checkout(branch, dry)
        clean(dry)

        out, err, rc = git("merge", "--no-edit", "--no-ff", MASTER, timeout=60)
        if "Already up to date" in (out + err):
            info("已是最新")
        elif rc != 0:
            # 自动处理冲突：先 abort 再用 -X theirs 重试（master 版本优先）
            if "CONFLICT" in (out + err) or "Automatic merge failed" in (out + err):
                fail(f"合并冲突，自动以 master 版本解决")
                git("merge", "--abort", timeout=10)
                out2, err2, rc2 = git("merge", "--no-edit", "--no-ff", "-X", "theirs", MASTER, timeout=60)
                if rc2 != 0:
                    fail(f"冲突解决失败: {err2}")
                    git("merge", "--abort", timeout=10)
                    continue
                ok("冲突已解决（采用 master 版本）")
            else:
                fail(f"合并失败: {err}")
                continue
        else:
            ok("合并成功")

        if dry:
            continue

        # ── 合并后清理（三重保险） ──
        for attempt in range(3):
            ps, pu = staged_del(), unstaged_del()
            if not ps and not pu:
                break
            info(f"合并后清理[pass {attempt+1}] staged={len(ps)} unstaged={len(pu)}")
            for f in ps:
                git("restore", "--staged", f, timeout=5)
            restore(ps + pu)
            if attempt == 2 and (staged_del() or unstaged_del()):
                fail("清理未完全生效，兜底恢复")
                git("checkout", "HEAD", "--", ".", timeout=60)

        # 最终检查：仍有残留则跳过推送
        final_s, final_u = staged_del(), unstaged_del()
        if final_s or final_u:
            fail(f"合并后仍有 {len(final_s)+len(final_u)} 个删除残留，跳过推送")
            for f in (final_s + final_u)[:5]:
                print(f"      {f}")
            continue

        git("push", "origin", branch, timeout=60)
        ok(f"已推送 origin/{branch}")


# ────────────────────────────────────────────────────────
# 全量验证
# ────────────────────────────────────────────────────────
def verify():
    hdr("最终验证")
    ok_all = True
    for branch in [MASTER] + FEATURE_BRANCHES:
        safe_checkout(branch)
        bad = staged_del() + unstaged_del()
        mods = modified()
        if bad:
            fail(f"{branch}: {len(bad)} 残留")
            for f in bad[:3]:
                print(f"      {f}")
            ok_all = False
        else:
            status = f"{len(mods)} modified" if mods else "干净"
            ok(f"{branch}: {status}")
    return ok_all


# ────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────
if __name__ == "__main__":
    p = argparse.ArgumentParser(description="分支提交与同步（自动检测分支）")
    p.add_argument("--clean", action="store_true", help="仅清理所有分支删除残留")
    p.add_argument("--dry", action="store_true", help="预演（零副作用）")
    p.add_argument("-m", "--message", type=str, default=None, help="自定义提交信息")
    p.add_argument("--to", type=str, default=None, metavar="BRANCH",
                   help="先切换到指定分支再执行（等同于 git checkout）")
    args = p.parse_args()

    # 清锁（仅删锁文件，不杀进程避免干扰 IDE）
    lock = REPO / ".git" / "index.lock"
    if lock.exists():
        try:
            lock.unlink()
        except PermissionError:
            info("index.lock 被占用，尝试释放...")
            import time
            time.sleep(1)
            try:
                lock.unlink()
            except PermissionError:
                fail("无法释放 index.lock，请关闭其他 Git 操作后重试")
                sys.exit(1)

    orig, _, _ = git("branch", "--show-current", timeout=5)
    if not orig:
        fail("无法获取当前分支")
        sys.exit(1)

    # ── --to: 切换分支 ──
    if args.to:
        if not is_clean_workspace():
            fail(f"工作区不干净，先提交或撤销后再切换 (--to {args.to})")
            sys.exit(1)
        git("checkout", args.to, timeout=10)
        orig = args.to
        ok(f"已切换到 {orig}")

    # ── --clean ──
    if args.clean:
        hdr("仅清理删除残留")
        for branch in [MASTER] + FEATURE_BRANCHES:
            safe_checkout(branch, args.dry)
            print(f"\n  -- {branch} --")
            n = clean(args.dry)
            ok(f"{'[dry] 将清理' if args.dry else '已清理'} {n} 个")
        safe_checkout(orig, args.dry)
        sys.exit(0)

    # ── 提交（根据分支自动决定后续） ──
    on_master = (orig == MASTER)
    mode = "全流程" if on_master else f"仅提交 ({orig})"
    hdr(f"模式: {mode} {'[预演]' if args.dry else ''}")

    if args.dry:
        commit_and_push(orig, args.message, dry=True)
        if on_master:
            info("[dry] 随后将合并到 feature 分支")
        sys.exit(0)

    commit_and_push(orig, args.message)

    if not on_master:
        print(f"\n{'='*60}\n  提交完毕 ({orig})\n{'='*60}")
        sys.exit(0)

    # master 全流程
    for branch in FEATURE_BRANCHES:
        safe_checkout(branch)
        print(f"\n  -- {branch} --")
        clean()

    merge_to_features()
    verify()
    safe_checkout(orig)
    print(f"\n{'='*60}\n  同步完毕\n{'='*60}")