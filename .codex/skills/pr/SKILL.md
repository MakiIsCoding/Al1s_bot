---
name: pr
description: "Create a GitHub Pull Request from the current branch. Use when the user types /pr, prskill, pr skill, or asks to ensure branch is committed+pushed (via gm), generate PR title/body from actual git diff, and create a PR via gh or GitHub REST API."
---

# pr Skill

当用户使用 `/pr`、`prskill`、`pr skill` 时，直接执行 GitHub Pull Request 创建流程。

目标结果：
1. 检查当前分支是否适合发起 PR
2. 如有未提交或未推送变更，先自动执行 `gm` 流程
3. 分析当前分支相对目标分支的改动
4. 生成合适的 PR 标题与正文
5. 调用 GitHub 能力创建 PR

## 关键结论

仅依赖本地 Git 信息无法真正“创建” GitHub PR。真正创建 PR 必须调用 GitHub 服务：
1. `gh pr create`
2. GitHub REST API

因此标准策略是：优先 `gh pr create`，否则使用 GitHub API；若都不可用则输出标题与正文并说明阻塞项。

## 目标分支规则（base）

默认目标分支优先级：
1. 用户显式指定的目标分支
2. 仓库默认分支（`git symbolic-ref refs/remotes/origin/HEAD`）
3. `master`

建议推导：
```bash
git symbolic-ref refs/remotes/origin/HEAD
```

## 创建前检查（必须执行）

```bash
git branch --show-current
git status --short --branch
git remote get-url origin
git log --oneline --decorate -n 10
```

检查规则：
- 若当前分支是 `master` 或 `main`：停止执行，提示应在功能分支发起 PR
- 若有未提交修改：自动执行 `gm`
- 若本地提交尚未 push：自动执行 `gm` 的 push 阶段
- 若没有 `origin`：停止并说明缺少远端仓库

## 差异分析（必须执行）

```bash
git diff <base>...HEAD --name-only
git diff <base>...HEAD --stat
git log <base>..HEAD --pretty=format:"%s"
git diff <base>...HEAD
```

根据 diff 判断 PR 类型：`功能` / `修复` / `文档` / `重构` / `测试` / `维护`。

## PR 标题规范

```text
【<类型>】<描述>
```

## PR 正文规范

```markdown
## 变更概述
- 简述本次改动解决的问题或新增的能力

## 主要修改
- 列出关键文件和关键改动点

## 测试与验证
- 说明已执行的验证
- 如果未测试，明确写明未测试

## 风险与注意事项
- 标明可能的影响范围
```

## GitHub 仓库推导

从 `git remote get-url origin` 推导 `<owner>/<repo>`，支持：
- `https://github.com/<owner>/<repo>.git`
- `git@github.com:<owner>/<repo>.git`

## 创建 PR 的执行策略

### 方案 A：使用 gh

```bash
gh pr create --base <base> --head <current-branch> --title "<title>" --body "<body>"
```

### 方案 B：使用 GitHub API

- 使用 `GITHUB_TOKEN` 环境变量（不得写入仓库）

```text
POST https://api.github.com/repos/<owner>/<repo>/pulls
```

### 方案 C：无法创建

输出已生成的标题与正文，并说明缺失项：`gh` / `GITHUB_TOKEN` / 网络权限。

## 注意事项

- 不要本地 squash/rebase/merge 到 `master`
- 不要把 GitHub Token 写入仓库
- 标题/正文必须贴合实际 diff，禁止空泛模板