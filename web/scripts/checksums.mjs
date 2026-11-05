// SHA-256 of every file the build serves, in dist/SHA256SUMS.txt: what anyone
// can compare with the site (curl https://faucet.zoka.network/<file> | sha256sum)
// and with a build of their own from the same commit.
import { createHash } from "node:crypto";
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

const dist = new URL("../dist/", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const out = join(dist, "SHA256SUMS.txt");

function files(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}

const lines = files(dist)
  .filter((path) => path !== out)
  .map((path) => {
    const hash = createHash("sha256").update(readFileSync(path)).digest("hex");
    return `${hash}  ${relative(dist, path).split(sep).join("/")}`;
  })
  .sort((a, b) => a.slice(66).localeCompare(b.slice(66)));

writeFileSync(out, `${lines.join("\n")}\n`);
console.log(`SHA256SUMS.txt: ${lines.length} files`);
