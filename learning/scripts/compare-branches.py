"""
分支差异比对工具 — 纯 Python，零 PS 依赖。
版本: v1.0  |  更新: 2026-10-23

───────────────────────────────────────────────────
  用途：比对两个分支的文件级差异、分支独有文件、NuGet 包差异，
        替代易卡死终端的 PS 管道脚本。
  适用场景：
    - 骨架合并前预检：确认 master 变更会碰哪些高危文件
    - 合并后验证：确认分支专属代码未被覆盖
    - 日常诊断：快速了解两个分支的差异范围

───────────────────────────────────────────────────
  用法:
    python scripts/compare-branches.py                          # master vs 当前分支（全量）
    python scripts/compare-branches.py -b master -t feat/visualization
    python scripts/compare-branches.py -b feat/comm -t feat/viz
    python scripts/compare-branches.py --packages-only          # 仅比对 NuGet 包
    python scripts/compare-branches.py --files-only             # 仅比对文件
    python scripts/compare-branches.py --summary                # 仅输出摘要

───────────────────────────────────────────────────
  输出阶段:
    Phase 1: 文件级差异   — git diff --name-status (A/M/D)
    Phase 2: 分支独有文件  — git ls-tree 全集差集对比
    Phase 3: NuGet 包差异  — csproj PackageReference + CPVM 版本
    Phase 4: 简要统计      — 提交领先数 + 变更行数

───────────────────────────────────────────────────
  与骨架合并工作流的配合:
    合并前: compare-branches.py → 判断高危文件 → 记录当前 diff
    合并后: compare-branches.py → 二次确认 → dotnet build 验证
  详见 .trae/rules/skeleton-change-workflow.md「高危共享文件清单」
"""
import subprocess, os, sys, argparse, xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(r"D:\dev\store\jia-skills\wpf-prism-docs")
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
    """执行 git 命令，返回 (stdout, stderr, returncode)。"""
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


def show_file_at(branch, path):
    """获取指定分支上某文件的内容（字符串），不存在返回 None。"""
    out, _, rc = git("show", f"{branch}:{path}", timeout=10)
    return out if rc == 0 else None


def file_exists_on(branch, path):
    """检查文件在指定分支是否存在。"""
    _, _, rc = git("cat-file", "-e", f"{branch}:{path}", timeout=10)
    return rc == 0


# ═══════════════════════════════════════════
#  Phase 1: 文件级差异
# ═══════════════════════════════════════════

def compare_files(base, target):
    """比对两个分支的文件变更（A/M/D）。"""
    print(f"\n{'=' * 70}")
    print(f"  文件级差异: {base} → {target}")
    print(f"{'=' * 70}")

    out, _, _ = git("diff", "--name-status", f"{base}...{target}", timeout=60)
    if not out:
        print("  无差异")
        return [], [], []

    added, modified, deleted = [], [], []
    for line in out.split("\n"):
        if not line or line.startswith("warning:"):
            continue
        parts = line.split("\t", 1)
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[1]
        if status.startswith("A"):
            added.append(path)
        elif status.startswith("D"):
            deleted.append(path)
        elif status.startswith("M") or status.startswith("R"):
            modified.append(path)

    if added:
        print(f"\n  [+] 新增 ({len(added)}):")
        for f in added:
            print(f"      {f}")

    if modified:
        print(f"\n  [~] 修改 ({len(modified)}):")
        for f in modified:
            print(f"      {f}")

    if deleted:
        print(f"\n  [-] 删除 ({len(deleted)}):")
        for f in deleted:
            print(f"      {f}")

    return added, modified, deleted


# ═══════════════════════════════════════════
#  Phase 2: 分支独有文件
# ═══════════════════════════════════════════

def compare_unique_files(base, target):
    """找出分支独有文件——目标有而基分支没有，以及基分支有而目标没有。"""
    print(f"\n{'=' * 70}")
    print(f"  分支独有文件")
    print(f"{'=' * 70}")

    # 获取双方文件树快照
    base_files = set()
    base_out, _, _ = git("ls-tree", "-r", "--name-only", base, timeout=30)
    if base_out:
        base_files = set(base_out.split("\n"))

    target_files = set()
    target_out, _, _ = git("ls-tree", "-r", "--name-only", target, timeout=30)
    if target_out:
        target_files = set(target_out.split("\n"))

    only_target = sorted(target_files - base_files)
    only_base = sorted(base_files - target_files)

    if only_target:
        print(f"\n  [{target}] 独有 ({len(only_target)}):")
        for f in only_target:
            print(f"      + {f}")

    if only_base:
        print(f"\n  [{base}] 独有 ({len(only_base)}):")
        for f in only_base:
            print(f"      - {f}")

    if not only_target and not only_base:
        print("  两侧文件树完全一致")

    return only_target, only_base


# ═══════════════════════════════════════════
#  Phase 3: NuGet 包差异
# ═══════════════════════════════════════════

def extract_packages(branch, csproj_path):
    """从指定分支的 csproj 中提取 PackageReference 列表。"""
    content = show_file_at(branch, csproj_path)
    if content is None:
        return set()

    packages = set()
    try:
        root = ET.fromstring(content)
        for ref in root.iter("PackageReference"):
            name = ref.attrib.get("Include", "")
            version = ref.attrib.get("Version", "")
            if name:
                packages.add((name, version))
    except ET.ParseError:
        pass
    return packages


def compare_packages(base, target):
    """比对两个分支所有 csproj 的 NuGet 包差异。"""
    print(f"\n{'=' * 70}")
    print(f"  NuGet 包差异: {base} → {target}")
    print(f"{'=' * 70}")

    # 收集所有 csproj 文件
    csproj_out, _, _ = git("ls-tree", "-r", "--name-only", target,
                           "templates/", timeout=30)
    csproj_files = [f for f in csproj_out.split("\n")
                    if f.endswith(".csproj")] if csproj_out else []

    total_diffs = 0
    for csproj in sorted(csproj_files):
        base_pkgs = extract_packages(base, csproj)
        target_pkgs = extract_packages(target, csproj)

        base_names = {p[0] for p in base_pkgs}
        target_names = {p[0] for p in target_pkgs}

        added = target_names - base_names
        removed = base_names - target_names

        if not added and not removed:
            continue

        total_diffs += 1
        print(f"\n  [{csproj}]")

        if added:
            for name in sorted(added):
                ver = next((v for n, v in target_pkgs if n == name), "?")
                print(f"      + {name} = {ver}")

        if removed:
            for name in sorted(removed):
                ver = next((v for n, v in base_pkgs if n == name), "?")
                print(f"      - {name} = {ver}")

    if total_diffs == 0:
        print("  所有项目包引用完全一致")

    # 也检查 Directory.Packages.props
    _compare_cpvm_versions(base, target)
    return total_diffs


def _compare_cpvm_versions(base, target):
    """比对 CPVM（Directory.Packages.props）中的包版本差异。"""
    props_path = "templates/Directory.Packages.props"
    base_content = show_file_at(base, props_path)
    target_content = show_file_at(target, props_path)

    if base_content is None or target_content is None:
        return

    def parse_versions(xml_str):
        versions = {}
        try:
            root = ET.fromstring(xml_str)
            for pv in root.iter("PackageVersion"):
                name = pv.attrib.get("Include", "")
                ver = pv.attrib.get("Version", "")
                if name and ver:
                    versions[name] = ver
        except ET.ParseError:
            pass
        return versions

    base_ver = parse_versions(base_content)
    target_ver = parse_versions(target_content)

    changed = {name for name in set(base_ver) & set(target_ver)
               if base_ver[name] != target_ver[name]}
    added = set(target_ver) - set(base_ver)
    removed = set(base_ver) - set(target_ver)

    if changed or added or removed:
        print(f"\n  [Directory.Packages.props (CPVM)]")

        if changed:
            for name in sorted(changed):
                print(f"      ~ {name}: {base_ver[name]} → {target_ver[name]}")

        if added:
            for name in sorted(added):
                print(f"      + {name} = {target_ver[name]}")

        if removed:
            for name in sorted(removed):
                print(f"      - {name} = {base_ver[name]}")


# ═══════════════════════════════════════════
#  Phase 4: 简要统计
# ═══════════════════════════════════════════

def show_summary(base, target):
    """输出差异摘要（总行数统计）。"""
    print(f"\n{'=' * 70}")
    print(f"  摘要: {base} → {target}")
    print(f"{'=' * 70}")

    # 提交数差异
    ahead, _, _ = git("rev-list", "--count", f"{base}..{target}", timeout=30)
    behind, _, _ = git("rev-list", "--count", f"{target}..{base}", timeout=30)

    # 文件统计
    diff_stat, _, _ = git("diff", "--stat", f"{base}...{target}", timeout=60)
    lines_changed = diff_stat.split("\n")[-1] if diff_stat else "0"

    print(f"  目标比基分支领先 {ahead or '0'} 个提交")
    print(f"  基分支比目标领先 {behind or '0'} 个提交")
    print(f"  变更: {lines_changed}")


# ═══════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="分支差异比对工具 — 纯 Python，零 PS 依赖")
    parser.add_argument("-b", "--base", default="master",
                        help="基分支 (默认: master)")
    parser.add_argument("-t", "--target",
                        help="目标分支 (默认: 当前分支)")
    parser.add_argument("--packages-only", action="store_true",
                        help="仅比对 NuGet 包")
    parser.add_argument("--files-only", action="store_true",
                        help="仅比对文件")
    parser.add_argument("--summary", action="store_true",
                        help="仅输出摘要")
    args = parser.parse_args()

    # 确定目标分支
    if args.target is None:
        current, _, rc = git("rev-parse", "--abbrev-ref", "HEAD", timeout=10)
        args.target = current if rc == 0 else "HEAD"

    # 验证分支存在
    for branch in [args.base, args.target]:
        _, _, rc = git("rev-parse", "--verify", branch, timeout=10)
        if rc != 0:
            print(f"✗ 分支不存在: {branch}")
            sys.exit(1)

    print(f"  基分支:  {args.base}")
    print(f"  目标分支: {args.target}")

    all_modes = not (args.packages_only or args.files_only or args.summary)

    if all_modes or args.files_only:
        compare_files(args.base, args.target)
        compare_unique_files(args.base, args.target)

    if all_modes or args.packages_only:
        compare_packages(args.base, args.target)

    if all_modes or args.summary:
        show_summary(args.base, args.target)

    print()


if __name__ == "__main__":
    main()