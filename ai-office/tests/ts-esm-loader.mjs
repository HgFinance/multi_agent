// Minimal ESM resolver hook so `node --test` can import extensionless relative
// TypeScript specifiers (e.g. `import { BFF } from "./readModel"`) the same way
// the project's bundler (vinext/vite) resolves them. Node's native ESM resolver
// requires an explicit extension; this hook retries with `.ts`/`.tsx` before
// giving up, so raw-source unit tests can run without a build step.
//
// Used only by `node --test --experimental-strip-types --import ./tests/ts-esm-loader.mjs`.
// Not used by the Next.js build/runtime, which resolves TS imports itself.

import { existsSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";

const CANDIDATE_EXTENSIONS = [".ts", ".tsx", ".mts"];

export async function resolve(specifier, context, nextResolve) {
  try {
    return await nextResolve(specifier, context);
  } catch (error) {
    if (error?.code !== "ERR_MODULE_NOT_FOUND" || !specifier.startsWith(".")) {
      throw error;
    }
    const base = new URL(specifier, context.parentURL);
    for (const ext of CANDIDATE_EXTENSIONS) {
      const candidate = new URL(base.pathname + ext, base);
      if (existsSync(fileURLToPath(candidate))) {
        return nextResolve(pathToFileURL(fileURLToPath(candidate)).href, context);
      }
    }
    throw error;
  }
}
