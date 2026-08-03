export type MarkdownImageSourceKind = "local" | "remote" | "blocked";

const URI_SCHEME = /^[a-z][a-z0-9+.-]*:/i;
const URL_CONTROL_CHARACTER = /[\u0000-\u001f\u007f]/;

/**
 * Markdown is untrusted model/content output.  Only relative same-origin image
 * paths may render automatically.  Explicit and protocol-relative network URLs
 * require a user click; every other scheme is rejected rather than handed to
 * the browser's image loader.
 */
export function markdownImageSourceKind(source: string | undefined): MarkdownImageSourceKind {
  const value = (source ?? "").trim();
  if (!value) return "blocked";
  // react-markdown currently sanitizes these before calling the component, but
  // keep this classifier safe on its own: WHATWG URL parsing removes some C0
  // characters and could otherwise turn an apparently relative value into an
  // absolute network URL in a future caller.
  if (URL_CONTROL_CHARACTER.test(value)) return "blocked";
  if (/^https?:\/\//i.test(value) || /^[/\\]{2}/.test(value)) return "remote";
  if (URI_SCHEME.test(value) || value.startsWith("\\")) return "blocked";
  return "local";
}
