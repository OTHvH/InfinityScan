import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const packagePath = require.resolve("brace-expansion/package.json");
const packageMetadata = JSON.parse(readFileSync(packagePath, "utf8"));

if (packageMetadata.version !== "5.0.8") {
  throw new Error(`Expected brace-expansion 5.0.8, found ${packageMetadata.version}`);
}

const commonJsPath = require.resolve("brace-expansion");
const marker = "// InfinityScan minimatch 3 CommonJS compatibility";
const source = readFileSync(commonJsPath, "utf8");

if (!source.includes("exports.expand = expand")) {
  throw new Error("brace-expansion CommonJS entry point has an unexpected export shape");
}

if (!source.includes(marker)) {
  writeFileSync(
    commonJsPath,
    `${source}\n${marker}\nmodule.exports = Object.assign(exports.expand, exports);\n`,
  );
}
