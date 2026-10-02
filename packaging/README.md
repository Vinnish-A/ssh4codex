# Release 构建与发布

发行平台：Linux x86_64 / WSL，glibc 2.35+（Ubuntu 22.04+）。本地/CI 使用 Ubuntu 22.04 与 Python 3.10 构建，依赖锁在 requirements.txt。

```bash
python3 -m venv .build-venv
.build-venv/bin/pip install -r packaging/requirements.txt
SSH4CODEX_BUILD_PYTHON=.build-venv/bin/python bash packaging/build.sh
```

PyInstaller onedir 含 Python/MCP；tools/ 包含 OpenSSH 和非 glibc 动态库。下载用户不需要 Python/pip/venv/OpenSSH 安装，仍需标准 Linux/glibc；远端需要 Python/tmux。运行时不能携带维护者配置、私钥、known_hosts 或本地日志。

输出 release-assets/ssh4codex-linux-x86_64.tar.gz 和 .sha256。`install.sh` 验证后安装版本目录并切换 symlink；保留旧版本，不影响运行中的进程。源码方式仍可 pip install -e '.[mcp]'。

在 main 上提交并推送同版本 vX.Y.Z 标签，GitHub Actions 执行单元/离线运行时测试，构建 Release 并上传包与校验文件。发布前真实 SSH/MCP/产物测试由维护者进行，CI 不接触真实私钥。

`tests/release.py` 将客户端 PATH 设为 `/nonexistent`，验证版本、无内置部署配置、内置 SSH、stdio MCP 初始化/工具发现/配置错误和正常 EOF 退出。依赖使用已锁定的 pydantic-settings 2.12.0，避免更新版本针对 MCP 泛型 lifespan 注解的无关初始化警告。
