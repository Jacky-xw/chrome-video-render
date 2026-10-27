#!/usr/bin/env node
// 只读取 composition 的元数据（帧数、尺寸、fps），不渲染任何帧。
// 入口脚本必须先知道帧数才能判断短/长视频、选对应方案，所以单独提供这个
// 轻量查询：只做 bundle 与 selectComposition，并复用 worker 的 bundle 缓存。
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {createRequire} = require('node:module');

const required = (name) => {
  const value = process.env[name];
  if (!value) throw new Error(`missing ${name}`);
  return value;
};

(async () => {
  const project = required('CVR_PROJECT');
  const entry = required('CVR_ENTRY');
  const compositionId = required('CVR_COMPOSITION');
  const inputProps = JSON.parse(process.env.CVR_INPUT_PROPS || '{}');
  const browserExecutable = process.env.CVR_BROWSER || undefined;
  const chromeMode = process.env.CVR_CHROME_MODE || 'headless-shell';

  // 与 worker 一致：用目标项目的依赖解析方式，避免宿主 NODE_PATH 干扰。
  const projectRequire = createRequire(path.join(entry, '..', 'package.json'));
  const {bundle} = projectRequire('@remotion/bundler');
  const {selectComposition} = projectRequire('@remotion/renderer');

  // 复用 worker 的 bundle 缓存，避免为了读元数据重复打包。
  const cacheBase = path.join(project, 'node_modules', '.cache', 'cvr-bundle');
  const hash = crypto.createHash('sha1');
  const walk = (dir, depth) => {
    if (depth > 6) return;
    let entries = [];
    try {
      entries = fs.readdirSync(dir, {withFileTypes: true});
    } catch {
      return;
    }
    entries.sort((a, b) => a.name.localeCompare(b.name));
    for (const item of entries) {
      const full = path.join(dir, item.name);
      if (item.isDirectory()) {
        if (item.name === 'node_modules' || item.name.startsWith('.')) continue;
        walk(full, depth + 1);
      } else if (/\.(tsx?|jsx?|css|json)$/.test(item.name)) {
        hash.update(full);
        try {
          hash.update(fs.readFileSync(full));
        } catch {}
      }
    }
  };
  walk(path.dirname(entry), 0);
  hash.update(project);
  const serveUrl = path.join(cacheBase, hash.digest('hex').slice(0, 16));

  if (!fs.existsSync(serveUrl)) {
    fs.mkdirSync(cacheBase, {recursive: true});
    await bundle({entryPoint: entry, outDir: serveUrl});
  }

  const composition = await selectComposition({
    serveUrl,
    id: compositionId,
    inputProps,
    browserExecutable,
    chromeMode,
  });
  process.stdout.write(
    JSON.stringify({
      durationInFrames: Number(composition.durationInFrames),
      fps: Number(composition.fps),
      width: Number(composition.width),
      height: Number(composition.height),
    }),
  );
})().catch((error) => {
  process.stderr.write(String(error && error.message ? error.message : error) + '\n');
  process.exit(1);
});
