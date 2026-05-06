"use strict";
/**
 * Optional richer TS/JS relative-import edges for architecture_map.py.
 * Enable with GROUNDED_REVIEW_ARCH_MAP_TS_MORPH=true after: npm install (in this directory).
 */
const path = require("path");
let Project;
try {
  ({ Project } = require("ts-morph"));
} catch (e) {
  console.log("[]");
  process.exit(0);
}

const scanRoot = path.resolve(process.argv[2] || ".");
const rawList = process.argv[3] || "[]";
let files = [];
try {
  files = JSON.parse(rawList);
} catch {
  console.log("[]");
  process.exit(0);
}

const project = new Project({
  compilerOptions: { allowJs: true },
  skipAddingFilesFromTsConfig: true,
});

for (const f of files) {
  try {
    project.addSourceFileAtPath(f);
  } catch (_) {}
}

const edges = [];
for (const sf of project.getSourceFiles()) {
  const fromAbs = sf.getFilePath();
  let fromRel = path.relative(scanRoot, fromAbs).split(path.sep).join("/");
  fromRel = fromRel.replace(/\.(tsx?|jsx?|mjs|cjs)$/, "");

  for (const d of sf.getImportDeclarations()) {
    const spec = d.getModuleSpecifierValue();
    if (!spec || !(spec.startsWith("./") || spec.startsWith("../"))) continue;
    const targetAbs = path.resolve(path.dirname(fromAbs), spec);
    let toRel = path.relative(scanRoot, targetAbs).split(path.sep).join("/");
    if (toRel.startsWith("..")) continue;
    toRel = toRel.replace(/\.(tsx?|jsx?|mjs|cjs)$/, "");
    edges.push({ from: fromRel, to: toRel });
  }
}

console.log(JSON.stringify(edges));
