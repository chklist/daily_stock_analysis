# GitHub Actions 主动选股

`主动选股与深度分析` 工作流复用 DSA 已内置的 AlphaSift 衍生选股引擎；不需要安装独立 AlphaSift、公开 DSA API 或常驻服务器。原自选股工作流保持独立。

## 流程与默认值

北京时间工作日 18:15 触发，GitHub 排队可能延迟。使用 XSHG 交易日历，非交易日、未收盘或日历不可用时不生成当天名单。

1. `balanced_alpha` 规则、因子及本地 scorecard 排序，开启高风险否决，最多 3 只。初筛不调用 LLM，也不启用独立行业/概念增强。
2. 全市场快照少于 1000 条、无来源或仅有旧缓存时失败；逐只校验日线必须到最近完整交易日，不接纳过期日线缓存。
3. 候选代码显式传给 `StockAnalysisPipeline`，沿用现有模型、Sub2API Responses 配置及 Anspire 新闻搜索。空候选不会回退到 `STOCK_LIST`。
4. DSA 评分至少 60、动作 buy/add（旧结果兼容买入/加仓/增持/强烈买入）、非低置信度且本轮新闻检索有结果，才进入观察名单。这是初始工程筛选规则，未经收益回测验证；分数不是胜率。
5. 一次汇总推送到既有 Telegram。没有合格股票会明确说明。模型失败、行情校验失败或发送失败会使任务失败并保留诊断，避免误报成功。

入选理由、筛选来源、行情日期和完整 DSA 复核结果保存在 `reports/proactive/`；Actions artifact 保留 30 天。脚本不自动下单。尚未添加跨次任务的候选去重或 T+N 自动评估；下载结果可供后续研究，不能据此宣称已验证选股收益。

## 配置与运行

工作流复用仓库 `LLM_CHANNELS`、`LITELLM_MODEL`、`LLM_PRIMARY_*` Variables/Secrets，以及 `ANSPIRE_API_KEYS`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` Secrets；与当前已验证的 primary 渠道一致。密钥不写进代码。

Actions → 主动选股与深度分析 → Run workflow：

- `strategy`：均衡多因子、质量价值或双低策略。
- `force_run`：仅用于非交易日/收盘前验证，不伪造当天行情日期。
- `screen_only`：只验证候选和行情，不调用模型，也不发送消息。
- `notify`：完整分析后是否推送；定时运行默认推送。

本地入口：

```bash
python scripts/run_proactive_screening.py --screen-only
python scripts/run_proactive_screening.py --strategy balanced_alpha --top 3 --min-score 60 --notify
python -m unittest discover -s scripts/tests -p test_proactive_screening.py -v
```

脚本参数不会改写 `.env`；沿用现有配置。若要修改默认数量或分数，请调整工作流参数，数量限制为 1–3。推送仅包含通过复核的详细报告，未通过者列出排除原因。

## 限制与回滚

### 资金流采集诊断

每日分析和主动选股工作流默认使用 `FUNDAMENTAL_STAGE_TIMEOUT_SECONDS=45`、
`FUNDAMENTAL_FETCH_TIMEOUT_SECONDS=15`，可通过同名 Actions Variables 覆盖。
它们分别表示聚合总预算和单个采集任务预算；本地/Docker 默认值仍为 8 秒。
A 股个股资金流先执行，避免财务接口耗尽预算；普通分析仅采集个股资金，
不等待可选板块资金流。独立资金流工具仍保留板块查询能力。

资金流调用明确传入股票代码和 sh/sz/bj 市场，按日期取最新主力净额（元），
5/10 日值仅在有足够有效历史记录时求和。不会用排名、占比或默认股票数据替代。
来源、耗时、错误类型写入 `[capital_flow]` 日志；主动选股 JSON 额外保留
`capital_flow_diagnostics`，包括实际数据日期。接口中断仍可能造成缺失，
修复不保证第三方接口可用，也不移除买入降级规则。

Actions → `资金流采集诊断` → Run workflow 可独立验证沪深样本，
不调用模型、不发送通知。该工作流先跑回归测试，再调用生产基本面聚合入口，
只有两个样本都取得最近完整交易日的资金净额才成功；记录保留 14 天。
本地运行：`python scripts/diagnose_capital_flow.py --stocks 601328,000001`。
若接口不可用，查看 artifact 中的 `capital-flow-check.json`，不要仅提高超时后认定恢复。

资金流改动可通过撤销对应修复提交回滚；原有密钥、通知设置和评分门槛均不需修改。

第三方实时快照不保证每行都携带交易日；本流程拒绝旧缓存并验证候选日线日期，但不等于独立审计全部实时字段。行业/概念数据未增强时，报告明确提示热点信息不完整。实时源和模型仍可能超时，45 分钟作业上限负责最终中止。

关闭此工作流即可停止新增选股推送，不影响原 `每日股票分析`。撤销引入本工作流和脚本的提交可回滚代码；不删除原 Secrets、报告或数据库。

本文为该 fork 的中文运维说明，不修改上游中英文产品首页；未新增英文副本。
