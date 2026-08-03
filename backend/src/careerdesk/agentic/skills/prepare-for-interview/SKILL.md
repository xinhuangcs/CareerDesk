---
name: prepare-for-interview
description: 汇总目标公司的调研报告、岗位打法、简历、知识弱点和历史练习，形成临近面试的优先准备方案；当用户问某场面试该怎么准备时使用。
tools: [query_timeline, query_prep, request_application_prep, query_library, query_study, query_grill, query_status]
---
# 生成面试准备方案

1. 用 `query_timeline` 定位目标公司、岗位、阶段与时间；信息已在用户问题中明确时不要重复追问。
2. 用 `query_prep` 获取公司报告与岗位调研页；缺失时如实说明，不编造公司近况。
   用户明确要求当场生成或刷新时，才用 `request_application_prep` 启动后台任务；启动后给出页面入口，不在本轮等待或轮询。
3. 用 `query_library` 读取相关简历，用 `query_study` 读取岗位相关弱点和题目。
4. 如用户有拷打记录，用 `query_grill` 补充真实表现；必要时用 `query_status` 提醒反复出现的状态因素。
5. 按“最可能被问且当前最薄弱”排序，输出少量可执行准备项，并标明每项依据来自哪个本地数据来源。

输出形态：先两三行交代目标面试（公司/岗位/当前环节/时间），再给按优先级排序的准备清单——每项写清准备什么、为什么优先、依据来自哪个数据来源；控制在一屏内读完，不铺陈全部原始数据。

默认只读。除非用户另外明确要求记录或修改，不调用写入 Tool，也不声称生成了数据库中不存在的准备产物。
