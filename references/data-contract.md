# 数据与证据约定

## 配置

配置是 JSON，包含 schema_version: 1、focus、window_days（1–365）和 sources。每个数据源有唯一 id（小写字母、数字和连字符）、type、limit（1–100）。

- skills_html：url 为公开 Trending 或 Hot 页面，view 为 trending 或 hot，keywords 为可选筛选词列表。只解析页面实际展示的行；页面可能折叠同一仓库的其他技能，因此结果是可见行的样本，不是完整排行榜。
- github_issues：query 为 GitHub Issue 搜索表达式，可使用 {since} 占位符替换为采集窗口起始日期。推荐明确 is:issue、仓库和时间范围。采集器按最近更新排序，保留创建和更新时间。
- hn_search：query 为检索词，tags 默认为 story，按创建时间限制窗口。Show HN / Launch HN 归为 launch，其余归为 discussion；均需审阅正文才知道是否含需求。

每个来源请求有 25 秒超时和 5 MB 大小限制，不读凭据。网络超时、408/429/5xx，以及明确的 403 限流最多请求三次，保留每次状态；遵循 Retry-After / 限流重置时间，需等待超过 10 秒时保留失败和建议等待时长，交给后续重跑。页面解析变化和普通权限错误不盲目重试。部分失败保留有效线索，不写成全部成功。初始配置位于项目根目录，可直接调整主题、仓库、数量。

失败时可复用同一输出根目录里 24 小时以内、配置完全一致且原始响应哈希有效的成功快照。manifest 的来源状态仍为 error，cache 单独记录原始采集时间与来源轮次；缓存 observation 标 cached，不产生新需求票数。超过时限、查询已改或快照被改过则不复用。signals.json 包含可继续研究的有效线索，退出码仍如实为部分失败或全部失败。

## 快照

每轮独立保存到 data/runs/<UTC时间>/：

- manifest.json：配置快照、实际请求 URL、采集时间、状态、原始文件 SHA-256。
- raw/：原始网络响应；只作为可追溯证据。
- signals.json：规范化的候选线索。
- inbox.md：便于阅读的索引。

signal 的 id 根据规范化 URL 生成。同一条线索可有多个 observations，每次保留来源和榜单指标，绝不累加为总热度。规范化去掉片段和常见跟踪参数，保留 HN 的 item id 等语义参数；跨站转载由研究者在审阅时合并。

kind：supply / feedback / launch / discussion。summary 是原始正文或摘要，可能包含不相关信息和未经证实的用户说法。

榜单 created_at 为 null，表示创建时间未知。榜单观察时间不等于技能首次发布时间。GitHub 按 updated 采集可能带回老问题，报告必须分别列出日期。

## cards.json

顶层为列表。每张卡要求：

    {
      "id": "short-action-name",
      "title": "具体任务名称",
      "user": "目标用户",
      "scenario": "触发情境和当前障碍",
      "input": "需要的文件、文本和参数",
      "output": "可检查的交付物",
      "gap": "当前办法及未覆盖的部分",
      "differentiation": "本人可加入的判断或资源",
      "cost": "预估开发工作、依赖和持续成本；明确是估计",
      "next_step": "一个可实际执行的验证动作",
      "success_checks": ["可观察的成功条件"],
      "evidence": [
        {
          "signal_id": "来自 signals.json 的 ID",
          "relationship": "direct_request",
          "note": "这条证据支持什么、不支持什么"
        }
      ],
      "ratings": {
        "personal_fit": {"score": 1, "reason": "匹配本人工作的理由"},
        "repeatability": {"score": 1, "reason": "任务重复发生的依据"},
        "feasibility": {"score": 1, "reason": "现有工具是否能完成"},
        "differentiation": {"score": 1, "reason": "已有方案之外的增量"}
      }
    }

这是格式示意，不是已核实的需求。实际 signal_id 必须出现在本轮快照中。

relationship：

- direct_request：正文明确提出相关任务或障碍；只接受 feedback 或 discussion，未核实的已关闭 Issue 不接受。
- supporting_supply：现有技能或工具说明有人提供此能力，不能作为用户痛点证据。
- context：背景、反例、作者发布自己的产品、相邻问题。
- resolved_feedback：已经有解决办法的反馈，说明剩余缺口。

每项评分 1–5：1 低、3 中、5 高，必须给理由。默认权重：本人匹配 35%、复用 25%、可实现 25%、差异化 15%。分数表达个人工作优先级，不量化市场价值。

报告单独显示需求证据状态：没有直接反馈、单条反馈、多个不同公开账号的反馈。不同账号也不证明不同自然人、真实购买或市场规模。需求存在、原型可用、本人复用有收益是三个需要分别验证的结论。
