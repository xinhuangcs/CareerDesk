import type { ApplicationStage } from "./timelineContract";

export const TIMELINE_PROFILE_FIELDS = [
  "company",
  "position",
  "department",
  "channel",
  "stage",
  "current_step",
  "applied_date",
  "pause_reason",
  "jd_text",
] as const;

export type TimelineProfileField = typeof TIMELINE_PROFILE_FIELDS[number];

export type TimelineProfileDraft = Omit<Record<TimelineProfileField, string>, "stage"> & {
  expected_revision: number;
  stage: ApplicationStage;
};

/**
 * Three-way merge for the profile CAS command. Fields changed only by the
 * server adopt the saved value; fields changed locally keep the user's draft.
 * A field is reported as conflicting only when both sides changed it to
 * different values, so the UI can require an explicit review before retrying.
 */
export function mergeConflictingApplicationProfile(
  base: TimelineProfileDraft,
  draft: TimelineProfileDraft,
  saved: TimelineProfileDraft,
): { draft: TimelineProfileDraft; conflictingFields: TimelineProfileField[] } {
  const merged = { ...saved };
  const conflictingFields: TimelineProfileField[] = [];
  for (const field of TIMELINE_PROFILE_FIELDS) {
    const locallyChanged = draft[field] !== base[field];
    const externallyChanged = saved[field] !== base[field];
    if (locallyChanged) {
      if (field === "stage") merged.stage = draft.stage;
      else merged[field] = draft[field];
    }
    if (locallyChanged && externallyChanged && draft[field] !== saved[field]) {
      conflictingFields.push(field);
    }
  }
  return { draft: merged, conflictingFields };
}

export function timelineProfileDraftChanged(
  draft: TimelineProfileDraft,
  saved: TimelineProfileDraft,
): boolean {
  return TIMELINE_PROFILE_FIELDS.some((field) => draft[field] !== saved[field]);
}
