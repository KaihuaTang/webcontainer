#!/usr/bin/env node
/*
 * 把 public/ 里的 Godot 4.7 Web 发布包适配到 webcontainer 门户。
 *
 * 门户以 http://<主机>:38000 提供服务：浏览器只把 localhost 视为安全上下文，
 * 因此外部访问时 window.isSecureContext === false，一批 [SecureContext] API
 * （AudioWorklet / Gamepad …）在页面里根本不存在。Godot 4.7 的 Web 运行时
 * 有三处会因此出问题，本脚本逐一处理：
 *
 * A. index.html —— 加载器把 Engine.getMissingFeatures() 的结果当硬性门槛，其中含
 *    "Secure Context - Check web server configuration (use HTTPS)"，会直接拒绝启动。
 *    游戏真正依赖的 WebAssembly / WebGL2 / fetch 在普通 HTTP 下都可用，
 *    故把这一条从缺失特性列表里过滤掉。
 * B. index.js —— godot_audio_init 无条件调用 ctx.audioWorklet.addModule(...)，
 *    抛 TypeError 把 startGame 打挂，页面停在
 *    "Cannot read properties of undefined (reading 'addModule')"。
 * C. index.js —— SampleNode.connectPositionWorklet 里 new AudioWorkletNode 失败后
 *    整段被 catch 掉，连带 this.start() 一起没执行，音效不响。
 *    改为兜底：放弃播放位置上报，声音照常走 ScriptProcessor 路径。
 *
 * 另有一处与门户「单来源多项目」有关：
 *
 * D. index.html —— Godot 存档走 IDBFS，IndexedDB 库名取自挂载点 "/userfs"，
 *    同一来源下的多个 Godot 项目会共用它。门户所有项目同处一个来源，
 *    故按 /apps/<项目>/ 路径给库名加前缀，避免互相覆盖存档。
 *
 * 以及部署优化：
 *
 * E. 生成 .gz / .br 预压缩副本。网关（aiohttp FileResponse）按请求的
 *    Accept-Encoding 优先发送同名副本，并自动补 Content-Encoding 与 Vary，
 *    Content-Type 仍按原文件名判定。39MB 的 index.wasm 由此压到 ~6.6MB。
 *
 * index.html 补丁支持多套锚点：Godot 默认导出模板、以及本项目这类手写外壳
 * （把 getMissingFeatures 调用写成单行）。命中哪套自动判断，都对不上才报错。
 *
 * ⚠️ 每次用新构建替换 public/ 后都要重跑本脚本：补丁会丢失、旧压缩副本会残留
 *    （残留副本会被优先发出，让玩家拿到上一版 wasm）。脚本幂等，重跑总是安全的；
 *    锚点对不上会报错退出并指出是哪一处，不会写出半成品。
 *
 * ⚠️ 若整包覆盖的是**项目目录**而不是 public/，project.json / icon.svg / tools/
 *    会一起被删掉，门户上该项目直接消失。恢复办法见 docs/examples/godot-web/。
 *
 * 用法（项目根目录）：node tools/adapt.js
 */

'use strict';

const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const { pipeline } = require('stream/promises');

const PUBLIC_DIR = path.join(__dirname, '..', 'public');
const INDEX_HTML = path.join(PUBLIC_DIR, 'index.html');
const INDEX_JS = path.join(PUBLIC_DIR, 'index.js');
const MARK = 'webcontainer 适配';

// ---- 补丁定义 -----------------------------------------------------------

/** A 的补丁体：把 Secure Context 一条滤掉；indent 为该语句所在行的缩进。 */
function secureContextFilter(indent) {
	const b = indent + '\t';
	return '.filter(function (feature) {\n'
		+ `${b}// ${MARK}：门户以 http://<主机>:38000 提供服务，属非安全上下文。\n`
		+ `${b}// 引擎实际需要的 WebAssembly / WebGL2 / fetch 在普通 HTTP 下均可用，\n`
		+ `${b}// 音频自动回落到 ScriptProcessor；仅 Gamepad API 不可用（键鼠、触屏不受影响）。\n`
		+ `${b}if (feature.indexOf('Secure Context') === 0) {\n`
		+ `${b}\tconsole.warn('[${MARK}] 非安全上下文（HTTP），已放行 Godot 的 Secure Context 检查；'\n`
		+ `${b}\t\t+ '如需手柄支持请以 HTTPS 或 localhost 访问。');\n`
		+ `${b}\treturn false;\n`
		+ `${b}}\n`
		+ `${b}return true;\n`
		+ `${indent}})`;
}

/** D 的补丁体：在 index.js 之前插一段 indexedDB.open 包装。 */
function idbIsolationShim(indent) {
	return `${indent}<script>\n`
		+ `// ${MARK}：Godot 存档走 IDBFS，库名取自挂载点 "/userfs"；门户所有项目同处\n`
		+ '// http://<主机>:38000 这一个来源，会共用同一个库。按 /apps/<项目>/ 路径加前缀隔离。\n'
		+ '(function () {\n'
		+ "\tif (!window.indexedDB || typeof indexedDB.open !== 'function') {\n"
		+ '\t\treturn;\n'
		+ '\t}\n'
		+ "\tvar prefix = location.pathname.replace(/[^/]*$/, '');\n"
		+ "\tif (prefix === '/') {\n"
		+ '\t\treturn;  // 直接挂在站点根下时无需隔离\n'
		+ '\t}\n'
		+ '\tvar open = indexedDB.open.bind(indexedDB);\n'
		+ '\tindexedDB.open = function (name, version) {\n'
		+ "\t\tif (typeof name === 'string' && name.charAt(0) === '/') {\n"
		+ '\t\t\tname = prefix + name.slice(1);\n'
		+ '\t\t}\n'
		+ '\t\treturn version === undefined ? open(name) : open(name, version);\n'
		+ '\t};\n'
		+ '}());\n'
		+ `${indent}</script>\n`
		+ `${indent}<script src="index.js"></script>\n`;
}

const HTML_PATCHES = [
	{
		name: 'A. 放行加载器的 Secure Context 检查',
		variants: [
			{
				shell: '手写外壳（单行调用）',
				from: '\t\tconst missing = Engine.getMissingFeatures({ threads: false });\n',
				to: '\t\tconst missing = Engine.getMissingFeatures({ threads: false })'
					+ secureContextFilter('\t\t') + ';\n',
			},
			{
				shell: 'Godot 默认模板',
				from: '\tconst missing = Engine.getMissingFeatures({\n'
					+ '\t\tthreads: GODOT_THREADS_ENABLED,\n'
					+ '\t});\n',
				to: '\tconst missing = Engine.getMissingFeatures({\n'
					+ '\t\tthreads: GODOT_THREADS_ENABLED,\n'
					+ '\t})' + secureContextFilter('\t') + ';\n',
			},
		],
	},
	{
		name: 'D. 按项目路径隔离 IndexedDB 存档库',
		variants: [
			{
				shell: '手写外壳（1 个 tab 缩进）',
				from: '\t<script src="index.js"></script>\n',
				to: idbIsolationShim('\t'),
			},
			{
				shell: 'Godot 默认模板（2 个 tab 缩进）',
				from: '\t\t<script src="index.js"></script>\n',
				to: idbIsolationShim('\t\t'),
			},
		],
	},
];

const JS_PATCHES = [
	{
		name: 'B. godot_audio_init 无条件调用 audioWorklet.addModule',
		variants: [{
			shell: 'Godot 4.7 运行时',
			from: 'GodotAudio.audioPositionWorkletPromise=ctx.audioWorklet.addModule(path);',
			to: 'GodotAudio.audioPositionWorkletPromise=ctx.audioWorklet?ctx.audioWorklet.addModule(path):Promise.resolve();',
		}],
	},
	{
		name: 'C. SampleNode.connectPositionWorklet 失败时漏掉 start()',
		variants: [{
			shell: 'Godot 4.7 运行时',
			from: 'async connectPositionWorklet(start){await GodotAudio.audioPositionWorkletPromise;'
				+ 'if(this.isCanceled){return}this._source.connect(this.getPositionWorklet());if(start){this.start()}}',
			to: 'async connectPositionWorklet(start){try{await GodotAudio.audioPositionWorkletPromise;'
				+ 'if(this.isCanceled){return}this._source.connect(this.getPositionWorklet())}'
				+ 'catch(e){if(this.isCanceled){return}'
				+ 'if(!GodotAudio.__wcNoPositionWorklet){GodotAudio.__wcNoPositionWorklet=true;'
				+ `console.warn("[${MARK}] 当前上下文没有 AudioWorklet，已跳过播放位置上报，音效照常播放。")}}`
				+ 'if(start){this.start()}}',
		}],
	},
];

/** 定点替换；已打过的跳过，所有变体都对不上则报错退出（不写半成品）。 */
function applyPatches(file, patches) {
	const label = path.basename(file);
	let text = fs.readFileSync(file, 'utf8');
	let changed = 0;

	for (const patch of patches) {
		if (patch.variants.some((v) => text.includes(v.to))) {
			console.log(`${label} 已含补丁「${patch.name}」，跳过`);
			continue;
		}

		const hit = patch.variants
			.map((v) => ({ variant: v, hits: text.split(v.from).length - 1 }))
			.find((r) => r.hits === 1);

		if (!hit) {
			const detail = patch.variants
				.map((v) => `${v.shell} 命中 ${text.split(v.from).length - 1} 次`)
				.join('；');
			console.error(
				`${label} 中「${patch.name}」找不到唯一锚点（${detail}，均应为 1 次）：`
				+ 'Godot 版本或页面外壳可能变了，请人工确认后更新 tools/adapt.js。'
			);
			process.exit(1);
		}

		text = text.replace(hit.variant.from, hit.variant.to);
		changed += 1;
		console.log(`${label} 打上「${patch.name}」（匹配到：${hit.variant.shell}）`);
	}

	if (changed) {
		fs.writeFileSync(file, text);
		console.log(`${label} 共改 ${changed} 处（其 SHA256 将与 SHA256SUMS.txt 不一致，属预期）`);
	}
}

// ---- E. 预压缩 -----------------------------------------------------------

// 只压可压的文本/字节码；png 等已压缩格式跳过
const COMPRESSIBLE = new Set(['.wasm', '.js', '.html', '.css', '.json', '.svg', '.txt', '.md', '.pck']);
const MIN_SIZE = 1024;      // 小于 1KB 的文件压了也省不了传输
const MAX_RATIO = 0.9;      // 压缩率高于 90% 视为不值得，直接丢弃副本

function listFiles(dir) {
	return fs.readdirSync(dir, { withFileTypes: true })
		.filter((entry) => entry.isFile())
		.map((entry) => entry.name);
}

async function precompressAll() {
	// 先清理旧副本，避免构建更新后残留
	for (const name of listFiles(PUBLIC_DIR)) {
		if (name.endsWith('.gz') || name.endsWith('.br')) {
			fs.unlinkSync(path.join(PUBLIC_DIR, name));
			console.log(`清理 ${name}`);
		}
	}

	for (const name of listFiles(PUBLIC_DIR)) {
		const src = path.join(PUBLIC_DIR, name);
		const size = fs.statSync(src).size;
		if (!COMPRESSIBLE.has(path.extname(name)) || size < MIN_SIZE) continue;

		const variants = [
			['.gz', () => zlib.createGzip({ level: 9 })],
			['.br', () => zlib.createBrotliCompress({
				params: {
					[zlib.constants.BROTLI_PARAM_QUALITY]: 11,
					[zlib.constants.BROTLI_PARAM_LGWIN]: 24,
					[zlib.constants.BROTLI_PARAM_SIZE_HINT]: size,
				},
			})],
		];

		for (const [ext, makeStream] of variants) {
			const dst = src + ext;
			const started = process.hrtime.bigint();
			await pipeline(fs.createReadStream(src), makeStream(), fs.createWriteStream(dst));
			const out = fs.statSync(dst).size;
			const ratio = out / size;
			const secs = Number(process.hrtime.bigint() - started) / 1e9;
			if (ratio > MAX_RATIO) {
				fs.unlinkSync(dst);
				console.log(`跳过 ${name}${ext}（压缩率 ${(ratio * 100).toFixed(1)}%，收益太小）`);
				continue;
			}
			console.log(
				`${name}${ext}  ${(size / 1048576).toFixed(2)}MB -> ${(out / 1048576).toFixed(2)}MB`
				+ `  (${(ratio * 100).toFixed(1)}%, ${secs.toFixed(1)}s)`
			);
		}
	}
}

(async () => {
	if (!fs.existsSync(PUBLIC_DIR)) {
		console.error(`找不到 ${PUBLIC_DIR}`);
		process.exit(1);
	}
	applyPatches(INDEX_HTML, HTML_PATCHES);
	applyPatches(INDEX_JS, JS_PATCHES);
	await precompressAll();
})();
