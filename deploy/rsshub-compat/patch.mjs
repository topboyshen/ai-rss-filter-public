import { createHash } from 'node:crypto';
import { lstatSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const BASE_IMAGE = 'diygod/rsshub@sha256:e7cd1bd8838b91f89c2ed56b0889f3754edc6aa1f1f5dac002e2e97e1079bd54';
export const BASE_REVISION = 'f23ada8a636fbbb5f92d8e91558cf4e8ffaca66a';
export const MODULES = Object.freeze({
  'wallstreetcn-news-BKjmaYn2.mjs': '80539407f40be2ed181c6e5853a7c3e26247a1ecedb372d5b0e7ab0ccea0fee7',
  'cls-telegraph-DFOzEo9d.mjs': '06d7f8eed8704604c0848084a1f595ce1dc0c2926c5444b6f1fc38eefcd604de',
});
export const DEPENDENCIES = Object.freeze({
  'not-found-CaYyolkS.mjs': 'd3c82ed3470436d32741c63525a505b0ae1dcf7e2958daa9d5b9877a76ca07c2',
});

export function sha256(value) {
  return createHash('sha256').update(value).digest('hex');
}

export function replaceExactlyOnce(source, before, after) {
  if (!before || source.split(before).length !== 2) {
    throw new Error('Compatibility patch requires exactly one matching fragment');
  }
  return source.replace(before, after);
}

export function patchModule(name, bytes) {
  if (!Object.hasOwn(MODULES, name) || sha256(bytes) !== MODULES[name]) {
    throw new Error(`Unexpected candidate module bytes: ${name}`);
  }
  let source = Buffer.from(bytes).toString('utf8');
  if (name === 'wallstreetcn-news-BKjmaYn2.mjs') {
    source = replaceExactlyOnce(source,
      'import{t as e}from"./cache-CtYX8KBG.mjs";',
      'import{t as AICompatNotFoundError}from"./not-found-CaYyolkS.mjs";import{t as e}from"./cache-CtYX8KBG.mjs";');
    source = replaceExactlyOnce(source,
      'async function c(t){let a=t.req.param(`category`)??`global`,c=',
      'async function c(t){let a=t.req.param(`category`)??`global`;if(!Object.hasOwn(i,a))throw new AICompatNotFoundError(`Unsupported Wallstreetcn news category. Use a documented category; china is retired.`);let c=');
    source = replaceExactlyOnce(source,
      'channel=${a}&accept=article',
      'channel=${a===`global`?`global-channel`:a}&accept=article');
  } else {
    source = replaceExactlyOnce(source,
      'link:e.shareurl||`https://www.cls.cn/detail/${e.id}`,',
      'link:s===void 0?e.shareurl:e.shareurl||`https://www.cls.cn/detail/${e.id}`,');
  }
  return Buffer.from(source, 'utf8');
}

function checkedRead(path) {
  if (!lstatSync(path).isFile()) throw new Error(`Expected regular file: ${path}`);
  return readFileSync(path);
}

export function contextDigest(root) {
  const files = [];
  function walk(relative) {
    for (const name of readdirSync(join(root, relative)).sort()) {
      const path = relative ? `${relative}/${name}` : name;
      const stat = lstatSync(join(root, path));
      if (stat.isDirectory()) walk(path);
      else if (stat.isFile()) files.push(path);
      else throw new Error(`Non-regular build context entry: ${path}`);
    }
  }
  walk('');
  if (!files.length) throw new Error('Empty compatibility build context');
  const hash = createHash('sha256');
  for (const path of files.sort()) {
    hash.update(`${path}\0${sha256(checkedRead(join(root, path)))}\n`);
  }
  return hash.digest('hex');
}

export function patchTree(dist, manifestPath, contextSha256) {
  if (!/^[a-f0-9]{64}$/.test(contextSha256)) throw new Error('Invalid context SHA256');
  const dependencies = Object.entries(DEPENDENCIES).map(([name, expected]) => {
    if (sha256(checkedRead(join(dist, name))) !== expected) {
      throw new Error(`Unexpected dependency bytes: ${name}`);
    }
    return { path: `/app/dist/${name}`, sha256: expected };
  });
  // Validate every input before modifying any module. An interrupted build is never published.
  const replacements = Object.entries(MODULES).map(([name, before]) => {
    const bytes = patchModule(name, checkedRead(join(dist, name)));
    return { name, bytes, before, after: sha256(bytes) };
  });
  for (const item of replacements) {
    writeFileSync(join(dist, item.name), item.bytes);
    if (sha256(checkedRead(join(dist, item.name))) !== item.after) {
      throw new Error(`Patched module verification failed: ${item.name}`);
    }
  }
  const manifest = {
    schema_version: 1,
    compat_version: 'v1',
    base_image: BASE_IMAGE,
    base_revision: BASE_REVISION,
    context_sha256: contextSha256,
    modules: replacements.map((item) => ({
      path: `/app/dist/${item.name}`,
      before_sha256: item.before,
      after_sha256: item.after,
    })),
    dependencies,
  };
  writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, { flag: 'wx' });
  return manifest;
}

function main(args) {
  if (args.length === 2 && args[0] === '--context-digest') {
    console.log(contextDigest(resolve(args[1])));
    return;
  }
  if (args.length !== 8 || args[0] !== '--dist' || args[2] !== '--manifest'
      || args[4] !== '--context-root' || args[6] !== '--context-sha256') {
    throw new Error('Expected --dist DIR --manifest FILE --context-root DIR --context-sha256 SHA256');
  }
  const root = resolve(args[5]);
  if (dirname(fileURLToPath(import.meta.url)) !== root || contextDigest(root) !== args[7]) {
    throw new Error('Build context SHA256 mismatch');
  }
  console.log(JSON.stringify(patchTree(resolve(args[1]), resolve(args[3]), args[7])));
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try { main(process.argv.slice(2)); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
