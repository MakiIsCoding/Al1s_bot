---
name: gm
description: "Run a full Git commit+push workflow automatically. Use when the user types /gm, gmskill, gm skill, or asks to stage all changes, generate an accurate commit message from the actual diff, commit, and push."
---

# gm Skill

当用户使用 `/gm`、`gmskill`、`gm skill` 时，直接执行完整 Git 提交流程（默认不反问确认）。

## 执行流程

### 1) 读取状态（必须先执行并分析）

```bash
git status --short --branch
git branch --show-current
git config user.name
git diff --stat
git diff --cached --stat
```

- 如果没有任何变更：告知用户无需提交并停止执行。

### 2) 判断分支

- 读取当前分支：

```bash
git branch --show-current
```

- 如果当前分支是 `master` 或 `main`：
  - 必须根据改动内容判断分支类型
  - 自动创建新分支并切换过去
  - 分支名格式：`<type>/<git-user-name>/<short-description>`
    - `<type>`：优先使用仓库已有管理方式：`feat/`、`fix/`、`docs/`、`refactor/`、`test/`、`chore/`
    - `git-user-name`：来自 `git config user.name`
    - `short-description`：英文、小写、下划线，避免空格和中文

示例：

```bash
git checkout -b feat/MakiIsCoding/ai_skill_refactor
```

- 如果当前分支不是 `master` 且不是 `main`：
  - 必须直接使用当前分支名
  - 不重新命名分支
  - 不自动创建新分支
  - push 时直接推送当前分支到远端

### 3) 暂存代码（必须执行）

```bash
git add -A
```

### 4) 生成提交信息（必须基于实际 diff）

生成 commit message 前必须读取：

```bash
git diff --cached --stat
git diff --cached
```

提交信息格式：

- `【类型】(<范围>)<描述>`
- 无合适范围可省略：`【类型】<描述>`

允许的类型：`功能`、`修复`、`文档`、`重构`、`测试`、`维护`

生成规则：
- 必须根据实际 diff 生成，描述要具体
- 禁止泛化描述（例如“更新代码/修复问题/提交修改/feat: update code”等）

示例：
- `【功能】(ai)新增 Git 提交与 PR 创建 skill 流程`
- `【修复】(auth)修复登录态校验空指针问题`
- `【文档】(skill)更新 gm 与 pr 的使用说明`
- `【重构】(service)拆分任务处理逻辑并收敛公共方法`

### 5) 提交

```bash
git commit -m "<生成的提交信息>"
```

- 如果没有可提交内容：停止并说明原因
- 不要默认执行 `git commit --amend`
- 不要默认执行强推
- 不要修改用户已有的无关提交历史

### 6) 推送

优先执行：

```bash
git push -u origin <current-branch>
```

- 如果当前分支不是 `master` 且不是 `main`，`<current-branch>` 必须就是当前本地分支名
- 如果当前分支已跟踪远端，也可以执行：`git push`
- 仅在 push 因“上游分支不存在”失败时，改为 `git push -u origin <branch>`
- 如果远端认证失败：明确告诉用户需要补齐本机/服务器 Git 凭证

## 输出要求

执行完成后汇总：
- 当前分支名
- 最终提交信息
- 是否已成功 push
- 对应远端分支名

## 默认不反问（除非明确风险）

仅在以下情况停下来询问：
- 存在冲突
- push 需要额外认证且当前环境无法完成
- 当前变更明显混杂多个无关需求，无法生成单一提交说明