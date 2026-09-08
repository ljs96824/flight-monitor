# T曲线与forecast样本入选对账

本次为只读诊断，不改变采样、阈值、日格、regime或预测准入语义。源码审计基线为
`b68245b597313d6dce2b20281c42d22711e654d7`，核实日期为2026-09-08。
以下数据结论仅适用于指定快照与筛选；不是其他航线、未来采集轮或模型优劣结论。

## 输入与复现边界

### 可得性与证据等级

| 证据面 | 可得性与结论 | 等级/时点 |
| --- | --- | --- |
| 固定代码 | Git对象与本地源码可读，基线一致 | direct_evidence / current_requery |
| 上传终端输出 | 当前任务只有维护者给出的计数与摘要SHA，没有可定位的原始终端文件或运行时间 | user_reported；绝对先后关系未核验 |
| 本地生产输入 | 五个状态文件可访问；只做短窗口哈希与历史日志取证 | direct_evidence / current_requery |
| 今晨归档 | backup_id=`20260908T021358Z-2b88e8d2`，含完整core_snapshot及两份复放输出 | direct_evidence / current_requery |
| 本次audit_snapshot | 使用该归档内既有core_snapshot，没有重新捕获活SQLite | direct_evidence / current_requery |
| 异盘备份 | 维护者已核验different_device_verified=true；本笔未重做异盘设备核验 | user_reported |

`historical_output` 不等于 `audit_snapshot`。本次复算确实得到146/95/51/160，
但这些数值不是预设验收目标。归档报告的全文SHA与复算一致，证明的是归档复放的
可重现性；不能据此认证未提供的终端输出时刻或其当时使用的输入。

归档源码SHA等于本次基线。归档记录Python 3.13.14；本次实际Python 3.12.10、
SQLite 3.49.1、numpy 2.5.2、python-dotenv 1.2.2、PyYAML 6.0.3、matplotlib 3.11.1。
没有读取真实.env；进程设置NO_LIVE_API=1、PYTHON_DOTENV_DISABLED=1。
不同Python小版本下，本次两份报告仍与归档逐字节相同。

### 捕获与输入完整性

`runtime_backup.py:746`复用`create_readonly_snapshot`，并非复制正在写入的SQLite主文件。
归档manifest记录collection single-flight、SQLite online backup和JSON锁内读取；
snapshot_manifest记录attempts=1、两库data_version均为1、
consistency=`file_level_stable_inputs`。两库本次以mode=ro打开并执行完整integrity_check，均为`ok`。

- capture记录起点：2026-09-08T02:13:58.820414Z。
- 独立capture结束字段：未记录。snapshot_manifest归档成员mtime为
  2026-09-08T02:14:00.529285Z，作为快照已发布的结束代理，不冒充精确结束事件。
- 归档两份报告成员mtime分别为02:14:07.977719Z与02:14:07.983243Z，均晚于快照发布代理。
- `permission_quality_cells`确实在snapshot_manifest中：16条，引用10个受影响轮次。
- 归档SHA-256：`1b735252c1e2d767be08719ef5063d6a3f4b31d5fb8bed19510f37ae9411ad45`。
- 归档manifest SHA-256：`07808b0d10282ab188d38e3cdc947b26390a25c8fa453e79c3cec2d58cee5d71`。
- snapshot_manifest SHA-256：`c82889d7cb4d32da466df18929cc8f17680565891d4a2fa801fa161c4e45737f`。

| 成员 | 捕获时source SHA-256 | snapshot SHA-256 |
| --- | --- | --- |
| prices.db | ea4ac34e4247c93e5220d57afaf01e01df438a3b16185ea839bead21016c81f9 | 7d1d81d07955c2a106b1ec96b650614124706e814670865bf45c86520fe7ea1c |
| observations.sqlite3 | c2ab5338353e426f1345f2010c347e074f7d64fc3f74821ca24fcbf7ff93e0d4 | f4f671f7219feb04f733e1e100fd18a7f229ac3eb5ce263aae0ed3b53b977e52 |
| api_usage.json | 465409f26109c6c05650e51631b14a03d413b0e202a9908a6085b89467311952 | 465409f26109c6c05650e51631b14a03d413b0e202a9908a6085b89467311952 |

SQLite online backup可能改变文件布局；source与snapshot哈希分别记录，不要求两者相等。
发布后所有长时间对账只读快照，两轮复算前后四个快照成员存在性及SHA完全相同。

### 参数锁定

完整route取自归档`manifest.replay.route`，保存在私有参数文件；不公开逐条航线信息。
pair=null，未指定机场对；按同一城市有向路线读取，economy、price_cny>0、
include_degraded=false。forecast实际as_of=2026-09-08，来自角色过滤后最大observed_day，
不是主机today；复算显式传入该值。两份报告API均收到完整core_snapshot目录。
特别是`tcurve_report._load_default_quality_cells`在目录参数下加载manifest，避免裸DB参数使质量诊断置空。

| 参数或规则 | 本次有效值 | 源码依据 |
| --- | --- | --- |
| T曲线/shape点门 | MIN_SAMPLE_FOR_TCURVE=MIN_SHAPE_N=5 | tcurve.py:43；forecast.py:43、90 |
| level门 | MIN_OBS_FOR_LEVEL=4 | forecast.py:38、231 |
| 回测技能门 | MIN_BACKTEST_CASES=5；相对MAPE改善至少0.10 | forecast.py:39、477 |
| 方法版本 | tcurve_v2 / forecast_v2 / holiday_calendar_v1 | method_registry.py；forecast.py:19；holidays.py:25 |
| 日型 | 静态节假日标签，HOLIDAY_SHOULDER_DAYS=1；互斥优先级节日、节前、节后、周末、normal | holidays.py:24；forecast.py:106、140 |
| 源策略 | 按route_type、economy及observed_day求expected_listing_sources，保留退役前历史应到源 | source_profiles.py:152；tcurve.py:342 |

本快照179格的应到源集合为juhe单源95格、hasdata+juhe双源84格；T分别95/51，S分别44/51。
这些是历史观测日规则，不将当前单源策略倒写到旧日格。

## 统计单位

价格原始层来自observations；collection_cells提供角色、cohort与采集终态来源，
不是把一条请求或一个航班直接当成一个统计样本。
有向路线原始观测72597行，其中正价格economy读取72559行；其余38行为非该舱位读取范围。
该路线原始非正价格0行，时间歧义排除0行。

`tcurve.py:133`在SQL限制economy和正价格，之后按城市方向过滤。
角色连接键含round_id/source/机场对/depart_date/cabin，同键按既有优先级取角色；
daily outcome连接另用机场对/depart_date/observed_day_shanghai/cabin（tcurve.py:221）。
私有lineage表区分城市日格候选台账与实际附着的request_fingerprint，不将候选等同实际贡献。

`fold_tcurve_daily_cells`（tcurve.py:279）实际键为：
`origin_city / dest_city / depart_date / observed_day`。
机场对和舱位只保留为读取筛选与原始来源，折叠后不再拆回机场日格。
价格取城市日格global_min，保留完整`sample_roles`集合；源状态/退化按原实现求值。

| 层次 | 单位与本次规模 | 私有追溯关系 |
| --- | --- | --- |
| 原始来源 | 72559条被读取的观测；台账并非同一分母 | observation rowid、实际角色连接组、request_fingerprint及候选台账rowid |
| 城市有向日格 | 179格；33格退化；T入选146格 | 完整四元键、原始来源、sample_roles、T/S成员资格 |
| forecast shape | S=95格，8个独立出发日 | regime/shape训练格集合 |
| level | 8个目标出发日，各带as_of cutoff | 目标—cutoff—候选日格/实际ratio日格；均无可用ratio |
| backtest | 每个horizon=95个候选目标，共285个候选关系 | horizon、target_day、cutoff_day、fit/同regime fit/目标格/history/同T关系 |

私有证据不使用无上下文的`backtest_eligible`布尔代替逐折关系。

## 双向差集

T定义为同快照、同方向、不含退化的T曲线入选日格；S为同筛选下经过角色、as_of、
非退化与正价格规则后送入forecast shape的日格，不是通过shape点样本门的日格。

| 集合 | 数量 |
| --- | ---: |
| T | 146 |
| S | 95 |
| T-S | 51 |
| S-T | 0 |
| T∩S | 95 |

逐键验证：`|T|-|S| = 146-95 = 51 = |T-S|-|S-T|`。
进一步逐键验证T-S精确等于T中“不拥有任何FORECAST_SAMPLE_ROLES允许角色”的集合，
不是以净差额等于probe标签数替代集合证明。

| 角色/排除类别 | 本次事实 |
| --- | --- |
| 仅cross_sectional_probe | 51格，全部且仅这些格属于T-S |
| probe与允许角色混合 | 0格；代码允许此类混合格，不能推广成本数据已实测混合probe |
| 含legacy | 71格，全部保留；其中2格还有其他允许角色 |
| 允许角色但不含legacy/probe | 24格，全部保留 |
| 其他/未知角色 | 0格 |
| 时间截止、非正价格、时间歧义导致T-S | 均为0 |
| 退化 | 33格已在T之前排除，S同样排除；不是额外的51格差集 |

设计依据是`docs/research-cohort-v2-2026-08-26.md:138`的“版本与消费纪律”：
trajectory_anchor/user_monitor/legacy可进入forecast shape，probe默认排除；原始T曲线披露全部角色。
代码事实是`forecast.py:44、58`的集合交集测试，空角色回退legacy，混合角色有一个允许角色即保留。
本次数据实际遵守该规则；不按批次编号推定设计来源，也不对设计优劣或阈值提出改写。

## 多角色计数

T的角色成员数分别为probe=51、legacy=71、trajectory_anchor=26、user_monitor=12，合计160。
日格总数146，多角色日格13；完整组合分布如下。

| 完整sample_roles组合 | 日格数 |
| --- | ---: |
| cross_sectional_probe | 51 |
| legacy | 69 |
| trajectory_anchor | 13 |
| legacy + trajectory_anchor | 1 |
| trajectory_anchor + user_monitor | 11 |
| legacy + trajectory_anchor + user_monitor | 1 |

12个双角色格贡献12次额外角色归属，1个三角色格贡献2次。
等式逐格验证：`160 - 146 = 14 = sum(max(len(sample_roles)-1, 0))`。
这是额外角色归属次数，不是“丢失记录”。

## 方向诊断

价格统计按origin_city与dest_city有序匹配（tcurve.py:248）；反向价格进入正向统计的数量为0。
质量展示则在`scripts/tcurve_report.py:generate_report`按无序城市集合匹配
`permission_quality_cells`，故可同时展示反向诊断。质量显示不会新增价格日格。

| 相对审计方向 | 展示质量格 | 无价格行的缺失格 | degraded=true | 失败请求事件 |
| --- | ---: | ---: | ---: | ---: |
| 正向 | 5 | 0 | 1 | 28 |
| 反向 | 11 | 8 | 0 | 43 |

8条反向缺失来自snapshot_manifest质量层的`all_day_row_count==0`，并非8条价格观测被丢弃。
质量生成链为`runtime_backup.build_permission_quality_metadata:559` →
`audit_permission_pollution.build_audit:459` → 日志PermissionError请求分组与库中全日经济舱行关联。
缺失格本身没有价格，不贡献global_min；质量层degraded计数也不能替代179价格日格中的33格退化数。

质量原始来源为7份历史round日志，覆盖脚本登记的10个受影响轮次，提取71个失败事件。
归档只保留最近七日round日志，未包含这7份较早日志；本次从本地只读取得并立即固化，
逐份SHA保存在私有manifest，等级为direct_evidence/current_requery，不冒充捕获时的日志副本。
以这7份日志和同一core_snapshot重建的16条affected_cells，与归档质量元数据结构化逐字段完全相等。
它证明可重建性，不证明这些历史日志自首次产生以来未经任何编辑。

## 回测归因

实际实现为`forecast.walk_forward_backtest:484`。
计数单位首先是`(depart_date,target_day)`候选，每个horizon独立计数，不是原始观测行。
本次95个可用日格形成95个唯一候选，没有该映射键碰撞。
每个cutoff严格为`target_day-horizon`；fit只含observed_day<=cutoff，target_day>cutoff。
shape只使用与目标相同regime的fit，并调用现有build_shape、estimate_level与predict_price。
未用当前全量shape替代历史fold shape，全部285折均检查无未来观测泄漏。

### 顺序淘汰（候选单位）

| horizon | 候选 | 目标shape不存在 | 目标shape不足5 | 此后level失败 | 此后history缺失 | 此后同T缺失 | cases |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 95 | 87 | 8 | 0 | 0 | 0 | 0 |
| 3 | 95 | 94 | 1 | 0 | 0 | 0 | 0 |
| 7 | 95 | 95 | 0 | 0 | 0 | 0 | 0 |

每行候选数精确等于互斥淘汰项加cases。predict_price真实状态分别为“无可用shape”
或“shape样本不足(n=1/2)”；285个目标点中276个不存在、7个n=1、2个n=2。
全部案例集合与原生产walk_forward_backtest返回集合相等，不只是比较n=0。

### 依赖链与补充诊断（不重复计损）

| horizon | level可用ratio=0的折数 | 同出发日history存在 | 同T基准存在 |
| --- | ---: | ---: | ---: |
| 1 | 95 | 87 | 24 |
| 3 | 95 | 83 | 11 |
| 7 | 95 | 78 | 6 |

生产代码会构造level/history/t_prices；但predict_price在shape失败处已返回，后续level检查
以及复合条件中的history、t_prices真值分支没有因此变成独立淘汰原因。
上表后两列作为诊断计算记录可得性，不声称流程已通过shape门或到达后续成功分支。
候选同T基准按整个fit而非只按目标regime取值，忠实保留现行代码。

当前全量同regime shape也仅有87个点：81个n=1、4个n=2、2个n=3，全低于5。
因此可同时成立“shape证据不足 → 可用ratio=0 → level不可靠 → 回测零案例”。
不能把链中各节点重复累计为多个丢失案例。三horizon的模型、朴素、T曲线MAE/MAPE均为null；
技能门因case_n=0不足5且不可比较而失败。`n=0`不表示模型表现劣于朴素基线。

## 可靠性语义

来源：`forecast.assess_overall_reliability:278`、`estimate_level:231`、
`source_coverage_for_departure:156`、`regime_departure_n:166`、`evaluate_skill_gate:477`；
报告调用路径为`scripts/forecast_report.py:145、200`。
8个目标出发日的五个actual_value均为0；均为已求值的二元证据门，不是连续概率或准确率。
下表input_n是证据个数，不是人为编造的比例分母；所有比例分母均“不适用”。

| component | actual_value / evaluation_state | predicate | input_scope / input_n | threshold | upstream_dependency / reason |
| --- | --- | --- | --- | --- | --- |
| level_reliability | 8个目标均0 / evaluated_false | bool(level.reliable) | 同目标出发日、cutoff内且对应shape中位可用的ratio；各目标n=0；比例分母不适用 | ratio数>=4 | 同regime shape点门；全部shape中位不可用 |
| shape_reliability | 8个目标均0 / evaluated_false | bool(points)且all(point.sufficient) | 每目标未来7日，共56点；46点不存在以n=0代入，10点n=1；比例分母不适用 | 每点n>=5，七日全通过 | 目标regime与目标T的shape；非只检查一个当前T |
| backtest_skill | 8个目标均0 / evaluated_false | backtest_gate.passed | 报告使用horizon=3技能门；case_n=0；比例分母不适用 | cases>=5且模型相对朴素MAPE改善>=10%，MAPE可比且朴素非0 | 逐折shape/level/基准；本次误差为null，不可计算改善 |
| source_coverage | 8个目标均0 / evaluated_false | bool(relevant)且all(not degraded) | 角色过滤后的同depart_date、observed_day<=as_of；总128格含33历史degraded；比例分母不适用 | 全部相关格非退化，无比例阈值 | 历史源覆盖/collection_state；不是最新一次成功即可通过 |
| regime_match | 8个目标均0 / evaluated_false | regime_departure_n>=MIN_SHAPE_N | 独立出发日：normal=2、holiday_return=1、holiday_eve=1、holiday=4；比例分母不适用 | 独立出发日数>=5 | 仅非退化、as_of内；不是城市日格数或观测行数 |

source_coverage函数自身只用depart_date与as_of上界限制窗口，没有近N日下界，包含历史degraded。
上游已经做路线、经济舱和角色过滤；不能把函数的局部窗口描述误写成全库无筛选。
shape_reliability检查as_of之后1至7日；`_future_shape_points:109`没有排除负T。
本次56个检查点中7个T<0，仍参加当前全通过判定。不是在本笔建议改变该行为。
overall_reliability取五个二元值的最小值；lineage是额外资格规则，不伪装成第六个可靠性分量。

## 证据包与局限

完整私有证据包位于仓库外`<AUDIT_ROOT>/sample-admission-20260908`，不进入Git、生产data或自动执行目录。
含core_snapshot、归档manifest、7份质量来源日志、完整审计程序、参数、原始行与日格映射、
T/S集合、8个level关系、285个backtest关系、阶段计数、报告全文及输入/输出SHA-256。
公开文档只保留聚合，不公开逐条航线—出发日—观测日明细、订阅、邮件、token或payload。

复算命令为`python -X utf8 <AUDIT_ROOT>/sample-admission-20260908/audit.py run --label final1`
与同命令`--label final2`；私有程序固定源码工作区基线，搬迁时须将程序路径指向同一Git对象。
程序只导入分析/只读报告代码，不导入采集计划或发送入口；socket连接/DNS/sendto审计钩子拒绝网络，
两次复算network_denial_hits均为0。Juhe/SerpAPI/Duffel/PushPlus/SMTP/PA Files API实际外呼均为0。

| 证据 | SHA-256 |
| --- | --- |
| 最终audit.py | 9a177eca40bff9b435046634d57194aa5d8a2ab447fb94c0abb7f2c86280cd39 |
| 私有evidence_manifest.json | dd4c34df5a04b941660df0e38641203beb27d625b7fc7f9ff51f812dfe1feb56 |
| 参数文件 | 5e38c23bf05e5a1987643e6f47cf571dba7851fb8a2e9a9db93ff562929ac265 |
| 集合文件 | 6bc84005c69d40cb1d1a03513f46f3c09876481b687cf73215edc874806f2015 |
| backtest关系与生产返回对象 | 15e96f09e689b4fb8b4cc004bba70e668cbb044d7ee5a93d99e1dc3c535b8f9a |
| level关系 | 054c76ea60a5407bac49cf31489d06515947d8c5492520ee3fac3b6654447bec |
| 汇总结果 | 67a00b38dad1d37a61cb1b81ceb65d9fd2fb4a1438132e21bfd27296b104b34b |
| tcurve复放全文 | ba0fb773c3d30b60b828ea657cafd048a3034914c99bed6d13ca03ce28833196 |
| forecast复放全文 | de728dc4de5f1ce655ebea6f6fabbe4be7641e209702287c1d49eb1e1ad5744a |

最终程序连续两次复算的10份输出逐文件SHA相同；与归档两份报告全文SHA也相同。
集合等式、角色等式、逐折无泄漏、互斥阶段计数、生产返回shape/level/reliability/backtest相等均执行断言。
最初版本采集误列非项目依赖pandas导致一次私有脚本失败；修正后上述证据全部来自最终程序的两次完整运行。

生产只在短静默窗口检查：2026-09-08T02:46:25.590289Z至02:46:27.471566Z，
默认collection single-flight前后均空闲，未跨09:30/15:00/21:00窗口。
五文件前后均存在且SHA相同；没有要求正常采集为长时间审计停机。

| 生产状态文件 | before=after SHA-256 |
| --- | --- |
| prices.db | ea4ac34e4247c93e5220d57afaf01e01df438a3b16185ea839bead21016c81f9 |
| observations.sqlite3 | c2ab5338353e426f1345f2010c347e074f7d64fc3f74821ca24fcbf7ff93e0d4 |
| api_usage.json | 465409f26109c6c05650e51631b14a03d413b0e202a9908a6085b89467311952 |
| subscriptions.json | 2ff5d41e23ec7a8c71987c36eb9deb0c8b8e1b849a761c221a424d075a5b9127 |
| runtime_config.yaml | c1652e99d6f0a6892303016065bd2f5ec2dffe66b9d502e4f6d5f1c6555d1664 |

文档合同仅验证章节与边界陈述。CI没有上传的生产数据库，不宣称CI重做本次真实样本审计。
RED预期唯一测试为`DocsAccuracyTest.test_sample_admission_reconciliation_report_structure`，
报告不存在时实际仅以`missing_sample_admission_report`失败；章节GREEN不能替代上述数据断言。

未完成/证据局限：未取得原始上传终端文件与时间戳；归档没有独立capture结束事件；
较早日志不是归档内副本；未独立重验异盘设备；本次不修改预测准入或评价设计优劣。
这些缺口不被合成fixture或后来的生产状态补齐；结论严格限定为所列真实audit_snapshot。
