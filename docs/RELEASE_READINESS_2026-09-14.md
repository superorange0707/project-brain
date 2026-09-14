# Project Brain 发布就绪复验 — 2026-09-14

本文件记录发布授权前的验收快照；后续发布状态以 GitHub Release 和对应 CI 为准。

结论：当前固定源码通过本机验收，可以进入 v1.0.26 发布流程；尚未满足正式发布的全部平台和真实模型门槛。不能据此声称所有规模、设备和模型的性能问题均已解决。

GitHub 当前最新正式版本为 [v1.0.25](https://github.com/superorange0707/project-brain/releases/tag/v1.0.25)。工作区版本号仍是 1.0.25，本次测试包也使用此版本号，仅用于隔离验证，不能当作新的正式发行包上传。未提交、推送、打标签或发布；仓库原有 `dist` 未修改。

## 最终源码与本机验收

在其他修改结束后记录了 104 个源码、测试和配套文件的 SHA-256，并在完整测试结束后复核：没有文件变化。本报告是随后新增的验收记录。

| 检查 | 结果 |
| --- | --- |
| UI 定向测试 | 48 项通过，17.215 秒 |
| 自动刷新定向测试 | 20 项通过，0.791 秒 |
| 全量 unittest，使用 CI 的 ResourceWarning 配置 | 843 项：838 通过、5 跳过、0 失败，321.017 秒 |
| 源码编译与 diff 空白检查 | 通过 |
| wheel 与 sdist 构建、隔离 wheel 安装 | 通过；wheel 内全部 36 个 Brain 文件与记录源码逐字节匹配 |
| macOS arm64 原生程序 | PyInstaller 构建、版本和帮助命令、发布夹具运行通过 |
| 原生程序与源码行为比较 | 路由、13 个实体身份、7 条证据、流程及 LF/CRLF 身份完全一致 |
| 最终 wheel 的浏览器布局 | 8 个页面 × 320/390/1440px × 明暗主题，共 48 组，无页面或表单行横向溢出 |
| 大结果展示 | 展示 104,960 字符的实际源码检索结果，三种宽度、两种主题均通过布局检查 |
| 最后修改的纯文本引导 | 普通文字分类保持检索禁用；生成 JSON 后可预览，复制实际 CTX-001，检索按钮启用 |
| 浏览器控制台 | 本次检查未捕获 warning 或 error |

5 项跳过均要求原生 Windows：目录句柄、根目录替换、进程树、跨进程锁和可执行路径兼容。不能记作通过。当前本机使用 Python 3.14.4；其他 Python 版本和操作系统必须由对应 CI 执行。

本次原生验证是 CI 所定义的 Core 程序构建及确定性夹具，不等于包含 Zoekt、结构后端、安装器和模型的完整发行归档验收。测试服务已关闭，临时浏览器页已关闭，视口覆盖已复原。

## 性能证据及适用范围

最终 wheel 使用浏览器原生 PerformanceObserver 记录加载。每种宽度丢弃一次预热后测五次，数据加载后再观察至少一秒；本机未限速，期间另有测试/构建活动。这些数字是实验室样本，不是 Lighthouse 分数、现场 INP 或真实手机结果。

| 指标 | 桌面 1440px | 窄屏 390px |
| --- | ---: | ---: |
| FCP 中位数 | 48 ms | 48 ms |
| LCP 中位数 | 48 ms | 48 ms |
| 项目数据就绪中位数 | 81.2 ms | 88.7 ms |
| 项目数据就绪最大值 | 96.4 ms | 203.6 ms |
| 最大 CLS | 0.07812 | 0.00657 |
| 超过 50 ms 的长任务 | 0 | 0 |
| 页面外部资源请求 | 0 | 0 |

本次加载已有一个 ticket 的工作区。上轮空工作区的 CLS 更低，两组不同页面状态不能直接解释为版本回退。

完整测试中的同条件 Java 解析实验保持输出一致，重复屏蔽扫描的中位耗时从 280.534 ms 降到 144.743 ms，约减少 48%。这仅是该解析夹具的结果，不能推广为整个产品提速 48%。

同次合成规模检查增长至 100 个微型仓库时，检索耗时 13.826 ms、物理操作 2 次；当轮新增 50 个仓库的刷新为 2,204.944 ms；随后一个仓库变化的增量刷新为 1,009.464 ms。它验证有界工作和增量行为，不代表 100 个大型企业仓库的耗时。

上一轮对 63 个 Project Brain 实际源码文件、约 3.38 MB 的隔离副本执行了 60 次明确符号检索，预期定义文件全部出现；六类查询中位数为 321–558 ms，P95 为 373–1,002 ms。首次刷新约 14.6 秒，无变更刷新中位数约 3.49 秒。当前检索核心代码与该轮一致；最后变动集中在 UI、自动刷新和状态展示。这不是盲测检索质量，也没有真实模型、Git 历史或企业规模负载。

已验证的故障恢复还包括：刷新失败退避与重试、轮询临时 503 后继续同一任务、页面重载恢复后台任务、检索期间草稿保留和防重复提交。详见 [稳定性验证记录](QA_2026-09-14.md)。

## 尚未完成的发布门槛

1. 将最终审阅内容整理为新版本 1.0.26，更新 `pyproject.toml`、`brain/__init__.py`、`CHANGELOG.md` 和 `RELEASE_NOTES.md`，从干净提交构建。不可覆盖已发布的 v1.0.25。
2. 新提交需要 Linux 和原生 Windows 的 Python 3.11–3.14 完整测试，以及五个平台的归档构建、安装器冒烟和确定性结果比较。
3. 必须重新执行 macOS、Windows 的真实 Semantic/Precision 模型验证。本轮修改了 `brain/core.py:simple_yaml_load`，现有 `verify_model_pack_reuse.py` 的兼容性指纹发生变化；不能复用 v1.0.25 的旧验证记录。此前发布的绿色 CI 只覆盖旧提交。
4. 所有发布依赖通过后，由现有 release workflow 生成校验和与构建来源证明；下载草稿资产验证成功后才正式公开。Homebrew 更新依赖正式发布成功及 tap 凭证可用；PyPI 保持现有 opt-in 策略。

流程依据：[发布流程](RELEASING.md)、[release.yml](../.github/workflows/release.yml) 与 [CI](../.github/workflows/ci.yml)。触发这些涉及提交、推送和正式发布的步骤，需要用户明确授权，依据仓库 `AGENTS.md` 的发布规则。

企业实际代码库、真实 M365 输出、目标机器上的模型延迟/内存和长期负载仍没有本次实测。正式发布说明应保留这一范围，不承诺“所有性能问题解决”。

## v1.0.26 发布说明草案

**Project Brain v1.0.26 — Reliable investigation continuation and source retrieval**

- Improve complete-reply JSON/YAML request handling, including surrounding explanation and fenced input. Offer a reviewable request from plain text when Copilot omits the structured request.
- Keep investigations usable beyond earlier round limits while preserving per-request resource budgets, pinned source identity and duplicate-request checks. Recover saved evidence and pending requests without resetting tickets.
- Reconnect to running UI jobs after temporary polling failures or page reloads. Preserve edits made during retrieval and prevent duplicate submission. Retry failed automatic refresh with backoff.
- Improve exact method-body delivery, explicit symbol/relationship retrieval, Python import-aware navigation and HTTP/configuration/event source navigation. Reuse bounded request-local work and retain explicit incomplete-result reporting.
- Fix narrow-screen layouts, long result/path wrapping, accessible form labels and readiness reporting for indexed non-Git snapshots.

After upgrading, regenerate the M365 Agent Kit and replace the existing Agent Builder instructions and project knowledge; no new Agent or ticket is required. Existing agents do not automatically receive local template changes.

Run Refresh Brain to build updated relationship projections for new investigations. Existing tickets retain their original generations. The regression fixture verifies rebuilding relationships without re-embedding unchanged Semantic cards; do not delete indexes, model packs or ticket history as an upgrade step.

Final publication requires the new version's complete native, model and artifact verification. Validation covers bounded public/synthetic fixtures and local Core browsing; it does not establish universal enterprise search quality or fixed model latency.

## 原始证据位置

最终本机日志、源码清单、wheel/sdist、原生程序、模型资格指纹和平台夹具结果：

`/private/tmp/project-brain-release-check-20260914-yol30w9x`

上一轮实际源码检索、浏览器故障注入及并发读测量：

`/private/tmp/project-brain-deep-20260914-egylppgl`

wheel SHA-256：`d786334897848a71812b546d737e4700bfd372cc8d0dd8932bdea5ff827197c2`。

本机原生程序 SHA-256：`4702258d3ded4d9863539b38b02457eefe1335d138bbf4c624fc964696850c42`。
