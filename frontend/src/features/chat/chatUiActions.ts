export type ChatUiAction = {
  kind:
    | "open_application"
    | "open_timeline"
    | "open_application_research"
    | "open_resume_adaptation"
    | "open_grill_session"
    | "open_grill"
    | "open_questions"
    | "open_library"
    | "open_resume";
  resourceId: number | null;
  label: string;
  href: string;
};

const RESOURCE_ACTIONS = new Set<ChatUiAction["kind"]>([
  "open_application",
  "open_application_research",
  "open_resume_adaptation",
  "open_grill_session",
  "open_resume",
]);

const LABELS: Record<ChatUiAction["kind"], readonly [string, string]> = {
  open_application: ["打开岗位详情", "Open role details"],
  open_timeline: ["打开求职进展", "Open Application Tracker"],
  open_application_research: ["查看公司与岗位调研", "View company and role research"],
  open_resume_adaptation: ["查看简历适配", "View résumé adaptation"],
  open_grill_session: ["打开这次练习", "Open this practice session"],
  open_grill: ["打开拷打室", "Open Interview Lab"],
  open_questions: ["打开题库", "Open Question Bank"],
  open_library: ["打开简历管理", "Open résumé library"],
  open_resume: ["打开这份简历", "Open this résumé"],
};

function href(kind: ChatUiAction["kind"], resourceId: number | null): string {
  switch (kind) {
    case "open_application": return `/timeline?application=${resourceId}`;
    case "open_timeline": return "/timeline";
    case "open_application_research": return `/timeline?application=${resourceId}&tab=research`;
    case "open_resume_adaptation": return `/timeline?application=${resourceId}&tab=adaptation`;
    case "open_grill_session": return `/grill?session=${resourceId}`;
    case "open_grill": return "/grill";
    case "open_questions": return "/grill?view=questions";
    case "open_library": return "/library";
    case "open_resume": return `/library?resumeId=${resourceId}`;
  }
}

export function chatUiActionsFromServer(value: unknown, locale: UiLocale = "zh-CN"): ChatUiAction[] | null {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > 8) return null;
  const result: ChatUiAction[] = [];
  const seen = new Set<string>();
  for (const item of value) {
    if (item === null || typeof item !== "object" || Array.isArray(item)) return null;
    const record = item as Record<string, unknown>;
    const keys = Object.keys(record).sort();
    const kind = record.kind;
    if (typeof kind !== "string" || !(kind in LABELS)) return null;
    const typedKind = kind as ChatUiAction["kind"];
    const needsResource = RESOURCE_ACTIONS.has(typedKind);
    const expectedKeys = needsResource ? ["kind", "resource_id"] : ["kind"];
    if (keys.length !== expectedKeys.length
        || keys.some((key, index) => key !== [...expectedKeys].sort()[index])) return null;
    const rawId = record.resource_id;
    const resourceId = needsResource && typeof rawId === "number"
      && Number.isSafeInteger(rawId) && rawId > 0 ? rawId : null;
    if (needsResource && resourceId === null) return null;
    const identity = `${typedKind}:${resourceId ?? ""}`;
    if (seen.has(identity)) return null;
    seen.add(identity);
    result.push({
      kind: typedKind,
      resourceId,
      label: LABELS[typedKind][locale === "en" ? 1 : 0],
      href: href(typedKind, resourceId),
    });
  }
  return result;
}
import type { UiLocale } from "../../i18n/i18n";
