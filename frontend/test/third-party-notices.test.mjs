import assert from "node:assert/strict";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { generateThirdPartyNotices } from "../scripts/generate-third-party-notices.mjs";

function packageFixture(nodeModules, directory, manifest, withLicense = true) {
  const packageRoot = join(nodeModules, directory);
  mkdirSync(packageRoot, { recursive: true });
  writeFileSync(join(packageRoot, "package.json"), JSON.stringify(manifest));
  if (withLicense) writeFileSync(join(packageRoot, "LICENSE"), "Example license\n");
}

test("the production distribution carries a complete npm license inventory", () => {
  const indexUrl = new URL("../dist/legal/node/index.json", import.meta.url);
  const inventory = JSON.parse(readFileSync(indexUrl, "utf8"));

  assert.equal(inventory.schema_version, 1);
  assert.ok(inventory.packages.length > 50);
  const react = inventory.packages.find((item) => item.name === "react");
  assert.ok(react);
  assert.match(react.license, /MIT/i);
  const ccByPackages = inventory.packages.filter((item) => /CC-BY/i.test(item.license));
  assert.ok(ccByPackages.length > 0);
  for (const item of ccByPackages) {
    assert.ok(item.author, `${item.name} lacks attribution`);
    assert.ok(item.homepage || item.repository, `${item.name} lacks an upstream source`);
  }
  for (const item of inventory.packages.filter((entry) => /MPL-/i.test(entry.license))) {
    assert.ok(item.homepage || item.repository, `${item.name} lacks an MPL source location`);
  }
  const attribution = readFileSync(
    new URL("../dist/legal/node/ATTRIBUTION.txt", import.meta.url),
    "utf8",
  );
  for (const item of ccByPackages) {
    assert.match(attribution, new RegExp(`${item.name}@${item.version}`));
    assert.ok(attribution.includes(item.author));
  }

  const packages = new Map(
    inventory.packages.map((item) => [`${item.name}@${item.version}`, item]),
  );
  for (const item of inventory.packages) {
    assert.ok(item.name);
    assert.ok(item.version);
    assert.ok(item.license);
    assert.ok(item.license_files.length > 0 || item.covered_by, `${item.name} lacks a license notice`);
    if (item.covered_by) {
      const owner = packages.get(item.covered_by);
      assert.ok(owner, `${item.name} has an unknown coverage owner`);
      assert.equal(owner.version, item.version);
      assert.equal(owner.license, item.license);
      assert.ok(owner.license_files.length > 0);
    }
    for (const file of item.license_files) {
      const directory = item.name.replace(/^@/, "").replaceAll("/", "__").replace(/[^A-Za-z0-9._-]/g, "_");
      const version = item.version.replace(/[^A-Za-z0-9._-]/g, "_");
      assert.ok(existsSync(new URL(`../dist/legal/node/${directory}/${version}/${file}`, import.meta.url)));
    }
  }
});

test("npm notice paths reject sanitized and case-insensitive collisions", (context) => {
  const root = mkdtempSync(join(tmpdir(), "careerdesk-node-notices-"));
  context.after(() => rmSync(root, { recursive: true, force: true }));
  const nodeModules = join(root, "node_modules");
  packageFixture(nodeModules, "first", { name: "a+b", version: "1.0.0", license: "MIT" });
  packageFixture(nodeModules, "second", { name: "a=b", version: "1.0.0", license: "MIT" });

  assert.throws(
    () => generateThirdPartyNotices({ nodeModules, destination: join(root, "out") }),
    /package notice path collision/,
  );
});

test("npm platform-package coverage requires the exact parent version", (context) => {
  const root = mkdtempSync(join(tmpdir(), "careerdesk-node-notices-"));
  context.after(() => rmSync(root, { recursive: true, force: true }));
  const nodeModules = join(root, "node_modules");
  packageFixture(nodeModules, "owner", { name: "esbuild", version: "1.0.0", license: "MIT" });
  packageFixture(
    nodeModules,
    "platform",
    { name: "@esbuild/darwin-arm64", version: "2.0.0", license: "MIT" },
    false,
  );

  assert.throws(
    () => generateThirdPartyNotices({ nodeModules, destination: join(root, "out") }),
    /does not provide a license file/,
  );
});

test("attribution and source-availability licenses fail closed without metadata", (context) => {
  const root = mkdtempSync(join(tmpdir(), "careerdesk-node-attribution-"));
  context.after(() => rmSync(root, { recursive: true, force: true }));

  const ccModules = join(root, "cc", "node_modules");
  packageFixture(ccModules, "cc-data", {
    name: "cc-data",
    version: "1.0.0",
    license: "CC-BY-4.0",
    repository: "example/cc-data",
  });
  assert.throws(
    () => generateThirdPartyNotices({ nodeModules: ccModules, destination: join(root, "cc-out") }),
    /attribution metadata is incomplete/,
  );

  const mplModules = join(root, "mpl", "node_modules");
  packageFixture(mplModules, "mpl-code", {
    name: "mpl-code",
    version: "1.0.0",
    license: "MPL-2.0",
  });
  assert.throws(
    () => generateThirdPartyNotices({ nodeModules: mplModules, destination: join(root, "mpl-out") }),
    /MPL source location is missing/,
  );
});
