// https://docs.expo.dev/guides/using-eslint/
const { defineConfig } = require("eslint/config");
const expoConfig = require("eslint-config-expo/flat");
const prettierConfig = require("eslint-config-prettier");

module.exports = defineConfig([
  expoConfig,
  // Turns off stylistic rules that would fight Prettier (formatting is
  // Prettier's job: `npm run format:check` / `npm run format`).
  prettierConfig,
  {
    ignores: ["dist/*", ".expo/*"],
  },
]);
