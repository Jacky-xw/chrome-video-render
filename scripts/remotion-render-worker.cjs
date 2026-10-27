#!/usr/bin/env node
/* 用 Remotion 官方 API 完成分段渲染和 PCM/MKV 合并。
 *
 * 三个阶段：
 *   segment  渲染单段（一个进程、一段、一个 Chrome），产物按编号落盘
 *   combine  读取已落盘的分段，合并为 PCM/MKV 再编码最终 AAC
 *   all      在一个进程里渲染全部段再合并（探针等短任务使用）
 *
 * 分段产物写在 CVR_SEGMENT_DIR（默认 output 同级的 <name>.segments/），
 * 不随进程退出删除，因此一段渲好就落盘；已完成的段带 .json 标记，
 * 下次运行会跳过，可以只补渲缺的那几段。
 */
const fs = require('node:fs');
const path = require('node:path');
const required = (name) => {
  if (!process.env[name]) throw new Error(`Missing ${name}`);
  return process.env[name];
};

// 加载目标项目自己的 Remotion 依赖。
const {createRequire} = require('node:module');
const projectRequire = createRequire(path.join(required('CVR_ENTRY'), '..', 'package.json'));
const {bundle} = projectRequire('@remotion/bundler');
const {
  selectComposition,
  renderMedia,
  combineChunks,
} = projectRequire('@remotion/renderer');

const probe = (file) => new Promise((resolve, reject) => {
  const {spawn} = require('node:child_process');
  const child = spawn('ffprobe', ['-v', 'error', '-show_streams', '-show_format', '-of', 'json', file]);
  let stdout = '';
  let stderr = '';
  child.stdout.on('data', (x) => { stdout += x; });
  child.stderr.on('data', (x) => { stderr += x; });
  child.on('error', reject);
  child.on('close', (code) => code === 0 ? resolve(JSON.parse(stdout)) : reject(new Error(stderr)));
});

const crypto = require('node:crypto');

// 对入口及其同目录源码取哈希；任一文件变化都会触发重新打包。
function sourceFingerprint(entry) {
  const dir = path.dirname(entry);
  const hash = crypto.createHash('sha256');
  const files = [];
  const walk = (current) => {
    for (const name of fs.readdirSync(current).sort()) {
      const full = path.join(current, name);
      const stat = fs.statSync(full);
      if (stat.isDirectory()) {
        if (name !== 'node_modules' && !name.startsWith('.')) walk(full);
      } else if (/\.(t|j)sx?$|\.[cm]?js$|/i.test(name)) {
        files.push(full);
      }
    }
  };
  walk(dir);
  files.sort();
  for (const file of files) {
    hash.update(path.relative(dir, file));
    hash.update(fs.readFileSync(file));
  }
  return hash.digest('hex').slice(0, 16);
}

// 复用 bundle：源码未变且缓存完整时直接返回旧目录，避免每次渲染重复打包。
async function bundleCached(entry) {
  const projectRoot = path.resolve(required('CVR_PROJECT'));
  const cacheBase = path.join(projectRoot, 'node_modules', '.cache', 'cvr-bundle');
  const target = path.join(cacheBase, sourceFingerprint(entry));
  const marker = path.join(target, 'index.html');
  if (fs.existsSync(marker)) {
    if (process.env.CVR_MODE === 'full') console.log(`CVR bundle cache hit: ${target}`);
    return {serveUrl: target, cached: true};
  }
  if (process.env.CVR_MODE === 'full') console.log('CVR bundle cache miss, bundling...');
  const built = await bundle({entryPoint: entry, outDir: target});
  return {serveUrl: built, cached: false};
}

const nowMs = () => Number(process.hrtime.bigint() / 1000000n);

// 分段编号统一两位起，排序即渲染顺序。
const segmentLabel = (index) => String(index + 1).padStart(2, '0');

// 分段产物目录：默认 output 同级的 <name>.segments/，显式传入时用传入值。
function resolveSegmentDir(output) {
  if (process.env.CVR_SEGMENT_DIR) return path.resolve(process.env.CVR_SEGMENT_DIR);
  const dir = path.dirname(output);
  return path.join(dir, `${path.basename(output, path.extname(output))}.segments`);
}

// 一段的产物路径与标记文件。
function segmentPaths(segDir, index) {
  const label = segmentLabel(index);
  return {
    videoFile: path.join(segDir, `${label}-video.ts`),
    audioFile: path.join(segDir, `${label}-audio.wav`),
    markerFile: path.join(segDir, `${label}.json`),
  };
}

// 读回已完成的段。标记存在且两个媒体文件都在，才算完成。
function readCompletedSegments(segDir) {
  const done = [];
  if (!fs.existsSync(segDir)) return done;
  for (const name of fs.readdirSync(segDir)) {
    if (!/^\d{2}\.json$/.test(name)) continue;
    let marker;
    try {
      marker = JSON.parse(fs.readFileSync(path.join(segDir, name), 'utf8'));
    } catch (e) {
      continue;
    }
    if (!marker || !marker.video || !marker.audio) continue;
    if (!fs.existsSync(marker.video) || !fs.existsSync(marker.audio)) continue;
    done.push(marker);
  }
  done.sort((a, b) => a.index - b.index);
  return done;
}

// 运行中采样真实内存占用。公式里的每 worker 开销是估算值，不同设备、不同
// composition 的实际占用可能相差数倍；采到的峰值写进报告，供上层校准。
function makeMemorySampler() {
  const sampler = {peakSystemUsed: 0, peakRss: 0, samples: 0, timer: null};
  const readMemInfo = () => {
    let total = 0, avail = 0;
    try {
      for (const line of fs.readFileSync('/proc/meminfo', 'utf8').split('\n')) {
        if (line.startsWith('MemTotal:')) total = Number(line.split(/\s+/)[1]) * 1024;
        else if (line.startsWith('MemAvailable:')) avail = Number(line.split(/\s+/)[1]) * 1024;
      }
    } catch (e) {}
    return {total, avail};
  };
  const sample = () => {
    const {total, avail} = readMemInfo();
    if (total && avail) sampler.peakSystemUsed = Math.max(sampler.peakSystemUsed, total - avail);
    try {
      const rss = process.memoryUsage().rss;
      sampler.peakRss = Math.max(sampler.peakRss, rss);
    } catch (e) {}
    sampler.samples += 1;
  };
  if (process.env.CVR_SAMPLE_MEMORY !== '0') {
    sampler.timer = setInterval(sample, 250);
    if (sampler.timer.unref) sampler.timer.unref();
  }
  return sampler;
}

async function main() {
  const entry = path.resolve(required('CVR_ENTRY'));
  const compositionId = required('CVR_COMPOSITION');
  const output = path.resolve(required('CVR_OUTPUT'));
  const browserExecutable = required('CVR_BROWSER');
  const chromeMode = required('CVR_CHROME_MODE');
  const mode = process.env.CVR_MODE === 'full' ? 'full' : 'quick';
  const stage = process.env.CVR_STAGE || 'all';
  const requestedSegmentFrames = Number(process.env.CVR_SEGMENT_FRAMES || 0);
  let workers = Number(required('CVR_WORKERS'));
  const inputProps = JSON.parse(process.env.CVR_INPUT_PROPS || '{}');
  if (requestedSegmentFrames && (!Number.isInteger(requestedSegmentFrames) || requestedSegmentFrames < 5)) {
    throw new Error('segmentFrames must be an integer >= 5');
  }
  if (!Number.isInteger(workers) || workers < 1) throw new Error('workers must be positive');

  const timings = {};
  // 从进程启动到 main() 的间隔，主要是加载 Remotion 模块的固定成本。
  timings.node_startup_ms = Math.round(process.uptime() * 1000);
  let mark = nowMs();
  const bundled = await bundleCached(entry);
  const serveUrl = bundled.serveUrl;
  timings.bundle_ms = nowMs() - mark;
  timings.bundle_cache_hit = bundled.cached;
  mark = nowMs();
  const composition = await selectComposition({
    serveUrl,
    id: compositionId,
    inputProps,
    browserExecutable,
    chromeMode,
  });
  timings.select_composition_ms = nowMs() - mark;
  const fps = Number(composition.fps);
  const total = Number(composition.durationInFrames);
  if (!Number.isFinite(fps) || fps <= 0 || !Number.isInteger(total) || total < 1) {
    throw new Error(`Invalid composition metadata: ${JSON.stringify(composition)}`);
  }
  // 按真实画面尺寸复核并行预算，口径与 Python 侧 worker_budget 一致：
  // 每 worker 内存 = 固定开销 + 帧缓冲×分辨率。大核上限优先取 CVR_WORKER_CEILING。
  const hardware = JSON.parse(required('CVR_HARDWARE_JSON'));
  const totalMem = Number(hardware.memory_total_bytes) || 0;
  const availMem = Number(hardware.memory_available_bytes) || 0;
  const reserve = Math.max(2 * 1024 ** 3, Math.floor(totalMem * 0.20));
  const pixelBytes = composition.width * composition.height * 4;
  // 有本机实测系数时以实测为准：公式只是起点，测过真实占用就该用真实值。
  // Python 侧传下来的是未经安全余量的原始系数；这里补上同一系数，保证两侧
  // 算出同一个每 worker 预算，否则 Python 按保守值限制、JS 按原始值放宽。
  const calibrationBytes = Number(hardware.measured_bytes_per_worker) || 0;
  const safetyFactor = Number(hardware.memory_safety_factor) || 1;
  const perWorker = calibrationBytes > 0
    ? Math.floor(calibrationBytes * safetyFactor)
    : Math.floor(768 * 1024 ** 2 + pixelBytes * 48);
  const memoryBasis = calibrationBytes > 0 ? 'measured' : 'estimated';
  const ceiling = Number(process.env.CVR_WORKER_CEILING || 0);
  const cpuLimit = Math.max(1, Math.min(
    Number(required('CVR_MAX_WORKERS')),
    ceiling > 0 ? ceiling : Math.floor((hardware.cpu_count || 1) / 2) || 1,
  ));
  const memoryLimit = Math.max(0, Math.floor((availMem - reserve) / perWorker));
  const requested = Number(process.env.CVR_REQUESTED_WORKERS || workers);
  // 段真实并发数由上层调度器决定，通过 CVR_EFFECTIVE_WORKERS 传下来。
  // combine 阶段不再开浏览器，重算预算只会得到一个与段阶段无关的数字：
  // 可用内存此刻偏紧时会把 workers 压成 1，写进报告的并发数就成了假的，
  // 上层据此拒绝记录内存校准（单 worker 无法分离固定开销），实测系数永远存不下。
  const effectiveWorkers = Number(process.env.CVR_EFFECTIVE_WORKERS || 0);
  if (Number.isInteger(effectiveWorkers) && effectiveWorkers >= 1) {
    workers = effectiveWorkers;
  } else {
    workers = Math.max(1, Math.min(cpuLimit, memoryLimit || 1, requested));
  }

  const memSampler = makeMemorySampler();
  if (mode === 'full') console.log(`CVR hardware budget: ${workers} parallel segment(s), ~${Math.round(perWorker / 1024 ** 2)} MiB/worker (${memoryBasis})`);

  // 分段划分。未手动指定段长时按总帧数自适应，避免长片切成过多小段反复冷启动浏览器。
  const adaptiveSize = total <= 300 ? 90 : total <= 900 ? 150 : total <= 3000 ? 300 : 450;
  const segmentFrames = requestedSegmentFrames
    || Math.max(30, Math.min(600, adaptiveSize));
  const ranges = [];
  for (let start = 0; start < total; start += segmentFrames) {
    ranges.push([start, Math.min(total - 1, start + segmentFrames - 1)]);
  }
  if (mode === 'full' && !requestedSegmentFrames) {
    console.log(`CVR adaptive segmentation: ${ranges.length} segment(s) x ${segmentFrames} frames`);
  }

  const segDir = resolveSegmentDir(output);
  const browserOptions = {browserExecutable, chromeMode};
  const common = {
    serveUrl,
    inputProps,
    concurrency: 1,
    disallowParallelEncoding: true,
    // 分段使用 PCM 音频。
    audioCodec: 'pcm-16',
    forSeamlessAacConcatenation: false,
    compositionStart: 0,
    enforceAudioTrack: true,
    muted: false,
    ...browserOptions,
  };

  // 渲染一段。只用这一段的浏览器，渲完即随进程退出关闭。
  async function renderSegment(index) {
    const [start, end] = ranges[index];
    const paths = segmentPaths(segDir, index);
    const existing = readCompletedSegments(segDir).find((m) => m.index === index);
    if (existing) {
      process.stdout.write(`CVR segment ${segmentLabel(index)} already done, skipping` + '\n');
      return existing;
    }
    if (mode === 'full') {
      process.stdout.write(`CVR segment ${segmentLabel(index)}/${ranges.length}: frames ${start}-${end}` + '\n');
    }
    fs.mkdirSync(segDir, {recursive: true});
    await renderMedia({
      ...common,
      composition,
      codec: 'h264-ts',
      frameRange: [start, end],
      outputLocation: paths.videoFile,
      separateAudioTo: paths.audioFile,
    });
    const info = await probe(paths.audioFile);
    const videoInfo = await probe(paths.videoFile);
    const audio = info.streams.find((s) => s.codec_type === 'audio');
    const video = videoInfo.streams.find((s) => s.codec_type === 'video');
    // 检查分段音频编码。
    if (!audio || !audio.codec_name.startsWith('pcm_')) throw new Error(`Segment ${index} is not PCM`);
    // 逐段解码计数只用于 rvc 的审计明细。quick 模式整片还会再校验一次，
    // 这里重复解码每个分段只是白白多花一次整片解码的时间。
    let decodedFrames = null;
    if (mode === 'full') {
      const frameCountText = require('node:child_process').execFileSync('ffprobe', [
        '-v', 'error', '-count_frames', '-select_streams', 'v:0',
        '-show_entries', 'stream=nb_read_frames', '-of', 'default=nw=1:nk=1', paths.videoFile,
      ], {encoding: 'utf8'}).trim();
      decodedFrames = Number(frameCountText);
      if (Number.isFinite(decodedFrames) && decodedFrames !== end - start + 1) {
        throw new Error(`Segment ${index}: ${decodedFrames} frames != ${end - start + 1}`);
      }
    }
    const marker = {
      index, start, end,
      expectedFrames: end - start + 1,
      decodedFrames: Number.isFinite(decodedFrames) ? decodedFrames : null,
      videoStreamExposed: Boolean(video),
      sampleRate: audio.sample_rate, channels: audio.channels,
      videoCodec: video?.codec_name ?? 'h264-ts-input',
      audioCodec: audio.codec_name,
      video: paths.videoFile,
      audio: paths.audioFile,
      renderedAt: new Date().toISOString(),
    };
    // 标记最后写：媒体文件落盘在前，标记存在即代表这一段完整可用。
    fs.writeFileSync(paths.markerFile, JSON.stringify(marker, null, 2) + '\n');
    return marker;
  }

  // 把已落盘的分段合并成最终成片。
  async function combineSegments() {
    const done = readCompletedSegments(segDir);
    if (done.length !== ranges.length) {
      const have = new Set(done.map((m) => m.index));
      const missing = ranges.map((_, i) => i).filter((i) => !have.has(i));
      throw new Error(`Segments incomplete: ${done.length}/${ranges.length} ready, missing ${missing.map(segmentLabel).join(',')}`);
    }
    const first = done[0];
    for (const marker of done) {
      if (marker.sampleRate !== first.sampleRate || marker.channels !== first.channels) {
        throw new Error(`Segment ${marker.index} audio layout changed`);
      }
    }
    mark = nowMs();
    const combinedPcm = path.join(segDir, 'combined-pcm.mkv');
    await combineChunks({
      outputLocation: combinedPcm,
      videoFiles: done.map((m) => m.video),
      audioFiles: done.map((m) => m.audio),
      codec: 'h264-mkv',
      audioCodec: 'pcm-16',
      audioBitrate: null,
      fps,
      framesPerChunk: segmentFrames,
      preferLossless: false,
      compositionDurationInFrames: total,
      sampleRate: Number(first.sampleRate) || 48000,
    });
    timings.combine_ms = nowMs() - mark;
    mark = nowMs();
    const {spawn} = require('node:child_process');
    await new Promise((resolve, reject) => {
      const args = ['-v', 'error', '-xerror', '-y', '-i', combinedPcm,
        '-map', '0:v:0', '-map', '0:a:0', '-c:v', 'copy',
        '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', output];
      const child = spawn('ffmpeg', args, {stdio: 'inherit'});
      child.on('error', reject);
      child.on('close', (code) => code === 0 ? resolve() : reject(new Error(`Final AAC encode failed: ${code}`)));
    });
    timings.final_encode_ms = nowMs() - mark;
    return done;
  }

  function writeReport(segmentReports) {
    const finalInfo = probeSync(output);
    const audio = finalInfo.streams.find((s) => s.codec_type === 'audio');
    const report = {
      mode,
      ok: true,
      foreground: true,
      background: false,
      browser: browserExecutable,
      chromeMode,
      remotion_version: projectRequire('remotion/package.json').version,
      graphics_backend: 'CVR browser launcher',
      composition: compositionId,
      input_props: inputProps,
      memory: (() => {
        memSampler.samples += 1;
        if (memSampler.timer) clearInterval(memSampler.timer);
        return {
          peak_system_used_bytes: memSampler.peakSystemUsed || null,
          peak_process_rss_bytes: memSampler.peakRss || null,
          samples: memSampler.samples,
          workers: Math.max(1, workers),
          resolution: [composition.width, composition.height],
        };
      })(),
      frames: total,
      fps: String(composition.fps),
      width: composition.width,
      height: composition.height,
      segments: ranges.length,
      segment_frames: segmentFrames,
      segment_dir: segDir,
      workers,
      duration: total / fps,
      sample_rate: audio ? Number(audio.sample_rate) : null,
      channels: audio ? Number(audio.channels) : null,
      output,
      timings,
      sync: {status: 'external-measurement-required'},
      note: 'Official Remotion renderMedia/combineChunks to PCM/MKV; one final AAC encode',
    };
    if (mode === 'full') {
      report.segment_validation = segmentReports;
      report.estimated_bytes_per_worker = perWorker;
      report.memory_reserve_bytes = reserve;
    }
    fs.writeFileSync(
      path.join(path.dirname(output), `${path.basename(output, path.extname(output))}.render-report.json`),
      JSON.stringify(report, null, 2) + '\n',
    );
    return report;
  }

  if (stage === 'plan') {
    // 规划阶段：只报出分段范围，不渲染。上层据此决定逐段怎么调度。
    // 分段目录要和本次任务对得上：段长或总帧数变了，旧的段落编号含义就变了，
    // 复用会导致拼接出错。对不上就清空重来。
    const manifestPath = path.join(segDir, 'plan.json');
    const manifest = {
      total, fps: String(fps), segment_frames: segmentFrames,
      width: composition.width, height: composition.height,
      composition: compositionId,
    };
    let reuse = false;
    try {
      const old = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
      reuse = JSON.stringify(old) === JSON.stringify(manifest);
    } catch (e) {
      reuse = false;
    }
    if (!reuse && fs.existsSync(segDir)) {
      for (const name of fs.readdirSync(segDir)) {
        try {
          fs.rmSync(path.join(segDir, name), {recursive: true, force: true});
        } catch (e) {}
      }
    }
    fs.mkdirSync(segDir, {recursive: true});
    fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + '\n');
    process.stdout.write('CVR_PLAN_OK ' + JSON.stringify({
      fps, total, frames: total, width: composition.width, height: composition.height,
      segment_frames: segmentFrames, ranges, segment_dir: segDir,
    }) + '\n');
    return;
  }

  if (stage === 'segment') {
    // 单段模式：只渲这一段，渲完退出，浏览器随之关闭。
    const index = Number(process.env.CVR_SEGMENT_INDEX);
    if (!Number.isInteger(index) || index < 0 || index >= ranges.length) {
      throw new Error(`CVR_SEGMENT_INDEX must be 0..${ranges.length - 1}`);
    }
    const marker = await renderSegment(index);
    process.stdout.write('CVR_SEGMENT_OK ' + JSON.stringify({
      index, label: segmentLabel(index), start: marker.start, end: marker.end,
      video: marker.video, audio: marker.audio, segment_dir: segDir,
    }) + '\n');
    return;
  }

  if (stage === 'combine') {
    // 合并模式：不再渲染，只用已落盘的分段出片。
    const done = await combineSegments();
    const report = writeReport(done);
    process.stdout.write('CVR_COMBINE_OK ' + JSON.stringify({
      output, segments: done.length, frames: report.frames, duration: report.duration,
    }) + '\n');
    return;
  }

  // all 模式：一个进程渲完全部段再合并。探针等短任务使用。
  fs.mkdirSync(segDir, {recursive: true});
  const files = new Array(ranges.length);
  const segmentReports = new Array(ranges.length);
  let cursor = 0;
  // 每段独立浏览器，段后释放，避免复用状态累积拖慢后续分段。
  const SEGMENT_TIMEOUT_MS = Number(process.env.CVR_SEGMENT_TIMEOUT_MS || 900000);

  async function withSegmentTimeout(promise, index) {
    let timer;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`Segment ${index} timed out after ${SEGMENT_TIMEOUT_MS}ms`)), SEGMENT_TIMEOUT_MS);
    });
    try {
      return await Promise.race([promise, timeout]);
    } finally {
      clearTimeout(timer);
    }
  }

  try {
    async function next() {
      while (cursor < ranges.length) {
        const index = cursor++;
        const marker = await withSegmentTimeout(renderSegment(index), index);
        segmentReports[index] = marker;
        files[index] = marker;
      }
    }
    mark = nowMs();
    await Promise.all(Array.from({length: Math.min(workers, ranges.length)}, next));
    timings.render_segments_ms = nowMs() - mark;
    await combineSegments();
    writeReport(segmentReports);
  } finally {
    // 分段产物保留在 segDir，供续跑与审计使用；成片已在 combine 阶段写好。
  }
}

// 同步探测：写报告前需要已经存在的成片文件。
function probeSync(file) {
  const {execFileSync} = require('node:child_process');
  const stdout = execFileSync('ffprobe', [
    '-v', 'error', '-show_streams', '-show_format', '-of', 'json', file,
  ], {encoding: 'utf8'});
  return JSON.parse(stdout);
}

// 中断时退出，不删除分段产物：已渲好的段要留着，下次运行可以接着来。
process.on('SIGINT', () => process.exit(130));
process.on('SIGTERM', () => process.exit(143));

// 失败时输出机器可读的一行，供上层判断该调什么。OOM 与超时都会让进程被
// SIGKILL（exit -9、stderr 为空），上层若只看到「失败」就只能瞎猜；这里把
// 阶段、worker 数、内存峰值一并带出去。
main().catch((error) => {
  const detail = (error && (error.stack || error.message)) || String(error);
  const reason = /out of memory|heap out of memory|ENOMEM|Cannot allocate/i.test(detail)
    ? 'out_of_memory'
    : /timed out/i.test(detail)
      ? 'timeout'
      : 'render_error';
  const payload = {
    reason,
    workers: typeof workers !== 'undefined' ? workers : null,
    stage: process.env.CVR_STAGE || 'all',
    memory_peak_bytes: typeof memSampler !== 'undefined' ? memSampler.peakSystemUsed : null,
    per_worker_bytes: typeof perWorker !== 'undefined' ? perWorker : null,
  };
  console.error('CVR_RENDER_FAILED ' + JSON.stringify(payload));
  console.error(detail);
  process.exitCode = reason === 'timeout' ? 3 : reason === 'out_of_memory' ? 2 : 1;
});
