# myagents Git 提交规则

> 作者：Bryant Yang　最近更新：2026-07-26

仓库已经初始化 Git，本文立即生效。当前没有 commitlint/CI；只有用户明确要求时
才 commit、push、创建或修改 remote、hook 与 CI。

## 1. 格式

```text
type(scope): subject

可选 body：解释为什么，不复述代码做了什么。
```

允许的 `type`：

`feat`、`fix`、`perf`、`refactor`、`docs`、`test`、`build`、`ci`、
`chore`、`revert`。

建议 scope：

`tui`、`orchestrator`、`acp`、`adapter`、`host`、`tests`、`docs`、
`harness`。

subject 使用小写英文或简洁中文，结尾无标点，建议不超过 72 字符。

## 2. 操作约束

- `git add` 必须显式列文件，禁止 `git add .` / `git add -A`。
- 未经用户明确要求，不 commit、不 push、不创建远程。
- 禁止 `--no-verify`、`--no-gpg-sign` 和主分支 force push。
- 协议或安全边界的 breaking change 必须先有设计记录和迁移方案。

当前没有 commitlint/CI，格式依靠 review；未来是否接 hook 与远程必需检查由用户
另行决定。
