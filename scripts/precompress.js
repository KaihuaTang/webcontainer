#!/usr/bin/env node
/*
 * 给静态项目生成 .br / .gz 预压缩副本（递归整棵目录树）。
 *
 * 网关用 aiohttp 的 FileResponse 直出静态文件，它内建「同名预压缩副本」支持：
 * 请求 index.js 时若 Accept-Encoding 含 br/gzip 且 index.js.br / index.js.gz
 * 存在，就直接发送副本，Content-Type 仍按原文件名判定，并自动补
 * Content-Encoding 与 Vary（见 aiohttp/web_fileresponse.py 的 ENCODING_EXTENSIONS）。
 * 于是「压一次、每次请求都省流量」，不占网关 CPU。
 *
 * 用法：
 *   node scripts/precompress.js container/InchShade/dist [更多目录…]
 *   node scripts/precompress.js --force container/StillWind/dist   # 忽略新鲜度重压
 *
 * 幂等：副本比原文件新就跳过；原文件已删除或已更新的旧副本会被清掉
 * （残留旧副本会被网关优先发出，让访客拿到上一版代码——务必在每次
 *  重新构建后重跑本脚本）。压缩率高于 90% 的副本没有收益，直接丢弃。
 *
 * container/SWAPSHOT、container/PlayCard 这类 Godot 项目用的是各自
 * tools/adapt.js（打补丁 + 压缩一起做），逻辑与此处一致。
 */
'use strict';

const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const { pipeline } = require('stream/promises');

// 只压文本/字节码；png、jpg、woff2、mp3 等自身已压缩的格式压了反而更大
const COMPRESSIBLE = new Set([
	'.js', '.mjs', '.cjs', '.css', '.html', '.htm', '.json', '.svg', '.xml',
	'.txt', '.md', '.map', '.wasm', '.pck', '.csv', '.ico',
]);
const MIN_SIZE = 1024;    // 小于 1KB 压了也省不下传输
const MAX_RATIO = 0.9;    // 压缩率高于 90% 视为不值得，删掉副本
const VARIANTS = [
	['.gz', (size) => zlib.createGzip({ level: 9 })],
	['.br', (size) => zlib.createBrotliCompress({
		params: {
			[zlib.constants.BROTLI_PARAM_QUALITY]: 11,
			[zlib.constants.BROTLI_PARAM_LGWIN]: 24,
			[zlib.constants.BROTLI_PARAM_SIZE_HINT]: size,
		},
	})],
];

const args = process.argv.slice(2);
const force = args.includes('--force');
const targets = args.filter((a) => a !== '--force');

if (targets.length === 0) {
	console.error('用法: node scripts/precompress.js [--force] <目录> [更多目录…]');
	process.exit(1);
}

function* walk(dir) {
	for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
		// 跳过隐藏目录/文件：.vite（构建元数据，服务端明确拒绝直出）、.DS_Store 等
		if (entry.name.startsWith('.')) continue;
		const full = path.join(dir, entry.name);
		if (entry.isDirectory()) yield* walk(full);
		else if (entry.isFile()) yield full;
	}
}

function human(bytes) {
	return bytes >= 1048576
		? `${(bytes / 1048576).toFixed(2)}MB`
		: `${(bytes / 1024).toFixed(1)}KB`;
}

/** 清理孤儿副本（原文件已删）与过期副本（原文件更新过）。 */
function sweep(root) {
	let removed = 0;
	for (const file of walk(root)) {
		const ext = path.extname(file);
		if (ext !== '.br' && ext !== '.gz') continue;
		const source = file.slice(0, -ext.length);
		let stale = !fs.existsSync(source);
		if (!stale && fs.statSync(source).mtimeMs > fs.statSync(file).mtimeMs) stale = true;
		if (stale || force) {
			fs.unlinkSync(file);
			removed += 1;
			console.log(`清理 ${path.relative(root, file)}`);
		}
	}
	return removed;
}

async function compressTree(root) {
	let saved = 0;
	let made = 0;
	for (const file of walk(root)) {
		const ext = path.extname(file);
		if (ext === '.br' || ext === '.gz') continue;
		if (!COMPRESSIBLE.has(ext)) continue;
		const size = fs.statSync(file).size;
		if (size < MIN_SIZE) continue;

		for (const [suffix, makeStream] of VARIANTS) {
			const dst = file + suffix;
			if (fs.existsSync(dst)) continue;   // sweep 已经清掉过期的了
			await pipeline(fs.createReadStream(file), makeStream(size), fs.createWriteStream(dst));
			const out = fs.statSync(dst).size;
			const ratio = out / size;
			if (ratio > MAX_RATIO) {
				fs.unlinkSync(dst);
				console.log(`跳过 ${path.relative(root, dst)}（压缩率 ${(ratio * 100).toFixed(1)}%，收益太小）`);
				continue;
			}
			made += 1;
			if (suffix === '.br') saved += size - out;
			console.log(
				`${path.relative(root, dst)}  ${human(size)} -> ${human(out)}`
				+ `  (${(ratio * 100).toFixed(1)}%)`
			);
		}
	}
	return { made, saved };
}

(async () => {
	for (const target of targets) {
		const root = path.resolve(target);
		if (!fs.existsSync(root) || !fs.statSync(root).isDirectory()) {
			console.error(`找不到目录：${target}`);
			process.exit(1);
		}
		console.log(`==> ${target}`);
		sweep(root);
		const { made, saved } = await compressTree(root);
		console.log(`    生成 ${made} 份副本，br 相比原文件省下 ${human(Math.max(saved, 0))}\n`);
	}
})().catch((err) => {
	console.error(err);
	process.exit(1);
});
