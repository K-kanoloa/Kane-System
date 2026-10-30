import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { verifyAndPatchProjectDshAcp, writePatchedFile } from './patch-dsh-acp.mjs';

function workspace(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'kanaloa-patch-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

test('invalid patch leaves the installed runtime and backup unchanged', t => {
  const root = workspace(t);
  const target = path.join(root, 'index.js');
  fs.writeFileSync(target, 'export const original = true;');
  fs.writeFileSync(`${target}.kanaloa-backup`, 'previous backup');
  assert.throws(() => writePatchedFile(target, 'export const broken = ;'), /original retained/);
  assert.equal(fs.readFileSync(target, 'utf8'), 'export const original = true;');
  assert.equal(fs.readFileSync(`${target}.kanaloa-backup`, 'utf8'), 'previous backup');
  assert.deepEqual(fs.readdirSync(root).sort(), ['index.js', 'index.js.kanaloa-backup']);
});

test('valid patch replaces atomically and preserves the original for rollback', t => {
  const root = workspace(t);
  const target = path.join(root, 'index.js');
  fs.writeFileSync(target, 'export const original = true;');
  writePatchedFile(target, 'export const patched = true;');
  assert.equal(fs.readFileSync(target, 'utf8'), 'export const patched = true;');
  assert.equal(fs.readFileSync(`${target}.kanaloa-backup`, 'utf8'), 'export const original = true;');
});

test('version and partial patch checks fail without touching runtime bytes', t => {
  const root = workspace(t);
  const pkg = path.join(root, 'node_modules', '@deepseek-ai', 'dsh-acp');
  fs.mkdirSync(path.join(pkg, 'lib'), { recursive: true });
  const target = path.join(pkg, 'lib', 'index.js');
  const partial = 'this.agent.steer(message);';
  fs.writeFileSync(target, partial);
  fs.writeFileSync(path.join(pkg, 'package.json'), JSON.stringify({ version: '0.0.0' }));
  assert.throws(() => verifyAndPatchProjectDshAcp(root), /Unsupported/);
  fs.writeFileSync(path.join(pkg, 'package.json'), JSON.stringify({ version: '0.1.5-rc.3' }));
  assert.throws(() => verifyAndPatchProjectDshAcp(root), /partial steer/);
  assert.equal(fs.readFileSync(target, 'utf8'), partial);
  assert.deepEqual(fs.readdirSync(path.join(pkg, 'lib')), ['index.js']);
});

test('current pinned runtime patch is idempotent in an isolated copy', t => {
  const root = workspace(t);
  const source = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../node_modules/@deepseek-ai/dsh-acp');
  const target = path.join(root, 'node_modules', '@deepseek-ai', 'dsh-acp');
  fs.mkdirSync(path.join(target, 'lib'), { recursive: true });
  fs.copyFileSync(path.join(source, 'package.json'), path.join(target, 'package.json'));
  fs.copyFileSync(path.join(source, 'lib/index.js'), path.join(target, 'lib/index.js'));
  const before = fs.readFileSync(path.join(target, 'lib/index.js'), 'utf8');
  verifyAndPatchProjectDshAcp(root);
  const installed = fs.readFileSync(path.join(target, 'lib/index.js'), 'utf8');
  assert.equal(verifyAndPatchProjectDshAcp(root), false);
  assert.equal(fs.readFileSync(path.join(target, 'lib/index.js'), 'utf8'), installed);
  assert.ok(before.includes('function invalidParams(detail)'));
  assert.ok(installed.includes('throw invalidParams("Kanaloa model is not configured;'));
});

test('already-patched but syntactically broken runtime fails closed without rewriting it', t => {
  const root = workspace(t);
  const source = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../node_modules/@deepseek-ai/dsh-acp');
  const target = path.join(root, 'node_modules', '@deepseek-ai', 'dsh-acp');
  fs.mkdirSync(path.join(target, 'lib'), { recursive: true });
  fs.copyFileSync(path.join(source, 'package.json'), path.join(target, 'package.json'));
  fs.copyFileSync(path.join(source, 'lib/index.js'), path.join(target, 'lib/index.js'));
  verifyAndPatchProjectDshAcp(root);
  const entry = path.join(target, 'lib/index.js');
  const broken = fs.readFileSync(entry, 'utf8') + '\nconst damaged = ;\n';
  fs.writeFileSync(entry, broken);
  assert.throws(() => verifyAndPatchProjectDshAcp(root), /syntax validation failed/);
  assert.equal(fs.readFileSync(entry, 'utf8'), broken);
});
