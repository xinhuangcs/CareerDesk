import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";

export const sourceRootPath = fileURLToPath(new URL("../", import.meta.url));

export function isWithin(candidate, directory) {
  const relative = path.relative(directory, candidate);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

export function moduleStem(filename) {
  return path.normalize(filename.replace(/\.(?:[cm]?[jt]sx?)$/, ""));
}

export function resolveLocalModule(importer, specifier) {
  if (!specifier.startsWith(".")) return null;
  return moduleStem(path.resolve(path.dirname(importer), specifier));
}

async function productionTypeScriptFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const filename = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      files.push(...await productionTypeScriptFiles(filename));
    } else if (/\.tsx?$/.test(entry.name) && !/\.test\./.test(entry.name)) {
      files.push(filename);
    }
  }
  return files;
}

function bindingNames(name) {
  if (ts.isIdentifier(name)) return [name.text];
  return name.elements.flatMap((element) => (
    ts.isOmittedExpression(element) ? [] : bindingNames(element.name)
  ));
}

function hasExportModifier(statement) {
  return Boolean(
    ts.canHaveModifiers(statement)
      && ts.getModifiers(statement)?.some(
        (modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword,
      ),
  );
}

export function exportedNames(sourceFile) {
  const names = [];
  for (const statement of sourceFile.statements) {
    if (hasExportModifier(statement)) {
      if (ts.isVariableStatement(statement)) {
        for (const declaration of statement.declarationList.declarations) {
          names.push(...bindingNames(declaration.name));
        }
      } else if ("name" in statement && statement.name && ts.isIdentifier(statement.name)) {
        names.push(statement.name.text);
      }
    }
    if (ts.isExportDeclaration(statement)
        && statement.exportClause
        && ts.isNamedExports(statement.exportClause)) {
      names.push(...statement.exportClause.elements.map((element) => element.name.text));
    }
  }
  return names;
}

export function moduleReferences(sourceFile) {
  const references = [];
  function visit(node) {
    if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node))
        && node.moduleSpecifier
        && ts.isStringLiteral(node.moduleSpecifier)) {
      references.push({ node, specifier: node.moduleSpecifier.text });
    } else if (ts.isCallExpression(node)
        && node.expression.kind === ts.SyntaxKind.ImportKeyword
        && node.arguments.length === 1
        && ts.isStringLiteral(node.arguments[0])) {
      references.push({ node, specifier: node.arguments[0].text });
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return references;
}

export function descendants(root, predicate) {
  const matches = [];
  function visit(node) {
    if (predicate(node)) matches.push(node);
    ts.forEachChild(node, visit);
  }
  visit(root);
  return matches;
}

function aliasSource(initializer) {
  let expression = initializer;
  while (ts.isParenthesizedExpression(expression)
      || ts.isAsExpression(expression)
      || ts.isTypeAssertionExpression(expression)
      || ts.isNonNullExpression(expression)
      || ts.isSatisfiesExpression(expression)) {
    expression = expression.expression;
  }
  if (ts.isIdentifier(expression)) return { binding: expression.text, member: false };
  if ((ts.isPropertyAccessExpression(expression) || ts.isElementAccessExpression(expression))
      && ts.isIdentifier(expression.expression)) {
    return { binding: expression.expression.text, member: true };
  }
  return null;
}

function typeAliasSourceBinding(type) {
  let candidate = type;
  while (ts.isParenthesizedTypeNode(candidate)) candidate = candidate.type;
  return ts.isTypeReferenceNode(candidate) && ts.isIdentifier(candidate.typeName)
    ? candidate.typeName.text
    : null;
}

function forwardsOwnerValue(expression, ownerBindings, namespaceBindings) {
  const source = aliasSource(expression);
  if (source !== null) {
    return source.member
      ? namespaceBindings.has(source.binding)
      : ownerBindings.has(source.binding);
  }
  if (ts.isObjectLiteralExpression(expression)) {
    return expression.properties.some((property) => {
      if (ts.isShorthandPropertyAssignment(property)) {
        return ownerBindings.has(property.name.text);
      }
      if (ts.isPropertyAssignment(property) || ts.isSpreadAssignment(property)) {
        return forwardsOwnerValue(property.expression, ownerBindings, namespaceBindings);
      }
      return false;
    });
  }
  if (ts.isArrayLiteralExpression(expression)) {
    return expression.elements.some(
      (element) => !ts.isOmittedExpression(element)
        && forwardsOwnerValue(element, ownerBindings, namespaceBindings),
    );
  }
  return false;
}

export function importsFrom(sourceFile, importer, owner) {
  return sourceFile.statements.filter(
    (statement) => ts.isImportDeclaration(statement)
      && ts.isStringLiteral(statement.moduleSpecifier)
      && resolveLocalModule(importer, statement.moduleSpecifier.text) === moduleStem(owner),
  );
}

export function runtimeDefinitionNames(sourceFile) {
  return descendants(
    sourceFile,
    (node) => ts.isVariableDeclaration(node)
      || ts.isClassDeclaration(node)
      || ts.isFunctionDeclaration(node),
  ).flatMap((node) => {
    if (ts.isVariableDeclaration(node)) return bindingNames(node.name);
    return node.name && ts.isIdentifier(node.name) ? [node.name.text] : [];
  });
}

export function typeDefinitionNames(sourceFile) {
  return descendants(
    sourceFile,
    (node) => ts.isTypeAliasDeclaration(node) || ts.isInterfaceDeclaration(node),
  ).map((node) => node.name.text);
}

export async function sourceRecords() {
  const files = await productionTypeScriptFiles(sourceRootPath);
  return Promise.all(files.map(async (filename) => {
    const source = await readFile(filename, "utf8");
    return {
      filename,
      sourceFile: ts.createSourceFile(
        filename,
        source,
        ts.ScriptTarget.Latest,
        true,
        filename.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
      ),
    };
  }));
}

export function recordFor(records, filename) {
  const record = records.find((candidate) => candidate.filename === filename);
  assert.ok(record, `missing source record for ${filename}`);
  return record;
}

export function assertNoOwnerForwarding(records, ownerPaths, ownerLabel) {
  const ownerStems = new Set(ownerPaths.map(moduleStem));
  for (const { filename, sourceFile } of records) {
    const ownerImportBindings = new Set();
    const ownerNamespaceBindings = new Set();
    for (const statement of sourceFile.statements) {
      if (!ts.isImportDeclaration(statement)
          || !ts.isStringLiteral(statement.moduleSpecifier)
          || !ownerStems.has(resolveLocalModule(filename, statement.moduleSpecifier.text))) {
        continue;
      }
      const clause = statement.importClause;
      if (clause?.name) ownerImportBindings.add(clause.name.text);
      if (clause?.namedBindings && ts.isNamespaceImport(clause.namedBindings)) {
        ownerImportBindings.add(clause.namedBindings.name.text);
        ownerNamespaceBindings.add(clause.namedBindings.name.text);
      } else if (clause?.namedBindings && ts.isNamedImports(clause.namedBindings)) {
        for (const element of clause.namedBindings.elements) {
          ownerImportBindings.add(element.name.text);
        }
      }
    }

    const forwardedOwnerBindings = new Set(ownerImportBindings);
    const forwardedNamespaceBindings = new Set(ownerNamespaceBindings);
    let foundAlias = true;
    while (foundAlias) {
      foundAlias = false;
      for (const statement of sourceFile.statements.filter(ts.isVariableStatement)) {
        for (const declaration of statement.declarationList.declarations) {
          if (!ts.isIdentifier(declaration.name) || !declaration.initializer) continue;
          const source = aliasSource(declaration.initializer);
          if (source === null
              || (source.member
                ? !forwardedNamespaceBindings.has(source.binding)
                : !forwardedOwnerBindings.has(source.binding))
              || forwardedOwnerBindings.has(declaration.name.text)) {
            continue;
          }
          forwardedOwnerBindings.add(declaration.name.text);
          if (!source.member && forwardedNamespaceBindings.has(source.binding)) {
            forwardedNamespaceBindings.add(declaration.name.text);
          }
          foundAlias = true;
        }
      }
      for (const statement of sourceFile.statements.filter(ts.isTypeAliasDeclaration)) {
        const sourceBinding = typeAliasSourceBinding(statement.type);
        if (sourceBinding === null
            || !forwardedOwnerBindings.has(sourceBinding)
            || forwardedOwnerBindings.has(statement.name.text)) {
          continue;
        }
        forwardedOwnerBindings.add(statement.name.text);
        foundAlias = true;
      }
    }

    for (const statement of sourceFile.statements.filter(ts.isExportDeclaration)) {
      if (statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)) {
        assert.ok(
          !ownerStems.has(resolveLocalModule(filename, statement.moduleSpecifier.text)),
          `${path.relative(sourceRootPath, filename)} must not forward ${ownerLabel}`,
        );
      }
      if (statement.exportClause && ts.isNamedExports(statement.exportClause)) {
        for (const element of statement.exportClause.elements) {
          const localName = element.propertyName?.text ?? element.name.text;
          assert.ok(
            !forwardedOwnerBindings.has(localName),
            `${path.relative(sourceRootPath, filename)} must not alias ${ownerLabel}`,
          );
        }
      }
    }
    for (const statement of sourceFile.statements.filter(ts.isVariableStatement)) {
      if (!hasExportModifier(statement)) continue;
      for (const declaration of statement.declarationList.declarations) {
        for (const name of bindingNames(declaration.name)) {
          assert.ok(
            !forwardedOwnerBindings.has(name),
            `${path.relative(sourceRootPath, filename)} must not export ${ownerLabel} under an alias`,
          );
        }
      }
    }
    for (const statement of sourceFile.statements.filter(ts.isTypeAliasDeclaration)) {
      if (!hasExportModifier(statement)) continue;
      assert.ok(
        !forwardedOwnerBindings.has(statement.name.text),
        `${path.relative(sourceRootPath, filename)} must not export ${ownerLabel} as a type alias`,
      );
    }
    for (const statement of sourceFile.statements.filter(ts.isExportAssignment)) {
      assert.equal(
        forwardsOwnerValue(
          statement.expression,
          forwardedOwnerBindings,
          forwardedNamespaceBindings,
        ),
        false,
        `${path.relative(sourceRootPath, filename)} must not default-forward ${ownerLabel}`,
      );
    }
  }
}
