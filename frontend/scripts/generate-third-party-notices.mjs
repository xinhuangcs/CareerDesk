import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, readFileSync, realpathSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { basename, dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const LICENSE_FILE = /^(?:licen[cs]e|copying|notice|copyright|authors)(?:$|[-_.])/i;
const COVERAGE_PREFIXES = [
  ["@esbuild/", "esbuild"],
  ["@rollup/rollup-", "rollup"],
  ["@tailwindcss/oxide-", "@tailwindcss/oxide"],
  ["lightningcss-", "lightningcss"],
];
const MAX_NOTICE_BYTES = 5 * 1024 * 1024;
const MAX_TOTAL_BYTES = 100 * 1024 * 1024;
const MAX_PACKAGES = 20_000;
const MAX_PACKAGE_NAME_CHARS = 214;
const MAX_VERSION_CHARS = 200;
const MAX_LICENSE_CHARS = 1_024;
const MAX_AUTHOR_CHARS = 1_024;
const MAX_URL_CHARS = 2_048;

function packageDirectories(nodeModules) {
  const found = [];
  const seenNodeModules = new Set();

  function visitModules(directory) {
    const canonical = realpathSync(directory);
    if (seenNodeModules.has(canonical)) return;
    seenNodeModules.add(canonical);

    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      if (!entry.isDirectory() || entry.name === ".bin") continue;
      const candidate = join(directory, entry.name);
      if (entry.name.startsWith("@")) {
        for (const scoped of readdirSync(candidate, { withFileTypes: true })) {
          if (scoped.isDirectory()) visitPackage(join(candidate, scoped.name));
        }
      } else {
        visitPackage(candidate);
      }
    }
  }

  function visitPackage(directory) {
    const manifest = join(directory, "package.json");
    try {
      if (!statSync(manifest).isFile()) return;
    } catch {
      return;
    }
    found.push(directory);
    const nested = join(directory, "node_modules");
    try {
      if (statSync(nested).isDirectory()) visitModules(nested);
    } catch {
      // A hoisted package normally has no nested node_modules directory.
    }
  }

  visitModules(nodeModules);
  return found;
}

function safeSegment(value) {
  return value.replace(/^@/, "").replaceAll("/", "__").replace(/[^A-Za-z0-9._-]/g, "_");
}

function authorLabel(manifest) {
  if (typeof manifest.author === "string") return manifest.author.trim();
  if (manifest.author && typeof manifest.author === "object") {
    const name = typeof manifest.author.name === "string" ? manifest.author.name.trim() : "";
    const url = typeof manifest.author.url === "string" ? manifest.author.url.trim() : "";
    return name && url ? `${name} (${url})` : name || url;
  }
  if (!Array.isArray(manifest.contributors)) return "";
  return manifest.contributors
    .map((item) => typeof item === "string" ? item.trim() : item?.name?.trim?.() ?? "")
    .filter(Boolean)
    .join(", ");
}

function repositoryUrl(repository) {
  const raw = typeof repository === "string"
    ? repository.trim()
    : typeof repository?.url === "string" ? repository.url.trim() : "";
  if (/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(raw)) {
    return `https://github.com/${raw}`;
  }
  return raw.replace(/^git\+/, "").replace(/\.git$/, "");
}

function directLicenseFiles(directory) {
  return readdirSync(directory, { withFileTypes: true })
    .filter((entry) => entry.isFile() && LICENSE_FILE.test(entry.name))
    .map((entry) => entry.name)
    .sort();
}

export function generateThirdPartyNotices({ nodeModules, destination }) {
  const modulesRoot = resolve(nodeModules);
  const outputRoot = resolve(destination);
  try {
    statSync(outputRoot);
    throw new Error(`third-party notice output must not already exist: ${outputRoot}`);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }

  const packages = new Map();
  for (const directory of packageDirectories(modulesRoot)) {
    const manifest = JSON.parse(readFileSync(join(directory, "package.json"), "utf8"));
    if (typeof manifest.name !== "string" || !manifest.name
        || typeof manifest.version !== "string" || !manifest.version
        || typeof manifest.license !== "string" || !manifest.license
        || manifest.name.length > MAX_PACKAGE_NAME_CHARS
        || manifest.version.length > MAX_VERSION_CHARS
        || manifest.license.length > MAX_LICENSE_CHARS) {
      throw new Error(`package metadata is incomplete: ${relative(modulesRoot, directory)}`);
    }
    const homepage = typeof manifest.homepage === "string" ? manifest.homepage : "";
    const repository = repositoryUrl(manifest.repository);
    const author = authorLabel(manifest);
    if (homepage.length > MAX_URL_CHARS || repository.length > MAX_URL_CHARS
        || author.length > MAX_AUTHOR_CHARS) {
      throw new Error(`package URL metadata is too long: ${manifest.name}@${manifest.version}`);
    }
    if (/CC-BY/i.test(manifest.license) && (!author || !(homepage || repository))) {
      throw new Error(`attribution metadata is incomplete: ${manifest.name}@${manifest.version}`);
    }
    if (/MPL-/i.test(manifest.license) && !(homepage || repository)) {
      throw new Error(`MPL source location is missing: ${manifest.name}@${manifest.version}`);
    }
    const key = `${manifest.name}@${manifest.version}`;
    if (!packages.has(key)) {
      if (packages.size >= MAX_PACKAGES) {
        throw new Error(`package count exceeds the ${MAX_PACKAGES} safety limit`);
      }
      packages.set(key, {
        name: manifest.name,
        version: manifest.version,
        license: manifest.license,
        author,
        homepage,
        repository,
        licenseFiles: [],
      });
    }
    const item = packages.get(key);
    if (item.license !== manifest.license
        || item.author !== author
        || item.homepage !== homepage
        || item.repository !== repository) {
      throw new Error(`conflicting package metadata: ${key}`);
    }
    item.licenseFiles.push(...directLicenseFiles(directory).map((file) => ({ directory, file })));
  }

  mkdirSync(outputRoot, { recursive: true, mode: 0o700 });
  const rows = [];
  let totalBytes = 0;
  const outputOwners = new Map();
  for (const item of [...packages.values()].sort((left, right) =>
    left.name.localeCompare(right.name) || left.version.localeCompare(right.version))) {
    const packageKey = `${item.name}@${item.version}`;
    const outputKey = `${safeSegment(item.name)}/${safeSegment(item.version)}`;
    if (outputKey.startsWith("/") || outputKey.endsWith("/")) {
      throw new Error(`package metadata cannot form a safe notice path: ${packageKey}`);
    }
    const caseInsensitiveOutputKey = outputKey.toLowerCase();
    const existingOwner = outputOwners.get(caseInsensitiveOutputKey);
    if (existingOwner && existingOwner !== packageKey) {
      throw new Error(`package notice path collision: ${existingOwner} and ${packageKey}`);
    }
    outputOwners.set(caseInsensitiveOutputKey, packageKey);

    let coveredBy = "";
    if (item.licenseFiles.length === 0) {
      const ownerName = COVERAGE_PREFIXES.find(([prefix]) => item.name.startsWith(prefix))?.[1] ?? "";
      const owner = packages.get(`${ownerName}@${item.version}`);
      if (!owner || owner.licenseFiles.length === 0 || owner.license !== item.license) {
        throw new Error(`package does not provide a license file: ${item.name}@${item.version}`);
      }
      coveredBy = `${owner.name}@${owner.version}`;
    }

    const packageRoot = join(outputRoot, safeSegment(item.name), safeSegment(item.version));
    const copied = [];
    const usedOutputNames = new Set();
    const seenContent = new Set();
    for (const { directory, file } of item.licenseFiles) {
      const source = resolve(directory, file);
      if (!source.startsWith(`${resolve(directory)}${sep}`) || statSync(source).size > MAX_NOTICE_BYTES) {
        throw new Error(`unsafe license file: ${source}`);
      }
      const content = readFileSync(source);
      const fingerprint = createHash("sha256").update(content).digest("hex");
      if (seenContent.has(fingerprint)) continue;
      seenContent.add(fingerprint);
      totalBytes += content.byteLength;
      if (totalBytes > MAX_TOTAL_BYTES) {
        throw new Error("third-party license notices exceed the 100 MiB safety limit");
      }
      mkdirSync(packageRoot, { recursive: true, mode: 0o700 });
      let outputName = basename(file);
      let suffix = 1;
      while (usedOutputNames.has(outputName.toLowerCase())) {
        outputName = `${basename(file)}.${suffix}`;
        suffix += 1;
      }
      usedOutputNames.add(outputName.toLowerCase());
      copyFileSync(source, join(packageRoot, outputName));
      copied.push(outputName);
    }
    rows.push({
      name: item.name,
      version: item.version,
      license: item.license,
      author: item.author,
      homepage: item.homepage,
      repository: item.repository,
      license_files: copied,
      covered_by: coveredBy,
    });
  }

  const inventory = `${JSON.stringify({ schema_version: 1, packages: rows }, null, 2)}\n`;
  const attribution = rows.map((item) => [
    `${item.name}@${item.version}`,
    `  License: ${item.license}`,
    `  Author: ${item.author || "Not supplied by package metadata"}`,
    `  Source: ${item.homepage || item.repository || "Not supplied by package metadata"}`,
    `  License files: ${item.license_files.join(", ") || `covered by ${item.covered_by}`}`,
  ].join("\n")).join("\n\n") + "\n";
  const readme = "These files preserve notices and attribution supplied by the npm packages used to build CareerDesk.\n"
    + "ATTRIBUTION.txt identifies package authors, licenses, and upstream source locations.\n"
    + "A covered_by entry identifies the exact parent package version supplying the license.\n";
  if (totalBytes + Buffer.byteLength(inventory) + Buffer.byteLength(attribution)
      + Buffer.byteLength(readme) > MAX_TOTAL_BYTES) {
    throw new Error("third-party license notices exceed the 100 MiB safety limit");
  }
  writeFileSync(join(outputRoot, "index.json"), inventory, "utf8");
  writeFileSync(join(outputRoot, "ATTRIBUTION.txt"), attribution, "utf8");
  writeFileSync(join(outputRoot, "README.txt"), readme, "utf8");
  return rows;
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : "";
if (invokedPath === fileURLToPath(import.meta.url)) {
  const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
  const destination = process.argv[2]
    ? resolve(process.cwd(), process.argv[2])
    : join(frontendRoot, "dist", "legal", "node");
  const rows = generateThirdPartyNotices({
    nodeModules: join(frontendRoot, "node_modules"),
    destination,
  });
  process.stdout.write(`Bundled notices for ${rows.length} npm packages.\n`);
}
