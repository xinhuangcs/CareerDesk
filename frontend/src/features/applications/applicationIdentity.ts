// Keep this list identical to backend/platform/database/identity.py. Identity
// comparison removes only this frozen Unicode whitespace set; display text is
// preserved everywhere else.
const IDENTITY_WHITESPACE_CODEPOINTS = new Set([
  9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760,
  8192, 8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
  8232, 8233, 8239, 8287, 12288,
]);

export function normalizeApplicationIdentityPart(value: string | null): string | null {
  return value === null
    ? null
    : Array.from(value).filter((character) => (
      !IDENTITY_WHITESPACE_CODEPOINTS.has(character.codePointAt(0) ?? -1)
    )).join("");
}
