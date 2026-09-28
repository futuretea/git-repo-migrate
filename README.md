# git-repo-migrate

仓库迁移小工具集：把仓库从 Codeup（云效）或 GitHub Enterprise 迁到 GitHub 或 GitLab，
并在迁移后做逐仓 refs 核对。仅用 Python 标准库（≥ 3.9）+ `git`。

## 工具

| 工具 | 方向 | 说明 |
|------|------|------|
| `codeup2github.py` | Codeup → GitHub | 按代码组（含子组）批量迁移；支持 GitHub Enterprise |
| `github2gitlab.py` | GitHub Enterprise → GitLab | 迁到 token 用户的个人命名空间；支持克隆/推送分离 |
| `verify_migration.py` | 核对 | 源端 refs vs 目标端 refs；快照（本地克隆）与实时（API）两口径 |

共同流程：枚举仓库 → `git clone --bare` → 目标端建私有项目 → `git push --mirror` → 写运行清单。

## 快速开始

```bash
cp github2gitlab.example.ini github2gitlab.ini   # 填 base_url 与 token
python3 github2gitlab.py --dry-run               # 先看仓库与目标路径清单，不做任何改动
python3 github2gitlab.py --yes                   # 正式迁移（默认先全量克隆、再全量推送）
python3 verify_migration.py                      # 核对（默认快照口径；[verify] source = api 为实时口径）
```

令牌也可以不落盘：`GHE_TOKEN` / `GITLAB_TOKEN`（Codeup 工具对应 `CODEUP_ACCESS_TOKEN` / `GITHUB_TOKEN`）。

### 克隆 / 推送分离（单侧可达的网络窗口）

```bash
python3 github2gitlab.py --clone-only             # 只连源端；本地已与源端一致的仓跳过不重传
python3 github2gitlab.py --push-only --yes        # 只连目标端，从本地名单推送
```

- `--clone-only` 把「哪些本地克隆已验证完整」写进 `<clone_dir>/migrate-repos.json`；
- `--push-only` 不访问源端，只推被验证过的克隆（半成品或空克隆会被拒绝并记账为失败）；
- 运行清单 `migrate-manifest.txt` 是全程账本：补跑只刷新本次触及的行，其余保留。

## 退出码

`0` 成功 · `1` 有迁移失败 · `2` 用法或配置错误 · `130` 用户取消

## 测试

```bash
python3 -m unittest          # 全部离线用例（无网络、无真实端点）
```

## 安全说明

- 含令牌的 `*.ini`（`github2gitlab.ini`、`migrate.ini`）已在 `.gitignore` 中，请勿提交；
  发布或分享时用干净 clone / `git archive`，不要打包整个工作目录。
- `--push-only` / 默认流程的 `git push --mirror` 会覆盖目标端已有内容：只有交互运行（stdin 与 stdout
  都是 TTY）且未加 `--yes` 时才弹确认；脚本 / CI 这类非交互运行不会弹确认，会直接执行并覆盖目标——
  请在运行前自行核对目标集合（例如先跑 `--dry-run`），需要无条件跳过确认时用 `--yes`。
- 目标端同名仓库/项目的可见性不会被修改：推送前会确认既有目标为私有
  （`codeup2github.py` 要求 `private=true` 且 `visibility=private`，实例内部可见的仓库同样拒绝；
  `github2gitlab.py` 要求 `visibility=private`），否则该仓库记为失败并跳过，不会把私有历史推进公开仓库。
- 私有化部署自签证书场景可在配置里开 `insecure_tls`（仅限受信内网）。
