import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

function renderBlockingEntry(): Plugin {
  return {
    name: "careerdesk-render-blocking-entry",
    transformIndexHtml: {
      order: "post",
      handler(html) {
        let entryFound = false;
        const transformed = html.replace(/<script\b[^>]*>/gi, (tag) => {
          if (
            entryFound
            || !/\btype=["']module["']/i.test(tag)
            || !/\bsrc=["']\/(?:src|assets)\//i.test(tag)
          ) {
            return tag;
          }
          entryFound = true;
          return /\bblocking=["']render["']/i.test(tag)
            ? tag
            : tag.replace(/>$/, " blocking=\"render\">");
        });
        if (!entryFound) {
          throw new Error("CareerDesk module entry not found while enforcing render blocking");
        }
        return transformed;
      },
    },
  };
}

// Development uses a Vite proxy; production serves the build from FastAPI on one origin.
export default defineConfig({
  plugins: [react(), tailwindcss(), renderBlockingEntry()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/healthz": "http://127.0.0.1:8000",
    },
  },
});
