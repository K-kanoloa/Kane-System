// Diagnostic only: rerun the unchanged behavioral assertions with a longer TEST
// deadline on the slow vfs Docker host. Does not change a production timeout,
// native Agent code, the original test file, or the original acceptance result.
import { readFile, writeFile, mkdir, unlink } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),"..");
const original=path.join(root,"packages/kanaloa-tools/test/tools.test.mjs");
const source=await readFile(original,"utf8");
const hash=value=>createHash("sha256").update(value).digest("hex");
const directory=path.join(root,".kane");await mkdir(directory,{recursive:true,mode:0o700});
const generated=path.join(directory,`timeout-diagnostic-${process.pid}.test.mjs`);
const index=pathToFileURL(path.join(root,"packages/kanaloa-tools/index.mjs")).href;
const packageRoot=pathToFileURL(path.join(root,"packages/kanaloa-tools/")).href;
const adapted=source.replace("from '../index.mjs'",`from '${index}'`)
  .replace("new URL('../', import.meta.url)",`new URL('${packageRoot}')`)
  .replace("new URL('../index.mjs', import.meta.url)",`new URL('${index}')`)
  .replaceAll("timeout: 30_000","timeout: 120_000");
try {
  await writeFile(generated,adapted,{mode:0o600});
  const child=spawn(process.execPath,["--test","--test-name-pattern=real IPython persists per native session",generated],{cwd:root,env:{...process.env,KANALOA_TEST_CONTAINER:"1"},stdio:"inherit"});
  const exit=await new Promise(resolve=>child.once("exit",resolve));
  process.exitCode=exit??1;
} finally {
  await unlink(generated);
  if(hash(await readFile(original,"utf8"))!==hash(source))throw new Error("original test changed during diagnostic");
  console.log("Diagnostic deadline 120s only; the original 30s gate remains a recorded timeout.");
}
