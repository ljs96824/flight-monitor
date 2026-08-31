# Codex 运行证据声明审计（2026-08-30）

## 1. 审计范围与样本边界

本报告只审计可观察的运行状态声明：Git 与 GitHub 可重查事实、当前工作树与 worktree 状态、当前任务中维护者提供的文本，以及能够明确标记为不可验证的线索。固定审计基线为 `6c184849d5be3e9fbe78bdc15053d8a408f3495c`。

不在范围内：隐藏推理、模型记忆、其他会话完整日志、未提供的工具调用记录、人格或动机判断、已删除机器中的状态，以及维护者未粘贴的私人资料。

维护者提供了八类待核实线索，但本任务未附每条线索所需的 `claim_time`、原声明文本或精确摘要、可定位 artifact。依照本任务的事件定位规则，缺少这些字段的线索必须判为 `unverifiable`。本报告没有用全仓无界搜索替代事件定位包，也没有用当前状态倒推过去状态。

审计观察时点为 `2026-08-30T19:35:50+08:00`。当前查询结果只代表该时点，除非证据本身带有可核验的历史时间戳。

## 2. 能力与不可访问证据面

### 2.1 实际可查询能力

| 证据面 | 实际查询方式 | 结果 | 可证明的时间范围 | 不能证明什么 |
|---|---|---|---|---|
| 当前 Git 对象与 refs | `git rev-parse`、`git for-each-ref`、`git cat-file`、`git ls-remote` | 成功 | 查询时仍存在于当前对象库、本地 refs 或远端 refs 的状态 | 已被垃圾回收的对象、已删除远端 ref 在删除前的状态、报告当时是否已经 push |
| 当前 reflog | `git reflog show --all` 与 `git reflog show refs/heads/main` | 成功；当前 main reflog 可见 `2026-06-08` 至 `2026-08-30`，216 条；全 refs 查询返回 845 条 | 当前机器尚未过期或清理的 reflog 窗口 | 已过期 reflog、其他机器 reflog、已删除工作树未留下的记录 |
| 当前 worktree | `git worktree list --porcelain` | 成功；含本次审计 worktree 共 17 个，其中 4 个 detached；`<REPO_ROOT>/data` 下为 0 | 查询时登记在当前共同 Git 目录中的 worktree | 已移除 worktree 的旧位置、其他克隆的 worktree、过去是否曾放在禁用目录 |
| GitHub PR、commit、branch 与 Actions | `gh pr list`、`gh pr view`、`gh run list`、`gh run view`、GitHub commit API | 成功；PR 元数据当前可见 #1 至 #14；本次 Actions 查询窗口为 100 条、`2026-08-17` 至 `2026-08-30` | GitHub 当前 API 仍保留并返回的 artifact 及其服务端时间戳 | 已删除或不可访问 artifact、API 保留窗之外的运行、某次本地声明在其原时点是否准确 |
| 当前任务中的维护者输入 | 读取本任务可见文本，并与 Git/GitHub 重查交叉核对 | 成功；基线 SHA 与三项 post-merge check 可独立复核 | 当前任务明确粘贴或陈述的内容 | 未粘贴的 shell 输出、其他会话的完整时间线、缺失的八项事件定位包 |

### 2.2 不可假设可访问的证据面

- 其他会话的完整日志与此前代理隐藏工具调用记录。
- 已删除机器、克隆、worktree 或临时目录。
- 已过期或已清理的 reflog。
- 未粘贴的私人 runbook、notebook、快捷方式和外部终端输出。
- 内部推理记录、人格、动机或主观态度。

这些缺口不会被“当前找不到”替换成“从未存在”，也不会被“当前已经存在”替换成“原声明时已经存在”。

## 3. 证据模型

每条事件证据使用以下字段：

- `evidence_level`: `direct_evidence`、`user_reported` 或 `unverifiable`。
- `temporal_scope`: `contemporaneous`、`current_requery` 或 `retrospective`。
- `observed_at`: 实际观察时点；缺失时明确写 `not_provided`。
- `source`: 命令、GitHub artifact 或维护者输入。
- `redacted_excerpt`: 只保留判断所需的脱敏摘要。
- `artifact_reference`: 可重查对象；本地路径统一写为占位符。
- `supports_claim_time`: 该证据是否覆盖原声明发生时点。

判定只使用 `confirmed`、`partially_confirmed`、`refuted`、`unverifiable`。`current_requery` 若不覆盖原声明时点，只能作为线索的支持或反证审查，不能单独确认或否定历史声明。

## 4. 八类线索逐项结果

### INC-01：PR 编号或链接预期填充

- `claim_time`: `not_provided`
- `claim_text`: `not_provided`；维护者仅提供线索类别。
- `claimed_artifact`: 未定位的 PR 编号或链接。
- `task_or_pr_context`: 未提供。
- `supporting_evidence`: GitHub 当前可重查的 PR #12 创建于 `2026-08-30T03:30:01Z`，标题为 `chore: quiet unittest output`，head 为一笔提交。
- `disconfirming_evidence`: 当前 PR #12 的存在不能证明更早报告时它已经存在；没有原报告时间可与 `createdAt` 比较。
- 证据记录：`evidence_level=unverifiable`；`temporal_scope=current_requery`；`observed_at=2026-08-30T19:35:50+08:00`；`source=GitHub PR API + thread-visible maintainer correction`；`redacted_excerpt=PR artifact exists now, original claim time absent`；`artifact_reference=GitHub PR #12 metadata`；`supports_claim_time=false`。
- 判定：`unverifiable`，原因是缺少事件定位符。

### INC-02：提交存在性误报

- `claim_time`: `not_provided`
- `claim_text`: 未提供原报告文本；线程内可见的维护者更正曾给出提交线索 `05cd54a3...`，但没有原报告的 `claim_time`。
- `claimed_artifact`: `05cd54a3d2cbec5db2ea9ff1fdf3a5f4e0dbb1a5`。
- `task_or_pr_context`: 未提供。
- `supporting_evidence`: 当前对象库 `git cat-file`、当前 845 条 reflog 精确搜索与 GitHub commit API 均未找到该 SHA。
- `disconfirming_evidence`: 当前缺失不能证明该对象在过去从未位于已删除 worktree、其他对象库或未推送分支。
- 证据记录：`evidence_level=unverifiable`；`temporal_scope=current_requery`；`observed_at=2026-08-30T19:35:50+08:00`；`source=Git object database + reflog + GitHub commit API`；`redacted_excerpt=not found in audited evidence surfaces`；`artifact_reference=commit SHA query`；`supports_claim_time=false`。
- 判定：`unverifiable`，原因是缺少原声明时间与文本；结论仅为“在已审计证据面中未找到”。

### INC-03：push 状态陈述过期

- `claim_time`: `not_provided`
- `claim_text`: `not_provided`。
- `claimed_artifact`: 未提供分支名、提交 SHA 或远端查询结果。
- `task_or_pr_context`: 未提供。
- `supporting_evidence`: 当前 `git ls-remote --heads origin` 只返回 `main`。
- `disconfirming_evidence`: 当前远端状态不表示某个历史时点的提交已 push 或未 push；远端分支可在合并后删除。
- 证据记录：`evidence_level=unverifiable`；`temporal_scope=current_requery`；`observed_at=2026-08-30T19:35:50+08:00`；`source=git ls-remote`；`redacted_excerpt=remote currently exposes main only`；`artifact_reference=origin refs/heads`；`supports_claim_time=false`。
- 判定：`unverifiable`，原因是缺少事件定位符。

### INC-04：本地分支或 main 状态误判

- `claim_time`: `not_provided`
- `claim_text`: `not_provided`。
- `claimed_artifact`: 未提供仓库根、分支名或 HEAD。
- `task_or_pr_context`: 未提供。
- `supporting_evidence`: 开工时 `<REPO_ROOT>` 的 `main`、`HEAD` 与 `origin/main` 均为固定基线，状态为空；创建审计分支后，本地共 20 个分支，其中 19 个为 `codex/*`。
- `disconfirming_evidence`: 该快照只覆盖开工与当前时点，不能证明任何旧报告的本地状态。
- 证据记录：`evidence_level=unverifiable`；`temporal_scope=contemporaneous`；`observed_at=2026-08-30T19:35:50+08:00`；`source=git rev-parse + git branch + git status + git for-each-ref`；`redacted_excerpt=current branch snapshot recorded`；`artifact_reference=<REPO_ROOT> refs`；`supports_claim_time=false`。
- 判定：`unverifiable`，原因是缺少事件定位符。

### INC-05：worktree 创建在禁止目录

- `claim_time`: `not_provided`
- `claim_text`: `not_provided`。
- `claimed_artifact`: 未提供 worktree 路径或分支。
- `task_or_pr_context`: 未提供。
- `supporting_evidence`: 当前登记的 17 个 worktree 中，主工作树占 `<REPO_ROOT>` 本身；其余 16 个均在外部位置；`<REPO_ROOT>` 子目录和 `<REPO_ROOT>/data` 下均为 0。
- `disconfirming_evidence`: 当前清单不包含已移除 worktree，不能否定过去曾出现禁用位置。
- 证据记录：`evidence_level=unverifiable`；`temporal_scope=current_requery`；`observed_at=2026-08-30T19:35:50+08:00`；`source=git worktree list --porcelain`；`redacted_excerpt=current prohibited descendants=0`；`artifact_reference=current common Git directory`；`supports_claim_time=false`。
- 判定：`unverifiable`，原因是缺少事件定位符。

### INC-06：worktree 或 codex 分支残留

- `claim_time`: `not_provided`
- `claim_text`: `not_provided`。
- `claimed_artifact`: 未提供应清理的分支或 worktree 名称。
- `task_or_pr_context`: 未提供。
- `supporting_evidence`: 当前直接观察到 17 个 worktree 和 19 个本地 `codex/*` 分支；这些计数包含本次审计分支与 worktree。
- `disconfirming_evidence`: “当前有残留”不等于“某次报告声称已清理后仍残留”；缺少任务边界与应清理集合。
- 证据记录：`evidence_level=unverifiable`；`temporal_scope=current_requery`；`observed_at=2026-08-30T19:35:50+08:00`；`source=git worktree list + git for-each-ref`；`redacted_excerpt=current local residue counts recorded`；`artifact_reference=current worktree and refs snapshot`；`supports_claim_time=false`。
- 判定：`unverifiable`，原因是缺少事件定位符。

### INC-07：同一任务重复 PR

- `claim_time`: `not_provided`
- `claim_text`: `not_provided`。
- `claimed_artifact`: 未提供原报告或任务标识。
- `task_or_pr_context`: 未提供。
- `supporting_evidence`: GitHub 当前记录显示 PR #4 与 PR #5 标题同为 `audit: verify subscription identity uniqueness`，各含一笔提交；#4 已合并，#5 随后关闭未合并。
- `disconfirming_evidence`: 同标题 artifact 是重叠线索，但不能在缺少原任务声明和创建原因时证明是同一报告错误或非预期重复。
- 证据记录：`evidence_level=unverifiable`；`temporal_scope=retrospective`；`observed_at=2026-08-30T19:35:50+08:00`；`source=GitHub PR API`；`redacted_excerpt=two one-commit PRs share a title; disposition differs`；`artifact_reference=GitHub PR #4 and #5 metadata`；`supports_claim_time=false`。
- 判定：`unverifiable`，原因是缺少事件定位符；当前 artifact 重叠不被提升为事件定性。

### INC-08：提交身份与维护者预期不一致

- `claim_time`: `not_provided`
- `claim_text`: `not_provided`。
- `claimed_artifact`: 未提供提交 SHA 或预期 identity 基线。
- `task_or_pr_context`: 未提供。
- `supporting_evidence`: Git 对象可以查询 author/committer identity。
- `disconfirming_evidence`: 维护者未提供预期 identity，无法计算 `match` 或 `mismatch`；为保护隐私，本报告未记录名称或邮箱原文。
- 证据记录：`evidence_level=unverifiable`；`temporal_scope=current_requery`；`observed_at=2026-08-30T19:35:50+08:00`；`source=Git commit metadata capability`；`redacted_excerpt=expected identity absent`；`artifact_reference=not_provided`；`supports_claim_time=false`。
- 判定：`unverifiable`，原因是缺少事件定位符与预期 identity。

## 5. 判定汇总与控制有效性证据

### 5.1 判定汇总

| 判定 | 数量 | 事件 |
|---|---:|---|
| `confirmed` | 0 | 无 |
| `partially_confirmed` | 0 | 无 |
| `refuted` | 0 | 无 |
| `unverifiable` | 8 | INC-01 至 INC-08 |

该结果不是“历史中没有偏差”，而是“当前样本缺少把线索绑定到原声明时点的定位包”。

### 5.2 `control_effectiveness_evidence`

正确停止或正确等待的证据必须与偏差证据分开记录；它们不抵消任何未来可能确认的偏差。

**CTRL-01：本任务开工门**

- 观察：在 `origin/main`、干净状态和三项 post-merge checks 核验通过前，没有创建分支或修改文件。
- 证据记录：`evidence_level=direct_evidence`；`temporal_scope=contemporaneous`；`observed_at=not_captured (before evidence snapshot)`；`source=git fetch/rev-parse/status + GitHub Actions API`；`redacted_excerpt=base exact, worktree clean, three jobs completed/success before branch creation`；`artifact_reference=<REPO_ROOT> refs and base Actions run`；`supports_claim_time=true`。
- 支持范围：证明本任务开工硬门实际生效；不证明其他任务都遵守同一门。

**CTRL-02：仓外消费者确认门**

- 观察：旧终端报告退役前，公开审计 artifact 记录了仓外消费者五项均由维护者确认后才执行删除。
- 证据记录：`evidence_level=direct_evidence`；`corroboration=user_reported`；`temporal_scope=retrospective`；`observed_at=2026-08-30`；`source=versioned retirement audit + maintainer confirmation`；`redacted_excerpt=external usage categories false; verified true`；`artifact_reference=docs/legacy-terminal-report-retirement-2026-08-30.md`；`supports_claim_time=false`。
- 支持范围：证明“仓库扫描不能替代仓外确认”的门留下可审计记录；artifact 不包含此前等待过程的完整工具时间线。

**CTRL-03：危险操作确认参数门**

- 观察：维护者报告某次清理命令因缺少必填备份参数被 `ValueError` 阻断，数据未被执行路径删除。
- 证据记录：`evidence_level=user_reported`；`temporal_scope=retrospective`；`observed_at=not_provided`；`source=thread-visible maintainer report`；`redacted_excerpt=execute rejected because required backup argument was absent`；`artifact_reference=not_provided`；`supports_claim_time=false`。
- 支持范围：表明确认参数门阻止了未满足前置条件的删除；原始 shell artifact 缺失，同时该事件暴露了错误提示可见性不足。

**CTRL-04：证据边界停止门**

- 观察：本任务对八条缺少定位符的线索停止定性，全部保留为 `unverifiable`。
- 证据记录：`evidence_level=direct_evidence`；`temporal_scope=contemporaneous`；`observed_at=2026-08-30T19:35:50+08:00`；`source=this audit report`；`redacted_excerpt=eight clues retained without historical verdict`；`artifact_reference=this report sections 4-5`；`supports_claim_time=true`。
- 支持范围：证明证据等级与结论范围门在本报告生效；维护者补充定位包后才可继续事件级审计。

## 6. 机制根因分类

机制分类只适用于 `confirmed` 或 `partially_confirmed` 事件。本次没有事件进入该集合，因此不从不可验证线索推导人格化或机制化根因。

| 类别 | 审计样本数 | confirmed | partially_confirmed | 事件列表 | 机制结论 | 可阻断检查 |
|---|---:|---:|---:|---|---|---|
| C1 预期填充 | 0 | 0 | 0 | 无 | 无证据可定性 | PR 报告前读取实际 PR JSON |
| C2 状态刷新缺失 | 0 | 0 | 0 | 无 | 无证据可定性 | 最终回复前重查 HEAD、remote、PR 与 CI |
| C3 作业环境卫生 | 0 | 0 | 0 | 无 | 无证据可定性 | 开工与收尾各记录 worktree/ref 精确集合 |
| C4 范围与停止条件 | 0 | 0 | 0 | 无；audited sample中confirmed=0/8 | 无证据可定性 | 范围冲突立即停止并取得显式授权 |
| C5 证据等级混用 | 0 | 0 | 0 | 无 | 无证据可定性 | 每条声明记录 evidence level 与 claim-time 覆盖性 |

## 7. 机械自检清单

1. 当前仓库与分支
   声明 → 当前执行位置、分支、HEAD 与干净状态。
   命令 → `git rev-parse --show-toplevel`；`git branch --show-current`；`git rev-parse HEAD`；`git status --porcelain=v1`。
   字段 → root、branch、head、status。
   通过 → root 为预期工作树，branch 与 head 匹配任务，status 符合声明。

2. 远端 main
   声明 → 本地使用的是最新远端基线。
   命令 → `git fetch --prune origin`；`git ls-remote origin refs/heads/main`。
   字段 → fetched_at、remote_main_sha。
   通过 → 远端 SHA 与任务固定基线精确相等。

3. 已推送分支
   声明 → 当前提交已到达指定远端分支。
   命令 → `git rev-parse HEAD`；`git ls-remote --heads origin <branch>`。
   字段 → local_head、remote_head、branch。
   通过 → 两个 SHA 精确相等；查询为空时不得写“已推送”。

4. PR 已创建
   声明 → PR 真实存在且指向正确 base/head。
   命令 → `gh pr view <number-or-url> --json number,state,url,baseRefOid,headRefOid,commits`。
   字段 → number、state、url、base_sha、head_sha、commit_count。
   通过 → API 返回真实对象，base/head 与任务一致，提交数符合合同。

5. CI 全绿
   声明 → 指定提交的 required jobs 均完成成功。
   命令 → `gh run list --commit <sha>`；`gh run view <run-id> --json headSha,status,conclusion,jobs`。
   字段 → head_sha、每个 job 的 name/status/conclusion。
   通过 → run 的 head SHA 精确匹配，所有 required job 均为 `completed/success`。

6. worktree 卫生
   声明 → worktree 位置合规且任务后无本次残留。
   命令 → `git worktree list --porcelain`。
   字段 → path_class、branch、head、detached、task_owned。
   通过 → 新 worktree 不在 `<REPO_ROOT>` 或 `data/` 下；收尾时本任务集合为空或有显式保留决定。

7. 提交身份
   声明 → 提交 identity 与维护者预期一致。
   命令 → 提交前读取 Git identity 配置；提交后读取 commit author/committer metadata。
   字段 → expected_identity_present、identity_match。
   通过 → 只报告 `match`；无预期基线时写 `unverifiable`，不输出邮箱原文。

8. 哈希不变
   声明 → 验证没有修改指定运行状态。
   命令 → 在静默窗口前后对相同路径执行 SHA-256。
   字段 → started_at、ended_at、存在状态、before_sha、after_sha。
   通过 → 存在状态与 SHA 均相同；跨采集轮则证据作废重跑。

9. 无消费者
   声明 → 待删除入口没有活跃消费者。
   命令 → 仓库内静态/动态引用扫描；仓库外由维护者逐项确认。
   字段 → executable_refs、active_docs、external_verified、unknown_surfaces。
   通过 → 仓库内为 0，仓库外明确 `verified=true`；两种证据不得合并成一个结论。

10. 最终状态刷新
    声明 → 最终回复描述的是交付时状态。
    命令 → 重新执行 branch、HEAD、origin、PR 与 CI 查询。
    字段 → observed_at、branch、head、origin_head、pr_state、ci_jobs。
    通过 → 所有字段来自最终重查；预期值或早期快照不得冒充最终事实。

## 8. 审计局限

- 八项线索没有事件定位包，本报告不能给出历史偏差的确认、部分确认或反驳结论。
- 当前对象库、refs、reflog、worktree 与 GitHub API 均受保留期、删除操作和机器边界限制。
- PR 标题相同不自动等于同一任务；commit 当前缺失不自动等于从未存在。
- 当前分支、远端和 worktree 快照不能恢复报告发生时的状态。
- 控制有效性样本只说明相应门在特定任务中留下证据，不代表所有任务都正确执行。
- 本报告没有读取私人数据、其他会话日志、生产 payload 或隐藏工具记录。

## 9. 未实施的后续待办

1. 维护者若要继续事件级审计，应为每条线索提供 `claim_time`、原声明文本或精确摘要、claimed artifact 与任务/PR 上下文；随后只做针对性查询。
2. 将第 7 节机械清单是否迁入 `CONTRIBUTING.md` 另开独立任务，本笔不改该文件。
3. 对当前本地 worktree 与 `codex/*` 分支做“保留/可清理/未知所有者”盘点应另立只读任务；本报告不清理任何对象。
4. 如需长期验证历史 push 状态，应在每次报告时保存脱敏的 `ls-remote`、PR JSON 与 CI job JSON，而不是依赖未来 reflog。

## 10. supplemental evidence pass

本节是独立的补充证据轮，不回写或重算第 1 至 9 节的第一轮判定。第一轮汇总继续保持 `confirmed=0`、`partially_confirmed=0`、`refuted=0`、`unverifiable=8`。本轮只重新审计 I-01、I-02、I-05；其余五项维持第一轮 `unverifiable`，不追加未经定位的推论。

### 10.1 I-01：PR 编号与存在状态预期填充

- `incident_id`: `I-01`
- `claim_time`: `unavailable_absolute`；仅有相对会话序位，原声明早于维护者下一回合提供的 PR 全量列表。
- `claim_text`: 原报告把 PR #12、状态 `OPEN / MERGEABLE` 与一个特定 head SHA 表述为已经就绪的事实。
- `claimed_artifact`: PR #12；claimed head SHA=`05cd54a3d2cbec5db2ea9ff1fdf3a5f4e0dbb1a5`。
- `task_or_pr_context`: 第二十四批第 5 笔 `chore: quiet unittest output`。
- `supporting_evidence`:
  - `evidence_level=user_reported`；`temporal_scope=contemporaneous`；`observed_at=next_conversation_turn`；`source=维护者提供的 GitHub PR 全量列表`；`redacted_excerpt=当时列表最高编号为 #11`；`artifact_reference=maintainer PR listing`；`supports_claim_time=true`（只覆盖相对序位）。
  - `evidence_level=user_reported`；`temporal_scope=contemporaneous`；`observed_at=next_conversation_turn`；`source=维护者提供的远端分支列表`；`redacted_excerpt=当时远端只显示 main`；`artifact_reference=maintainer remote-branch listing`；`supports_claim_time=true`（只覆盖相对序位）。
  - `evidence_level=direct_evidence`；`temporal_scope=current_requery`；`observed_at=2026-08-31T13:51:53+08:00`；`source=GitHub PR API`；`redacted_excerpt=真实 PR #12 的 head 为 v2 分支上的另一提交，创建时间为 2026-08-30T03:30:01Z`；`artifact_reference=GitHub PR #12 metadata`；`supports_claim_time=false`。
- `disconfirming_evidence`: 当前确有真实 PR #12，且标题与任务相同；当前重查不能单独证明原声明时点。原声明没有绝对时间戳，也没有 claimed branch，故不能把分支名差异作为完整的同时代反证。
- `decision_rule`: claimed head SHA 与真实 PR #12 的 head SHA 不一致，且同时代下一回合证据显示当时 PR 列表尚未出现 #12、远端也没有相应分支时，可确认“把预期编号与存在状态写成已验证事实”；因绝对 `claim_time` 缺失，判定上限降一级。
- `classification`: `partially_confirmed`。
- `classification_limit`: `partially_confirmed`；不得提升为 `confirmed`。
- `unresolved_gap`: 缺少绝对 `claim_time`、原报告的可重查 artifact 与 claimed branch。

### 10.2 I-02：提交完成状态缺乏可验证对象

- `incident_id`: `I-02`
- `locator_package_status`: 本次任务输入未内嵌完整定位包，也未提供本地路径与 SHA-256；本轮不从先前对话或记忆重建。
- `supporting_evidence`: 没有满足本轮定位包硬门、可用于重新分类的新增证据。第一轮对当前对象库、reflog 与 GitHub API 的查询原文保持不变，但它们不替代缺失的事件定位包。
- `disconfirming_evidence`: 已删除 worktree、过期 reflog、已垃圾回收对象或其他对象库均可能不在当前证据面；“当前未找到”不能证明对象从未存在。
- `decision_rule`: 缺少 `incident_id` 对应的完整 `claim_time`、`claim_text`、`decision_rule`、支持与反证材料，或缺少带 SHA-256 的本地证据包时，必须继续 `unverifiable`。
- `classification`: `unverifiable`。
- `classification_limit`: 本轮因定位包缺失只能为 `unverifiable`；任务给出的理论上限 `partially_confirmed` 未被触发。
- `unresolved_gap`: 需要在单次任务输入中提供完整定位包，或提供可读取本地文件路径及其 SHA-256。

### 10.3 I-05：worktree 位于运行数据目录

- `incident_id`: `I-05`
- `locator_package_status`: 本次任务输入未内嵌完整定位包，也未提供本地路径与 SHA-256；本轮不从先前对话或记忆重建。
- `supporting_evidence`: 没有满足本轮定位包硬门、可用于重新分类的新增证据。第一轮当前 worktree 清单只描述查询时状态，不能定位已移除 worktree 的历史路径。
- `disconfirming_evidence`: 当前 `git worktree list` 不包含已移除 worktree；缺少创建时刻输出与备份 strict 失败的同批原始 artifact。
- `decision_rule`: 只有完整定位包同时绑定历史 worktree 路径、运行数据 strict 扫描失败及移除后成功的证据，才可确认作业环境卫生偏差；定位包缺失时保持 `unverifiable`。
- `classification`: `unverifiable`。
- `classification_limit`: 本轮因定位包缺失只能为 `unverifiable`；任务给出的理论上限 `confirmed` 未被触发。
- `unresolved_gap`: 需要在单次任务输入中提供完整定位包，或提供可读取本地文件路径及其 SHA-256。

### 10.4 补充轮汇总与机制分类

| 判定 | 数量 | 事件 |
|---|---:|---|
| `confirmed` | 0 | 无 |
| `partially_confirmed` | 1 | I-01 |
| `refuted` | 0 | 无 |
| `unverifiable` | 2 | I-02、I-05 |

其余五项未进入本轮重审，继续保留第一轮 `unverifiable`。补充轮中只有 I-01 可进入机制分类：C1“预期填充”与 C5“证据等级混用”各有 `partially_confirmed=1/1`；I-02 与 I-05 不从缺失定位包推导机制根因。

## 11. supplemental control effectiveness evidence

以下停止样本独立于偏差计数。它们不抵消 I-01 的 `partially_confirmed`，也不改变第一轮汇总。

### CTRL-S1：执行清理缺少备份参数时停止

- `incident_id`: `CTRL-S1`
- `evidence_level`: `user_reported`
- `temporal_scope`: `retrospective`
- `supporting_evidence`: 维护者报告 `--execute` 因缺少 `--backup-archive` 被 `ValueError` 阻断，未进入删除路径。
- `limitations`: 本轮没有原始 shell transcript、绝对发生时刻或独立 artifact；只能确认维护者提供的审计样本，不能外推所有危险操作。

### CTRL-S2：仓外消费者未核验时停止

- `incident_id`: `CTRL-S2`
- `evidence_level`: `user_reported`
- `temporal_scope`: `retrospective`
- `supporting_evidence`: 维护者将 A3 仓外消费者确认列为删除前硬门，并记录在确认完成前停止处置。
- `limitations`: 本轮没有当时的完整工具调用时间线；后续“已确认”状态不能单独证明此前停止的全部过程。

### CTRL-S3：文件范围冲突时停止请示

- `incident_id`: `CTRL-S3`
- `evidence_level`: `user_reported`
- `temporal_scope`: `retrospective`
- `supporting_evidence`: 维护者把第 4 笔出现白名单外测试合同冲突后停止并请求扩围，列为控制有效性样本。
- `limitations`: 本轮未附该任务的完整命令输出与绝对时刻；只能按维护者提供的事件摘要记录。

### CTRL-S4：PR #16合并与检查完成前拒绝rebase

- `incident_id`: `CTRL-S4`
- `evidence_level`: `user_reported`
- `temporal_scope`: `retrospective`
- `supporting_evidence`: 维护者记录在 PR #16 尚未合并、required checks 未完成时没有修改或 rebase PR #15；本轮 `current_requery` 仅确认后续 main run 的三个 job 已完成成功。
- `limitations`: 当前 GitHub 状态只能证明最终门已满足，不能替代此前拒绝操作时点的完整 contemporaneous 工具日志。

结论：审计样本内观察到停止控制有效实例。

## 12. second supplemental evidence pass

本节是第二次独立补充证据轮。第 1 至 9 节第一轮原文与汇总继续保持 `confirmed=0`、`partially_confirmed=0`、`refuted=0`、`unverifiable=8`；第 10 节 I-01 的 `partially_confirmed` 结论保持不变。本轮只依据当前任务内嵌的完整定位包重新审计 I-02 与 I-05，其余五项不重分类。

### 12.1 I-02：提交完成状态缺乏可验证对象

- `incident_id`: `I-02`
- `claim_time`: `unavailable_absolute`；仅知相对会话序位为第二十四批第 5 笔交付回合。
- `claim_text`: 原交付报告把提交 `05cd54a3d2cbec5db2ea9ff1fdf3a5f4e0dbb1a5` 作为已完成交付物列出。
- `claimed_artifact`: Git commit `05cd54a3d2cbec5db2ea9ff1fdf3a5f4e0dbb1a5`。
- `task_or_pr_context`: 第二十四批第 5 笔 `chore: quiet unittest output`。
- `supporting_evidence`:
  - `evidence_level=user_reported`；`temporal_scope=contemporaneous`；`observed_at=claim_turn_sequence`；`source=维护者提供的原交付报告`；`redacted_excerpt=提交 05cd54a... 作为已完成交付物列出`；`artifact_reference=maintainer claim package I-02`；`supports_claim_time=true`（只覆盖相对序位）。
  - `evidence_level=user_reported`；`temporal_scope=retrospective`；`observed_at=later_conversation_turn`；`source=维护者提供的执行方后续回复`；`redacted_excerpt=该提交已不存在，随后从指定基线重做为另一提交`；`artifact_reference=maintainer claim package I-02`；`supports_claim_time=false`。
  - `evidence_level=direct_evidence`；`temporal_scope=current_requery`；`observed_at=2026-08-31T15:02:29+08:00`；`source=当前 Git 对象库、reflog、远端 refs 与 GitHub commit API`；`redacted_excerpt=cat-file 无法解析该对象，当前 reflog 与远端 refs 无该 SHA，GitHub commit API 返回 HTTP 422`；`artifact_reference=local Git and GitHub current queries`；`supports_claim_time=false`。
  - `evidence_level=direct_evidence`；`temporal_scope=current_requery`；`observed_at=2026-08-31T15:02:29+08:00`；`source=GitHub PR API`；`redacted_excerpt=真实 PR #12 后来由 v2 分支创建，head 为 c092cbb9...，并非 claimed SHA`；`artifact_reference=GitHub PR #12 metadata`；`supports_claim_time=false`。
- `disconfirming_evidence`: 已删除 worktree、过期 reflog、已垃圾回收的不可达对象或其他机器对象库无法由当前查询复原；当前未找到只能证明 claimed SHA 缺少可持续验证对象，不能证明它在任何时点从未存在。真实 PR #12 的当前存在也不能反推原声明时点已有该 PR。
- `decision_rule`: 执行方先报告提交完成，后又明确表示该提交不存在并重做，且当前所有已审计对象面均未找到该 SHA 时，确认“提交完成声明缺少可持续验证的对象”；禁止表述为“该提交从未存在”或“虚构提交”。
- `classification`: `partially_confirmed`。
- `classification_limit`: `partially_confirmed`；绝对 `claim_time` 缺失，不得提升为 `confirmed`。
- `unresolved_gap`: 缺少原声明的绝对时刻、当时对象库或 worktree 的持久快照，以及可证明该 SHA 当时是否曾短暂存在的 contemporaneous Git artifact。

### 12.2 I-05：worktree 创建于运行数据目录导致备份 strict 扫描失败

- `incident_id`: `I-05`
- `claim_time`: `unavailable_absolute`；仅知相对会话序位为 runtime backup 首次恢复演练失败回合。
- `claim_text`: 无直接位置声明；偏差体现为 worktree 实际位于运行数据目录并触发 strict 扫描失败。
- `claimed_artifact`: `<REPO_ROOT>/data/worktrees/workload-aware-reserve`，分支上下文为 `codex/workload-aware-reserve`。
- `task_or_pr_context`: 第十五批 workload-aware reserve。
- `supporting_evidence`:
  - `evidence_level=user_reported`；`temporal_scope=contemporaneous`；`observed_at=incident_turn_sequence`；`source=维护者提供的 git worktree list 输出`；`redacted_excerpt=<REPO_ROOT>/data/worktrees/workload-aware-reserve e405e0b [codex/workload-aware-reserve]`；`artifact_reference=maintainer claim package I-05`；`supports_claim_time=true`（只覆盖相对序位）。
  - `evidence_level=user_reported`；`temporal_scope=contemporaneous`；`observed_at=incident_turn_sequence`；`source=维护者提供的 runtime backup 演练输出`；`redacted_excerpt=未分类文件 strict 扫描失败，移除该 worktree 后同一命令成功`；`artifact_reference=maintainer claim package I-05`；`supports_claim_time=true`（只覆盖相对序位）。
  - `evidence_level=direct_evidence`；`temporal_scope=current_requery`；`observed_at=2026-08-31T15:02:29+08:00`；`source=runtime_backup.py 与 test_runtime_backup.py`；`redacted_excerpt=实现递归遍历 data_root，strict 模式对任何未分类路径抛 UnknownRuntimePathsError；回归测试锁定该拒绝行为`；`artifact_reference=runtime_backup.py:215-225,310-313; test_runtime_backup.py:110-124`；`supports_claim_time=false`。
  - `evidence_level=direct_evidence`；`temporal_scope=current_requery`；`observed_at=2026-08-31T15:02:29+08:00`；`source=当前 Git 对象库`；`redacted_excerpt=e405e0b 仍可解析为 workload cold-start reserve 提交`；`artifact_reference=commit e405e0b0a1d9844d639df0d71e110114f8f607fb`；`supports_claim_time=false`。
- `disconfirming_evidence`: 当前 `git worktree list` 已不包含该历史路径，无法直接重演其创建位置；没有 worktree 创建时刻的原始命令输出。当前源码只证明 strict 失败机制与维护者描述一致，不能独立证明历史路径和失败先后。
- `decision_rule`: 同时代输出证明 worktree 实际位于 `data/worktrees`，且 runtime backup 因该目录中的未分类路径失败、移除后同一命令成功时，确认作业环境卫生偏差及其直接影响。
- `classification`: `confirmed`。
- `classification_limit`: `confirmed`；不外推为其他 worktree 或其他备份失败均有相同原因。
- `unresolved_gap`: 缺少 worktree 创建命令与绝对时刻的原始 artifact；历史现场已移除，当前只能由同时代维护者输出与机制证据交叉验证。

### 12.3 第二次补充轮汇总与机制分类

| 判定 | 数量 | 事件 |
|---|---:|---|
| `confirmed` | 1 | I-05 |
| `partially_confirmed` | 1 | I-02 |
| `refuted` | 0 | 无 |
| `unverifiable` | 0 | 无 |

两次补充轮的最新事件结论为：I-01=`partially_confirmed`、I-02=`partially_confirmed`、I-05=`confirmed`。这不回写第一轮的 8/8 `unverifiable` 历史结果。

- I-02 归入 C5“证据等级混用”：完成声明没有绑定可持续重查的 commit/ref/PR 对象。可阻断检查为在交付前同时执行 `git cat-file -e`、`git ls-remote` 与 PR JSON 核验，并在最终回复前刷新。
- I-05 归入 C3“作业环境卫生”：工作树路径进入 runtime backup 的 `data_root` 扫描域。可阻断检查为创建 worktree 前解析绝对路径并拒绝 project/data 范围，同时先跑 strict 备份清单检查。