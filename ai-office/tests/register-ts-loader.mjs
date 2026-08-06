// Preload script (use via `node --import ./tests/register-ts-loader.mjs`) that
// registers the extensionless-TS-import resolver hook using the stable
// `node:module` register() API, per Node's recommended replacement for the
// deprecated `--experimental-loader` CLI flag.
import { register } from "node:module";
import { pathToFileURL } from "node:url";

register("./ts-esm-loader.mjs", pathToFileURL(`${import.meta.dirname}/`));
