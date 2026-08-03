import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { markdownImageSourceKind } from "./features/markdown/remoteImages";
import { useLocalizer } from "./i18n/useLocalizer";

export default function MarkdownRenderer({ text }: { text: string }) {
  const l = useLocalizer();
  return (
    <div className="md-body text-sm leading-relaxed [&_h1]:mb-2 [&_h1]:mt-3 [&_h1]:text-base [&_h1]:font-semibold [&_h2]:mb-1.5 [&_h2]:mt-3 [&_h2]:text-[15px] [&_h2]:font-semibold [&_h3]:mt-2 [&_h3]:font-semibold [&_p]:my-1.5 [&_ul]:my-1.5 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:my-1.5 [&_ol]:list-decimal [&_ol]:pl-5 [&_li]:my-0.5 [&_blockquote]:my-2 [&_blockquote]:border-l-2 [&_blockquote]:border-warn [&_blockquote]:bg-warn-soft [&_blockquote]:py-1 [&_blockquote]:pl-3 [&_code]:rounded [&_code]:bg-panel-2 [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-[13px] [&_pre]:my-2 [&_pre]:overflow-x-auto [&_pre]:rounded-lg [&_pre]:bg-panel-2 [&_pre]:p-3 [&_hr]:my-3 [&_hr]:border-line [&_strong]:font-semibold">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          table: ({ children }) => (
            <div className="my-2 overflow-x-auto">
              <table className="min-w-full border-collapse text-sm">{children}</table>
            </div>
          ),
          th: ({ children }) => (
            <th className="border-b border-line bg-panel-2 px-3 py-1.5 text-left font-semibold">{children}</th>
          ),
          td: ({ children }) => <td className="border-b border-line-2 px-3 py-1.5">{children}</td>,
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer" className="text-info underline">{children}</a>
          ),
          img: ({ src, alt }) => {
            const kind = markdownImageSourceKind(src);
            if (kind === "local") {
              return <img src={src} alt={alt ?? ""} loading="lazy" className="my-2 max-w-full rounded-lg border border-line" />;
            }
            if (kind === "remote") {
              return (
                <span role="note" className="my-1 inline-flex flex-wrap items-center gap-1 rounded-lg bg-panel-2 px-2 py-1 text-xs text-ink-3">
                  {l("疑似外部图片链接，请自行判断，确认安全后再点击查看：", "This may be an external image. Check the destination before opening it: ")}
                  <a href={src} target="_blank" rel="noopener noreferrer" className="text-info underline">
                    {alt?.trim() || l("打开图片链接", "Open image link")}
                  </a>
                </span>
              );
            }
            return (
              <span role="note" className="rounded-lg bg-panel-2 px-2 py-1 text-xs text-ink-3">
                {l("图片地址无法安全打开，已隐藏", "The image address could not be opened safely and was hidden.")}
              </span>
            );
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
