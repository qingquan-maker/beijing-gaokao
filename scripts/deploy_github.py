#!/usr/bin/env python3
"""一键把本仓库发布到 GitHub Pages。

为什么用 REST API 上传而不是 `git push`
--------------------------------------
本机环境下 git 的 HTTPS 通道在受限沙箱里不可用：
  * 直连 GitHub            → Connection was reset
  * 走系统代理 + schannel  → SEC_E_NO_CREDENTIALS（取不到 TLS 凭据）
  * 走系统代理 + openssl   → 公共仓库能读（ls-remote 成功）
  * 需要认证时             → git 会拉起自带的 sh.exe 取凭据，
                             而沙箱禁止创建管道：couldn't create signal pipe, Win32 error 5
而 Python 的 urllib 能正常访问 api.github.com。所以改为用 Git Data API
（blobs → tree → commit → ref）直接把文件写进仓库，完全不依赖 git 的认证链路。

做的事情：
  0. 探测网络通道（系统代理 + git 的 TLS 后端）
  1. 校验 token 并取回账号信息
  2. 创建（或复用）仓库
  3. 用 Git Data API 上传 main 分支
  4. 把 Actions 的 GITHUB_TOKEN 权限设为 write
     （否则定时任务里的 git push 提交数据会失败，这是最容易踩的坑）
  5. 开启 Pages 并把构建源设为 GitHub Actions（build_type=workflow）
  6. 触发一次工作流，轮询直到跑完，打印最终网址

用法（token 只从环境变量读，不落命令行历史）：
    $env:GH_TOKEN = "ghp_xxx"
    python scripts/deploy_github.py --repo-name beijing-gaokao
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"
WORKFLOW_FILE = "update-data.yml"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# --------------------------------------------------------------------------- 网络通道

def detect_proxy() -> str:
    """探测可用的 HTTP 代理。

    Python 的 urllib 会自动读 Windows 注册表里的系统代理，所以 Python 能连 GitHub；
    而 git 不读注册表，直连会被 reset。这里把系统代理找出来显式喂给 git。
    """
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                "ALL_PROXY", "all_proxy"):
        value = os.environ.get(key)
        if value:
            return value.strip()
    if sys.platform == "win32":
        try:
            import winreg

            path = (r"Software\Microsoft\Windows\CurrentVersion"
                    r"\Internet Settings")
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
                if not enabled:
                    return ""
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
            server = (server or "").strip()
            if not server:
                return ""
            if "=" not in server:                 # 形如 127.0.0.1:7890
                return f"http://{server}"
            parts = dict(item.split("=", 1)      # 形如 http=a:1;https=b:2
                         for item in server.split(";") if "=" in item)
            host = parts.get("https") or parts.get("http")
            return f"http://{host}" if host else ""
        except Exception:
            return ""
    return ""


PROXY = ""
SSL_BACKEND = ""


def git_cfg_args() -> list[str]:
    """给 git 注入代理与 TLS 后端（只在命令行生效，不写配置文件）。"""
    args: list[str] = []
    if PROXY:
        args += ["-c", f"http.proxy={PROXY}", "-c", f"https.proxy={PROXY}"]
    if SSL_BACKEND:
        args += ["-c", f"http.sslBackend={SSL_BACKEND}"]
    return args


def run_git(args: list[str], check: bool = True) -> str:
    """只用于本地操作（ls-files / remote add / branch）。不做任何需要认证的网络操作。"""
    proc = subprocess.run(
        ["git", *git_cfg_args(), *args],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and proc.returncode != 0:
        raise SystemExit(f"[失败] git {' '.join(args)}\n{proc.stderr.strip()[:500]}")
    return (proc.stdout or "") + (proc.stderr or "")


# --------------------------------------------------------------------------- API

def api(
    method: str,
    path: str,
    token: str,
    body: dict | None = None,
    *,
    allow: tuple[int, ...] = (),
    timeout: int = 60,
) -> tuple[int, dict | list | None]:
    url = path if path.startswith("http") else f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "gaokao-deploy")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = {"message": raw.decode("utf-8", "replace")[:300]}
        if exc.code in allow:
            return exc.code, payload
        message = (payload or {}).get("message", "") if isinstance(payload, dict) else ""
        raise SystemExit(f"[失败] {method} {url} -> HTTP {exc.code} {message}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"[失败] 无法连接 GitHub: {exc.reason}")


def local_files() -> list[str]:
    out = run_git(["ls-files"])
    return [line.strip() for line in out.splitlines() if line.strip()]


def upload_via_api(token: str, full: str, message: str) -> str:
    """用 Git Data API 把本地已提交的文件写入仓库，返回新 commit 的 sha。"""
    files = local_files()
    if not files:
        raise SystemExit("[失败] 本地没有已提交的文件（先 git add + git commit）")

    print(f"  待上传 {len(files)} 个文件")
    tree_entries = []
    for index, rel in enumerate(files, 1):
        payload = (ROOT / rel).read_bytes()
        _, blob = api("POST", f"/repos/{full}/git/blobs", token, {
            "content": base64.b64encode(payload).decode("ascii"),
            "encoding": "base64",
        })
        if not isinstance(blob, dict) or "sha" not in blob:
            raise SystemExit(f"[失败] 创建 blob 失败: {rel}")
        tree_entries.append({
            "path": rel.replace("\\", "/"),
            "mode": "100644",
            "type": "blob",
            "sha": blob["sha"],
        })
        if index % 10 == 0 or index == len(files):
            print(f"    已上传 {index}/{len(files)}")

    _, tree = api("POST", f"/repos/{full}/git/trees", token, {"tree": tree_entries})
    if not isinstance(tree, dict) or "sha" not in tree:
        raise SystemExit("[失败] 创建 tree 失败")

    parents: list[str] = []
    code, ref = api("GET", f"/repos/{full}/git/ref/heads/main", token, allow=(404, 409))
    if code == 200 and isinstance(ref, dict):
        parents = [ref["object"]["sha"]]
        print("  目标分支已存在，将创建新提交（保留历史）")

    _, commit = api("POST", f"/repos/{full}/git/commits", token, {
        "message": message,
        "tree": tree["sha"],
        "parents": parents,
    })
    if not isinstance(commit, dict) or "sha" not in commit:
        raise SystemExit("[失败] 创建 commit 失败")

    if parents:
        api("PATCH", f"/repos/{full}/git/refs/heads/main", token,
            {"sha": commit["sha"], "force": True})
    else:
        api("POST", f"/repos/{full}/git/refs", token,
            {"ref": "refs/heads/main", "sha": commit["sha"]})
    return commit["sha"]


# --------------------------------------------------------------------------- 主流程

def main() -> int:
    parser = argparse.ArgumentParser(description="把本仓库发布到 GitHub Pages")
    parser.add_argument("--repo-name", default="beijing-gaokao",
                        help="仓库名（默认 beijing-gaokao）")
    parser.add_argument("--private", action="store_true",
                        help="建私有仓库（注意：免费账号的 Pages 只能从公开仓库发布）")
    parser.add_argument("--description",
                        default="北京高考志愿录取数据查询（历年分数/位次/招生计划/难度标注）")
    parser.add_argument("--message", default="feat: 北京高考志愿录取数据查询站点")
    parser.add_argument("--no-trigger", action="store_true", help="不触发首次工作流")
    parser.add_argument("--proxy", default=None,
                        help="HTTP 代理，如 http://127.0.0.1:7890（默认自动探测）")
    parser.add_argument("--ssl-backend", default=None, choices=["openssl", "schannel"],
                        help="git 的 TLS 后端（Windows 上默认 openssl）")
    args = parser.parse_args()

    global PROXY, SSL_BACKEND
    PROXY = (args.proxy if args.proxy is not None else detect_proxy()).strip()
    SSL_BACKEND = args.ssl_backend or ("openssl" if sys.platform == "win32" else "")

    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        print("错误：请先设置环境变量 GH_TOKEN", file=sys.stderr)
        return 2

    print("== 0/6 网络通道 ==")
    print(f"  代理     : {PROXY or '（直连）'}")
    print(f"  TLS 后端 : {SSL_BACKEND or '（git 默认）'}")
    print("  上传方式 : GitHub REST API（不依赖 git 的认证链路）")

    # ---- 1) 校验 token ----
    print("\n== 1/6 校验 token ==")
    status, user = api("GET", "/user", token)
    if not isinstance(user, dict) or "login" not in user:
        raise SystemExit("[失败] token 无效")
    owner = user["login"]
    print(f"  已登录：{owner}")

    try:
        req = urllib.request.Request(f"{API}/user", method="GET")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("User-Agent", "gaokao-deploy")
        with urllib.request.urlopen(req, timeout=20) as resp:
            scopes = resp.headers.get("x-oauth-scopes", "")
        if scopes:
            print(f"  权限范围：{scopes}")
    except Exception:
        pass

    repo = args.repo_name
    full = f"{owner}/{repo}"

    # ---- 2) 创建或复用仓库 ----
    print(f"\n== 2/6 创建仓库 {full} ==")
    code, _ = api("GET", f"/repos/{full}", token, allow=(404,))
    if code == 200:
        print("  仓库已存在，直接复用")
    else:
        api("POST", "/user/repos", token, {
            "name": repo,
            "description": args.description,
            "private": bool(args.private),
            "auto_init": False,       # 保持空仓库，首次提交直接成为 main
            "has_issues": True,
            "has_wiki": False,
        })
        print(f"  已创建（{'私有' if args.private else '公开'}）")
    if args.private:
        print("  !! 免费账号的 GitHub Pages 只能从**公开**仓库发布，私有仓库可能没有网址")

    # ---- 3) 上传 ----
    print("\n== 3/6 上传文件 ==")
    sha = upload_via_api(token, full, args.message)
    print(f"  完成，commit {sha[:10]}")
    # 顺便把 origin 配好，方便你以后在本地查看（不影响本次上传）
    run_git(["remote", "remove", "origin"], check=False)
    run_git(["remote", "add", "origin", f"https://github.com/{full}.git"], check=False)
    run_git(["branch", "-M", "main"], check=False)

    # ---- 4) 允许工作流写仓库 ----
    print("\n== 4/6 开启工作流写权限 ==")
    code, _ = api("PUT", f"/repos/{full}/actions/permissions/workflow", token, {
        "default_workflow_permissions": "write",
        "can_approve_pull_request_reviews": False,
    }, allow=(403, 404, 422))
    if code == 200:
        print("  已设为 read-write（定时任务才能把数据提交回仓库）")
    else:
        print(f"  未能自动设置（HTTP {code}）。请手动到 Settings → Actions → "
              f"General → Workflow permissions 选 Read and write")

    # ---- 5) 开启 Pages ----
    print("\n== 5/6 开启 GitHub Pages ==")
    code, _ = api("POST", f"/repos/{full}/pages", token,
                  {"build_type": "workflow"}, allow=(409, 422))
    if code in (201, 204):
        print("  已开启，构建源 = GitHub Actions")
    elif code == 409:
        api("PUT", f"/repos/{full}/pages", token, {"build_type": "workflow"},
            allow=(204, 422))
        print("  Pages 原本已存在，已把构建源设为 GitHub Actions")
    else:
        print(f"  自动开启未成功（HTTP {code}）。请手动到 Settings → Pages → "
              f"Source 选 GitHub Actions")

    site = f"https://{owner.lower()}.github.io/{repo}/"

    # ---- 6) 触发首次运行 ----
    if args.no_trigger:
        print("\n== 6/6 跳过触发（--no-trigger）==")
    else:
        print("\n== 6/6 触发首次数据更新 ==")
        code, _ = api(
            "POST", f"/repos/{full}/actions/workflows/{WORKFLOW_FILE}/dispatches",
            token, {"ref": "main"}, allow=(404, 422),
        )
        if code in (204, 201):
            print("  已触发，等待运行结果…")
            seen = False
            for _ in range(25):
                time.sleep(6)
                code, runs = api(
                    "GET",
                    f"/repos/{full}/actions/workflows/{WORKFLOW_FILE}/runs?per_page=1",
                    token, allow=(404,),
                )
                if code == 200 and isinstance(runs, dict) and runs.get("workflow_runs"):
                    run = runs["workflow_runs"][0]
                    seen = True
                    if run["status"] == "completed":
                        print(f"  结果：{run['conclusion']}")
                        print(f"  详情：{run['html_url']}")
                        if run["conclusion"] != "success":
                            print("  !! 首次运行未成功；网址会在下次成功运行后可用")
                        break
                    print(f"  进行中…（{run['status']}）")
            if not seen:
                print("  未取到运行状态，请到仓库的 Actions 页面查看")
        else:
            print(f"  触发失败（HTTP {code}）；可到 Actions 页面手动点 Run workflow")

    print("\n" + "=" * 62)
    print("  你的公开网址：")
    print(f"    {site}")
    print("")
    print(f"  仓库：https://github.com/{full}")
    print("  提示：Pages 首次部署需 1~2 分钟；若 404 稍等再刷新。")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
