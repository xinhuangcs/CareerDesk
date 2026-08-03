function canonicalNote(value: string | null): string {
  return value?.trim() ?? "";
}

function joinDistinctNoteSections(primary: string, addition: string): string {
  if (!addition) return primary;
  if (!primary) return addition;
  if (primary === addition || primary.endsWith(`\n${addition}`)) return primary;
  if (addition.endsWith(`\n${primary}`)) return addition;
  return `${primary}\n${addition}`;
}

/**
 * Three-way merge for a note CAS conflict.
 *
 * The normal concurrent write is an Agent append, so apply the server suffix to
 * the user's edited draft.  Divergent replacements fall back to retaining both
 * complete values; the UI then asks the user to inspect the merged draft.
 */
export function mergeConflictingApplicationNote(
  base: string | null,
  draft: string,
  saved: string | null,
): string {
  const baseNote = canonicalNote(base);
  const draftNote = canonicalNote(draft);
  const savedNote = canonicalNote(saved);

  if (savedNote === baseNote) return draftNote;
  if (draftNote === savedNote) return draftNote;
  if (draftNote === baseNote) return savedNote;

  if (savedNote.startsWith(baseNote)) {
    const serverAddition = savedNote.slice(baseNote.length).trim();
    return joinDistinctNoteSections(draftNote, serverAddition);
  }
  if (draftNote.startsWith(baseNote)) {
    const userAddition = draftNote.slice(baseNote.length).trim();
    return joinDistinctNoteSections(savedNote, userAddition);
  }
  return joinDistinctNoteSections(savedNote, draftNote);
}
