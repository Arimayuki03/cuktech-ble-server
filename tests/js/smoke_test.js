#!/usr/bin/env node
/**
 * Smoke test: evaluates each page's scripts (app.js, phone.js, charge_history.js,
 * config.html inline script, embedded pages) in a sandbox with the REAL i18n
 * runtime and permissive DOM/fetch/Chart stubs, to catch ReferenceErrors and
 * other load-time/runtime wiring bugs introduced by the i18n changes.
 *
 * Usage: node tests/js/smoke_test.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

const WEB = path.resolve(__dirname, '../../web');
const STATIC = path.join(WEB, 'static');

function makeNoop() {
    const fn = function noop() {};
    return new Proxy(fn, {
        get(t, p) {
            if (p === Symbol.toPrimitive) return () => 0;
            return makeNoop();
        },
        set() { return true; },
        apply() { return makeNoop(); },
        construct() { return makeNoop(); },
    });
}

// 真实的 CSSStyleDeclaration 至少要有 setProperty/removeProperty——phone.js 用它
// 往卡片上写 --depth 这类自定义属性。只给 {} 的话这些赋值会被静默跳过，
// 冒烟测试就测不到那条路径了。按元素缓存一份，便于"写后读"。
function makeStyle() {
    const props = {};
    return {
        setProperty(k, v) { props[k] = String(v); },
        getPropertyValue(k) { return props[k] || ''; },
        removeProperty(k) { delete props[k]; },
    };
}

function makeEl() {
    const style = makeStyle();
    return new Proxy(function el() {}, {
        get(t, p) {
            if (p === 'style') return style;
            if (p === 'dataset') return {};
            if (p === 'classList') {
                return { add() {}, remove() {}, toggle() {}, contains() { return false; } };
            }
            if (p === 'children' || p === 'options') return [];
            return makeNoop();
        },
        set() { return true; },
        apply() { return makeNoop(); },
        construct() { return makeEl(); },
    });
}

function buildSandbox() {
    const sandbox = {
        console,
        Intl,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        Promise,
        JSON,
        Math,
        Date,
        RegExp,
        String,
        Number,
        Boolean,
        Object,
        Array,
        isNaN,
        parseFloat,
        parseInt,
        Error,
        fetch: async (url) => {
            // Serve a server-side language preference so every page's
            // initFromServer() → applyServerLanguage() path is exercised.
            if (String(url).indexOf('/api/web-language') !== -1) {
                return { ok: true, status: 200, json: async () => ({ ok: true, language: 'en' }) };
            }
            return { ok: true, status: 200, json: async () => ({}) };
        },
        EventSource: function () { this.onopen = null; this.onmessage = null; this.onerror = null; this.close = function () {}; },
        Chart: function () { return buildChart(); },
        localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        navigator: { language: 'en-US' },
        location: { origin: 'http://charger.local' },
        getComputedStyle: () => ({ getPropertyValue: () => '' }),
        addEventListener() {},
        removeEventListener() {},
    };
    sandbox.window = sandbox;
    const document = {
        documentElement: makeEl(),
        body: makeEl(),
        head: makeEl(),
        title: '',
        readyState: 'complete',
        getElementById: () => makeEl(),
        querySelector: () => makeEl(),
        querySelectorAll: () => [],
        createElement: () => makeEl(),
        addEventListener() {},
    };
    sandbox.document = document;
    return sandbox;
}

// evalInSandbox: 把**本仓库自身**的页面脚本挂到沙箱全局上执行（模拟浏览器
// <script> 加载）。用 globalThis 短暂换装的方式跑源码——脚本来源是仓库内
// 固定文件，不接触任何外部输入，不含动态执行外部内容的路径。
const SANDBOX_KEYS = ['window', 'document', 'navigator', 'localStorage',
                      'location', 'getComputedStyle', 'EventSource', 'Chart',
                      'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval',
                      'fetch', 'console', 'Intl'];

function evalInSandbox(sandbox, relPaths, inlineJs) {
    const saved = new Map();
    for (const k of SANDBOX_KEYS) {
        if (k in globalThis) saved.set(k, Object.getOwnPropertyDescriptor(globalThis, k));
        delete globalThis[k];
    }
    Object.defineProperties(globalThis,
        Object.fromEntries(Object.entries(sandbox).map(([k, v]) =>
            [k, { value: v, writable: true, configurable: true, enumerable: true }])));
    try {
        for (const rel of relPaths || []) {
            // 仓库内固定文件：内容随仓库走 review，非运行时外部输入
            const src = fs.readFileSync(path.join(STATIC, rel), 'utf8');
            new Function('window', 'globalThis', src)(sandbox, sandbox);
        }
        if (inlineJs) new Function('window', 'globalThis', inlineJs)(sandbox, sandbox);
    } finally {
        // 恢复原有全局的值语义，同时**保留**沙箱键挂载：页面脚本里导出的
        // 初始化函数可能在求值后才引用裸 document（IIFE 加载时不捕获）。
        for (const k of SANDBOX_KEYS) {
            if (saved.has(k)) {
                const d = saved.get(k);
                Object.defineProperty(globalThis, k, {
                    get() { return sandbox[k]; },
                    set(v) { sandbox[k] = v; },
                    configurable: true, enumerable: d.enumerable,
                });
            } else {
                Object.defineProperty(globalThis, k, {
                    value: sandbox[k], writable: true, configurable: true,
                    enumerable: true,
                });
            }
        }
    }
}

function buildChart() {
    return {
        data: { labels: [], datasets: [] },
        options: { scales: {} },
        _peakData: null,
        update() {},
        destroy() {},
        scales: {},
        canvas: { parentNode: makeEl() },
        ctx: {},
    };
}

function load(js) {
    const sandbox = buildSandbox();
    evalInSandbox(sandbox, ['locales/zh-CN.js', 'locales/en.js', 'i18n.js'], js);
    return sandbox;
}

// Flip the language back and forth: exercises every I18N.onChange re-render
// hook registered by the page scripts (rerenderDynamic, renderAll, refreshChargeHistory...).
function flipLocales(ctx, cycles) {
    const n = cycles || 2;
    for (let i = 0; i < n; i++) {
        ctx.I18N.setLocale('en');
        ctx.I18N.setLocale('zh-CN');
    }
}

const checks = [];
function check(name, fn) { checks.push({ name, fn }); }

function extractInlineScripts(htmlPath) {
    const html = fs.readFileSync(htmlPath, 'utf8');
    const scripts = [];
    const re = /<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/g;
    let m;
    while ((m = re.exec(html))) scripts.push(m[1]);
    return scripts.join('\n');
}

check('index.html chain: chart-config + chart-loader + charge_history + charge_limit + app', () => {
    const parts = [
        fs.readFileSync(path.join(STATIC, 'chart-config.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'chart-loader.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'charge_history.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'charge_limit.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8'),
    ].join('\n;\n');
    const ctx = load(parts);
    flipLocales(ctx); // exercises app.js rerenderDynamic + charge_history refresh
});

check('phone.html chain: charge_history + charge_limit + phone.js', () => {
    const parts = [
        fs.readFileSync(path.join(STATIC, 'charge_history.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'charge_limit.js'), 'utf8'),
        fs.readFileSync(path.join(STATIC, 'phone.js'), 'utf8'),
    ].join('\n;\n');
    const ctx = load(parts);
    flipLocales(ctx); // exercises phone.js renderAll on locale change
});

check('config.html inline script', () => {
    load(extractInlineScripts(path.join(WEB, 'config.html')));
});

check('countdown.html inline script', () => {
    const ctx = load(extractInlineScripts(path.join(STATIC, 'countdown.html')));
    flipLocales(ctx);
});

check('device_info.html inline script', () => {
    const ctx = load(extractInlineScripts(path.join(STATIC, 'device_info.html')));
    flipLocales(ctx);
});

check('port_monitor.html inline script', () => {
    const parts = [
        fs.readFileSync(path.join(STATIC, 'chart-config.js'), 'utf8'),
        extractInlineScripts(path.join(STATIC, 'port_monitor.html')),
    ].join('\n;\n');
    const ctx = load(parts);
    flipLocales(ctx);
});

check('power_chart.html inline script', () => {
    const parts = [
        fs.readFileSync(path.join(STATIC, 'chart-config.js'), 'utf8'),
        extractInlineScripts(path.join(STATIC, 'power_chart.html')),
    ].join('\n;\n');
    load(parts);
});

let failed = 0;
for (const { name, fn } of checks) {
    try {
        fn();
        console.log('  ✓ ' + name);
    } catch (e) {
        failed++;
        console.error('  ✗ ' + name);
        console.error('    ' + (e && e.stack ? e.stack.split('\n').slice(0, 4).join('\n    ') : e));
    }
}
console.log(failed === 0 ? `\nAll ${checks.length} smoke checks passed.` : `\n${failed}/${checks.length} smoke checks FAILED.`);
process.exit(failed === 0 ? 0 : 1);