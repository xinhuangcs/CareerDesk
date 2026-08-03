import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const pageUrl = new URL("./SettingsPage.tsx", import.meta.url);
const storageUrl = new URL("./StorageSettingsSection.tsx", import.meta.url);
const preferencesUrl = new URL("../preferences/PreferencesSettingsSection.tsx", import.meta.url);
const themeUrl = new URL("../theme/ThemeSettingsSection.tsx", import.meta.url);
const languageUrl = new URL("./LanguageSettingsSection.tsx", import.meta.url);
const grillUrl = new URL("../grill/GrillVisibilitySettingsSection.tsx", import.meta.url);
const stylesUrl = new URL("../../index.css", import.meta.url);

test("settings are split into three URL-backed pages", async () => {
  const source = await readFile(pageUrl, "utf8");

  assert.match(source, /\["network", "network"\]/);
  assert.match(source, /\["personal", "personal"\]/);
  assert.match(source, /\["appearance", "appearance"\]/);
  assert.match(source, /role="tablist"/);
  assert.match(source, /role="tab"/);
  assert.equal(source.match(/role="tabpanel"/g)?.length, 3);
  assert.match(source, /searchParams\.get\("page"\)/);
  assert.match(source, /SECTION_PAGE\[requestedSection/);
  assert.match(source, /experiments: "appearance"/);
  assert.match(source, /language: "appearance"/);
});

test("each settings page follows the requested semantic order", async () => {
  const source = await readFile(pageUrl, "utf8");
  const privacy = source.indexOf('<section id="settings-privacy"');
  const model = source.indexOf('<section id="settings-model"');
  const retrieval = source.indexOf('<section id="settings-retrieval"');
  const research = source.indexOf('<section id="settings-research"');
  const preferences = source.lastIndexOf('<div id="settings-preferences"');
  const storage = source.lastIndexOf('<div id="settings-storage"');
  const language = source.lastIndexOf("<LanguageSettingsSection />");
  const appearance = source.lastIndexOf("<ThemeSettingsSection />");
  const experiments = source.lastIndexOf("<GrillVisibilitySettingsSection />");

  assert.ok(privacy >= 0 && privacy < model && model < retrieval && retrieval < research);
  assert.ok(preferences >= 0 && preferences < storage);
  assert.ok(language >= 0 && language < appearance && appearance < experiments);
  assert.doesNotMatch(source, /settings-capacity/);
  assert.doesNotMatch(source, /overflow-x-auto/);
});

test("language choices apply immediately from the appearance page", async () => {
  const [page, language] = await Promise.all([
    readFile(pageUrl, "utf8"),
    readFile(languageUrl, "utf8"),
  ]);

  assert.match(page, /<LanguageSettingsSection \/>/);
  assert.match(language, /"system"/);
  assert.match(language, /"zh-CN"/);
  assert.match(language, /"en"/);
  assert.match(language, /saveLocaleMode\(option\.value\)/);
  assert.match(language, /aria-pressed=\{selected\}/);
});

test("English settings choices wrap without creating narrow-screen overflow", async () => {
  const [page, language, theme, styles] = await Promise.all([
    readFile(pageUrl, "utf8"),
    readFile(languageUrl, "utf8"),
    readFile(themeUrl, "utf8"),
    readFile(stylesUrl, "utf8"),
  ]);

  assert.match(page, /className=\{`button-wrap min-w-0/);
  assert.match(language, /className=\{`button-wrap min-w-0/);
  assert.match(theme, /className=\{`button-wrap min-w-0/);
  assert.match(styles, /\.button-wrap \{ white-space: normal; overflow-wrap: anywhere; \}/);
  assert.doesNotMatch(styles, /button \{ white-space: nowrap; \}/);
  assert.match(styles, /\.chip \{[\s\S]*?white-space: nowrap;/);
  assert.match(styles, /\.segmented-item \{[\s\S]*?white-space: nowrap;/);
});

test("local data paths cannot widen the settings page", async () => {
  const source = await readFile(storageUrl, "utf8");

  assert.match(source, /<code className="mt-1\.5 block break-all text-xs text-ink-2">\{state\[key\]\}<\/code>/);
  assert.match(source, /<span className="break-all text-ink-2">\{state\.credential_location\}<\/span>/);
  assert.match(source, /<code className="mt-1 block break-all">\{state\.migration_pending\}<\/code>/);
});

test("the settings heading and page tabs stay anchored above dividers", async () => {
  const source = await readFile(pageUrl, "utf8");

  assert.match(source, /<div className="sticky top-\[49px\][^\"]*bg-surface/);
  assert.match(source, /<header className="px-4 py-4/);
  assert.doesNotMatch(source, /<header className="[^\"]*border-/);
  assert.doesNotMatch(source, /管理联网与 AI、个人偏好、本机数据和界面外观/);
  assert.match(source, /<nav[\s\S]*className="border-b border-line/);
  assert.doesNotMatch(source, /<nav[\s\S]*className="sticky/);
  assert.match(source, /scroll-mt-48 min-\[560px\]:scroll-mt-36 md:scroll-mt-28/);
});

test("each network card saves only its own policy and credential drafts", async () => {
  const source = await readFile(pageUrl, "utf8");

  for (const scope of ["privacy", "model", "retrieval", "research"]) {
    assert.match(source, new RegExp(`saveControls\\("${scope}"\\)`));
  }
  assert.match(source, /POLICY_FIELDS_BY_SCOPE[\s\S]*privacy: \["strict_offline"\]/);
  assert.match(source, /retrieval: \["allow_conversation_embedding"\]/);
  assert.match(source, /research: \["allow_web_research", "allow_deep_research", "allow_ddg_fallback"\]/);
  assert.match(source, /keyDrafts\[scope\]/);
  assert.match(source, /clears\[scope\]/);
  assert.match(source, /setKeyDrafts\(\(current\) => \(\{ \.\.\.current, \[scope\]: \{\} \}\)\)/);
  assert.doesNotMatch(source, /onClick=\{\(\) => void save\(\)\}/);
});

test("capacity inputs stay inside the model card and only appear for custom capacity", async () => {
  const source = await readFile(pageUrl, "utf8");
  const modelStart = source.indexOf('<section id="settings-model"');
  const modelEnd = source.indexOf("</section>", modelStart);
  const modelCard = source.slice(modelStart, modelEnd);

  assert.match(modelCard, /showCustomCapacity &&/);
  assert.match(modelCard, /自定义模型容量/);
  assert.match(modelCard, /<ModelCapacityFields/);
  assert.doesNotMatch(source, /<h2[^>]*>型号容量<\/h2>/);
});

test("the fixed embedding model exposes conversation retrieval without fake model choices", async () => {
  const source = await readFile(pageUrl, "utf8");

  assert.match(source, /text-embedding-3-small/);
  assert.match(source, /可选的增强功能，用于加强求职助手的搜索能力等/);
  assert.match(source, /使用向量模型增强搜索/);
  assert.match(source, /id="allow-conversation-embedding"/);
  assert.match(source, /vectorEnabled &&/);
  assert.match(source, /llmUsesOpenAIKey/);
  assert.match(source, /\/api\/settings\/conversation-history\/clear/);
  assert.match(source, /删除全部历史对话/);
});

test("service credentials stay inline and stored unused keys remain clearable", async () => {
  const source = await readFile(pageUrl, "utf8");

  assert.match(source, /policyDraft\.allow_web_research && \(/);
  assert.match(source, /Tavily API Key（TAVILY_API_KEY）/);
  assert.match(source, /Brave Search API Key（BRAVE_API_KEY）/);
  assert.match(source, /SearXNG 实例地址（SEARXNG_BASE_URL）/);
  assert.match(source, /!policyDraft\.allow_web_research && SEARCH_CREDENTIAL_NAMES\.map/);
  assert.match(source, /!vectorEnabled && !llmUsesOpenAIKey && state\.keys\.OPENAI_API_KEY/);
  assert.match(source, /保存时将清除/);
});

test("switching LLM providers drops only hidden model credential drafts", async () => {
  const source = await readFile(pageUrl, "utf8");

  assert.match(source, /const changeModelChoice = \(nextChoice: string\)/);
  assert.match(source, /new Set\(nextProvider\?\.key_vars \?\? \[\]\)/);
  assert.match(
    source,
    /model: retainVisibleCredentialChanges\(current\.model, visibleKeyNames\)/,
  );
  assert.match(source, /changeModelChoice\(e\.target\.value\)/);
});

test("switching LLM providers cannot retain an invisible pending key deletion", async () => {
  const source = await readFile(pageUrl, "utf8");

  assert.match(
    source,
    /setClears\(\(current\) => \(\{[\s\S]*model: retainVisibleCredentialChanges\(current\.model, visibleKeyNames\)/,
  );
});

test("stored unused credential controls honor unavailable credential storage", async () => {
  const source = await readFile(pageUrl, "utf8");

  assert.match(source, /credentialStorageUnavailable: boolean/);
  assert.match(source, /系统凭据存储当前不可用，暂时不能清除/);
  assert.equal(
    source.match(/disabled=\{busy \|\| !state\.credential_storage\.available\}/g)?.length,
    2,
  );
  assert.equal(
    source.match(/aria-label=\{`\$\{pendingClear \? l\("撤销清除", "Undo removal"\) : l\("清除", "Remove"\)\}: \$\{label\}`\}/g)?.length,
    2,
  );
});

test("model privacy notices use neutral styling and omit duplicate provider capacity metadata", async () => {
  const source = await readFile(pageUrl, "utf8");

  assert.doesNotMatch(source, /容量来自当前 agentmaker 厂商默认型号元数据/);
  assert.match(
    source,
    /selected\.local && !strictModelBlocked \? "bg-ok-soft text-ok" : "bg-panel-2 text-ink-2"/,
  );
});

test("third-party copy avoids endorsements and volatile quota promises", async () => {
  const source = await readFile(pageUrl, "utf8");

  assert.match(source, /不表示赞助、认可或合作/);
  assert.match(source, /价格、配额、隐私政策和服务条款/);
  assert.doesNotMatch(source, /免费档约/);
});

test("requested settings copy is exact and removed copy stays absent", async () => {
  const source = await readFile(pageUrl, "utf8");

  assert.match(source, /一键暂停云端模型、向量模型和联网调研等外部服务/);
  assert.match(source, /当前未暂停；是否联网仍取决于下方各项设置/);
  assert.match(source, /使用云端模型时，完成任务所需的文字，包括简历全文等内容，会发送给所选模型的服务商/);
  assert.match(source, /服务商。"[\s\S]*?<br \/>[\s\S]*?原始文件和 CareerDesk 数据库仍留在本机/);
  assert.match(source, /如需适配其他向量模型，欢迎通过 GitHub Issue 提交需求/);
  assert.match(source, /对话和生成的检索索引均保存在本机/);
  assert.doesNotMatch(source, />\s*对话和检索索引保存在本机。\s*<\/p>/);
  assert.match(source, /当前已开启服务：/);
  assert.match(source, /搜集到的信息越全面。"[\s\S]*?\{policyDraft\.allow_web_research && \(\s*<>\s*<br \/>/);
  assert.match(source, /placeholder=\{l\("输入搜索引擎 ID（cx）"/);
  assert.match(source, /无需单独配置 API Key/);
  assert.match(source, /DuckDuckGo 及社区接口的当前说明为准/);
  assert.match(source, /Google Programmable Search[\s\S]*费用、配额和数据规则请以其当前页面为准/);
  assert.match(source, /SearXNG[\s\S]*费用、配额、隐私和数据规则请以实例运营方的当前说明为准/);
  assert.match(source, /基于已配置的搜索服务。结果更全面，也会消耗更多配额/);
  assert.match(source, /若直接下载安装包并使用，你添加的 API Key 只保存在这台电脑官方的/);
  assert.match(source, /保存的API key以继续使用配置好的服务；模型与联网设置保存在本机配置文件中。/);
  assert.match(source, /不会写入业务数据库。"[\s\S]*?<br \/>[\s\S]*?第三方服务及名称/);
  assert.doesNotMatch(source, /开启后不再调用外部服务/);
  assert.doesNotMatch(source, /本地模型在这台电脑上处理；云端模型/);
  assert.doesNotMatch(source, /调用该厂商模型时使用这项凭据/);
  assert.doesNotMatch(source, /仅在向量模型已开启且联网未暂停时使用/);
  assert.doesNotMatch(source, /生成或刷新调研时，读取搜索结果中的公开网页/);
  assert.doesNotMatch(source, /已彻底删除 .* 条历史对话及其检索索引/);
});

test("requested preference, storage, appearance, and feature copy stays exact", async () => {
  const [preferences, storage, theme, grill] = await Promise.all([
    readFile(preferencesUrl, "utf8"),
    readFile(storageUrl, "utf8"),
    readFile(themeUrl, "utf8"),
    readFile(grillUrl, "utf8"),
  ]);

  assert.match(preferences, /管理求职助手记住的关于您的求职方向、城市和薪资等长期偏好/);
  assert.match(preferences, /点击每项右侧的“手动编辑”即可修改/);
  assert.match(preferences, /l\("手动编辑", "Edit manually"\)/);
  assert.doesNotMatch(preferences, /如果另一标签页先改了同一项/);
  assert.match(storage, /所有业务数据、配置和日志仅保存在你的这台电脑上/);
  assert.match(storage, /<br \/>/);
  assert.match(storage, /下载安装更新后的版本后会自动识别原有数据/);
  assert.match(storage, /不建议放进网盘同步目录/);
  assert.doesNotMatch(theme, /选择界面配色。更改立即生效/);
  assert.match(grill, /l\("实验版", "Experimental"\)/);
  assert.doesNotMatch(grill, />实验中<\/span>/);
});
