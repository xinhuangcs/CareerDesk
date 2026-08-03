export type PromptIcon = "replay" | "target" | "clipboard" | "bookmark" | "board" | "pulse";

export type QuickPrompt = {
  title: string;
  hint: string;
  text: string;
  icon: PromptIcon;
};

export type QuickPromptGroup = {
  label: string;
  hint: string;
  prompts: QuickPrompt[];
};

export const QUICK_PROMPT_GROUPS: QuickPromptGroup[] = [
  {
    label: "管理进展",
    hint: "记录、跟进与统计",
    prompts: [
      {
        title: "查看求职进展",
        hint: "汇总每个岗位的阶段、日程和下一步",
        text: "请汇总我目前的求职进展，以及每个岗位的下一步。",
        icon: "board",
      },
      {
        title: "找出待跟进岗位",
        hint: "检查下一步逾期或长期没有新进展的岗位",
        text: "请找出下一步已经逾期，或者 30 天没有新进展的岗位，并告诉我应该先跟进哪几个。",
        icon: "board",
      },
      {
        title: "制定本周行动",
        hint: "结合日程、待跟进岗位和练习表现安排本周重点",
        text: "请结合未来 7 天日程、待跟进岗位和最近的练习表现，给我一份本周求职行动计划，最多安排 3 件事。",
        icon: "target",
      },
      {
        title: "批量导入岗位",
        hint: "上传表格文件，核对后加入求职进展",
        text: "请帮我批量导入这份表格中的岗位，并生成可核对的导入预览",
        icon: "clipboard",
      },
      {
        title: "统计投递表现",
        hint: "按阶段和渠道统计当前投递快照",
        text: "请统计我当前各阶段和渠道的投递情况，并指出最值得关注的现象。",
        icon: "board",
      },
    ],
  },
  {
    label: "备战面试",
    hint: "调研、简历与练习",
    prompts: [
      {
        title: "准备最近面试",
        hint: "定位下一场面试，读取或补齐公司与岗位调研",
        text: "请找出我下一场要面试的岗位并读取已有调研；如果调研缺失、失败或已过期，请启动公司与岗位调研，不要刷新仍然有效的报告。",
        icon: "target",
      },
      {
        title: "检查简历适配",
        hint: "查看下一场面试岗位的匹配点、缺口与下一步",
        text: "请找出我下一场要面试的岗位，读取它已有的简历适配，告诉我最关键的匹配点、缺口和下一步；如果还没有结果，请带我去对应页面。",
        icon: "target",
      },
      {
        title: "分析岗位差距",
        hint: "结合目标岗位，找出最值得优先提升的能力",
        text: "请对照目标岗位的 JD，找出我最需要优先提升的 3 项能力。",
        icon: "target",
      },
      {
        title: "继续上次练习",
        hint: "定位最近一场未完成的拷打练习，再由你决定是否继续",
        text: "请找出我最近一场未完成的拷打练习，并带我回去继续。",
        icon: "replay",
      },
      {
        title: "复盘练习表现",
        hint: "读取最近一场已结束练习的逐题反馈",
        text: "请复盘我最近一场已结束的拷打练习，指出做得好的地方、主要缺口和下一步练习重点。",
        icon: "pulse",
      },
    ],
  },
  {
    label: "复盘成长",
    hint: "沉淀经验、状态与偏好",
    prompts: [
      {
        title: "复盘一次面试",
        hint: "整理面试过程、问题、表现与下一步",
        text: "我想复盘一场刚结束的面试：",
        icon: "replay",
      },
      {
        title: "回顾岗位历程",
        hint: "按公司和岗位回看完整进展并提炼关键变化",
        text: "请回顾这个岗位的完整求职历程，并总结关键变化。公司 / 岗位：",
        icon: "replay",
      },
      {
        title: "总结面试状态",
        hint: "从历史复盘中寻找影响发挥的规律",
        text: "请总结我最近面试状态的规律，并给出改进建议。",
        icon: "pulse",
      },
      {
        title: "只做一件小事",
        hint: "精力不足时，只选择一个今天能完成的最小任务",
        text: "我现在精力很低。请结合我的待办和近期状态，只给我一件今天能完成的最小求职任务，不要一次安排很多。",
        icon: "bookmark",
      },
      {
        title: "保存求职偏好",
        hint: "记录方向、城市和薪资等长期偏好",
        text: "请记住我的求职偏好：",
        icon: "bookmark",
      },
    ],
  },
];

export const QUICK_PROMPT_GROUPS_EN: QuickPromptGroup[] = [
  {
    label: "Manage progress",
    hint: "Record, follow up, and measure",
    prompts: [
      {
        title: "Review application progress",
        hint: "Summarise the stage, schedule, and next step for every role",
        text: "Summarise my current job-search progress and the next step for each role.",
        icon: "board",
      },
      {
        title: "Find roles to follow up",
        hint: "Find overdue next steps and roles with no recent progress",
        text: "Find roles whose next step is overdue or that have had no progress for 30 days, and tell me which ones to follow up first.",
        icon: "board",
      },
      {
        title: "Plan this week",
        hint: "Prioritise upcoming events, follow-ups, and practice insights",
        text: "Use my next seven days, roles needing follow-up, and recent practice performance to make a job-search plan for this week with no more than three actions.",
        icon: "target",
      },
      {
        title: "Import roles in bulk",
        hint: "Upload a workbook and review it before adding applications",
        text: "Import the roles from this workbook and prepare a preview for me to review.",
        icon: "clipboard",
      },
      {
        title: "Analyse application results",
        hint: "Summarise the current pipeline by stage and channel",
        text: "Summarise my current applications by stage and channel, and highlight the most important patterns.",
        icon: "board",
      },
    ],
  },
  {
    label: "Prepare for interviews",
    hint: "Research, resumes, and practice",
    prompts: [
      {
        title: "Prepare for my next interview",
        hint: "Find the next interview and read or complete its research",
        text: "Find my next interview and read the existing company and role research. If the research is missing, failed, or expired, start it; do not refresh a report that is still valid.",
        icon: "target",
      },
      {
        title: "Review resume fit",
        hint: "See the strongest matches, gaps, and next step for the next interview",
        text: "Find my next interview, read its existing resume adaptation, and explain the most important matches, gaps, and next step. If it has not been generated, take me to the right page.",
        icon: "target",
      },
      {
        title: "Analyse role gaps",
        hint: "Identify the capabilities that deserve the most attention",
        text: "Compare me with the target role's job description and identify the three capabilities I should improve first.",
        icon: "target",
      },
      {
        title: "Continue my last practice",
        hint: "Find the latest unfinished interview drill so I can resume it",
        text: "Find my latest unfinished interview drill and take me back to continue it.",
        icon: "replay",
      },
      {
        title: "Review practice performance",
        hint: "Use question-level feedback from the latest completed drill",
        text: "Review my latest completed interview drill and explain what went well, the main gaps, and what to practise next.",
        icon: "pulse",
      },
    ],
  },
  {
    label: "Reflect and grow",
    hint: "Capture experience, wellbeing, and preferences",
    prompts: [
      {
        title: "Review an interview",
        hint: "Capture the process, questions, performance, and next step",
        text: "I want to review an interview that just finished:",
        icon: "replay",
      },
      {
        title: "Review a role's history",
        hint: "Trace the complete journey and identify meaningful changes",
        text: "Review the complete application journey for this role and summarise the key changes. Company / role:",
        icon: "replay",
      },
      {
        title: "Understand interview patterns",
        hint: "Find patterns in past reviews that affect performance",
        text: "Summarise the patterns in my recent interview performance and suggest how to improve.",
        icon: "pulse",
      },
      {
        title: "Choose one small action",
        hint: "When energy is low, pick one manageable task for today",
        text: "My energy is very low. Use my current tasks and recent state to give me just one small job-search action I can finish today; do not assign several things at once.",
        icon: "bookmark",
      },
      {
        title: "Save career preferences",
        hint: "Remember long-term preferences such as direction, location, and salary",
        text: "Please remember these job-search preferences:",
        icon: "bookmark",
      },
    ],
  },
];

export const QUICK_PROMPTS = QUICK_PROMPT_GROUPS.flatMap((group) => group.prompts);

function promptRotation(groups: QuickPromptGroup[]): QuickPrompt[] {
  const maxGroupSize = Math.max(...groups.map((group) => group.prompts.length));
  return Array.from(
  { length: maxGroupSize },
  (_, promptIndex) => groups
    .map((group) => group.prompts[promptIndex])
    .filter((prompt): prompt is QuickPrompt => prompt !== undefined),
  ).flat();
}

export const QUICK_PROMPT_ROTATION = promptRotation(QUICK_PROMPT_GROUPS);
export const QUICK_PROMPT_ROTATION_EN = promptRotation(QUICK_PROMPT_GROUPS_EN);

export function quickPromptRotation(locale: "zh-CN" | "en"): QuickPrompt[] {
  return locale === "zh-CN" ? QUICK_PROMPT_ROTATION : QUICK_PROMPT_ROTATION_EN;
}
