/* 让 HyperFrames 使用 CVR 浏览器后端，保留其原生并行渲染和音频处理。 */
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';

const VENDOR = '{vendor: "Google Inc. (Google)", renderer: "SwiftShader"}';

export async function resolve(specifier, context, nextResolve) {
  if (specifier === process.env.CVR_HYPERFRAMES_ENGINE_URL) {
    // 保持上游相对导入路径。
    return {url: new URL(specifier, context.parentURL).href, shortCircuit: true};
  }
  return nextResolve(specifier, context);
}

export async function load(url, context, nextLoad) {
  const upstreamUrl = process.env.CVR_HYPERFRAMES_ENGINE_URL;
  if (url !== upstreamUrl) return nextLoad(url, context);
  if (!upstreamUrl) throw new Error('CVR_HYPERFRAMES_ENGINE_URL is required');
  let source = await readFile(new URL(upstreamUrl), 'utf8');
  const originalCanvas = source.match(/async function readWebGlVendorInfoFromCanvas\(page\) \{[\s\S]*?\n\}/);
  const originalGpu = source.match(/async function readWebGlVendorInfo\(page\) \{[\s\S]*?\n\}/);
  if (!originalCanvas || !originalGpu) throw new Error('HyperFrames WebGL probe signature changed; CVR requires review');
  // 只替换后端探测函数，其余模块沿用上游实现。
  source = source.replace(originalCanvas[0], `async function readWebGlVendorInfoFromCanvas(page) { return ${VENDOR}; }`);
  source = source.replace(originalGpu[0], `async function readWebGlVendorInfo(page) { return ${VENDOR}; }`);
  return {format: 'module', source, shortCircuit: true};
}
