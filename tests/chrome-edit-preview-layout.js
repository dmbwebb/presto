#!/usr/bin/env node
/* Chrome harness for Presto's slide-frame layout (presto/edit-layout.js).
 *
 * Runs the deck in headless Chrome, injects presto/edit-layout.js the way
 * Presto does, and for tall, wide, square and laptop windows checks real DOM
 * geometry on title, section-header, content and appendix slides with
 * tests/engine-redesign-layout-probe.js: the slide centred in the canvas (in
 * audience and preview modes, centred across and pinned to the top), the
 * section nav's top on the slide's top edge, the footer's bottom on the slide's
 * bottom edge, both exactly the slide's width, the progress bar along the
 * slide's top, the light canvas or letterbox, and the KaTeX compensation.
 *
 * WEBSLIDES_LAYOUT_MODE=edit (default): edit mode, a 232px rail, a 72px step
 * strip and 28px padding, with and without the 360px inspector.
 * WEBSLIDES_LAYOUT_MODE=preview (or present): audience mode, and preview mode
 * on ?mode=preview. WEBSLIDES_TEST_WIDTH/HEIGHT test one window size only.
 * The same probe runs in WebKit from tests/test_engine_redesign_layout.py.
 */

const fs = require('node:fs/promises');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');

const projectRoot = path.resolve(__dirname, '..');

function parsePositiveIntegerEnv(name, fallback) {
    const raw = process.env[name];
    if (raw === undefined || raw === '') return fallback;
    if (!/^\d+$/.test(raw)) {
        throw new Error(`${name} must be a positive integer, got ${JSON.stringify(raw)}`);
    }
    const parsed = Number(raw);
    if (!Number.isSafeInteger(parsed) || parsed <= 0) {
        throw new Error(`${name} must be a positive integer, got ${JSON.stringify(raw)}`);
    }
    return parsed;
}

const DEFAULT_SIZES = [
    { label: 'tall', width: 900, height: 1130 },
    { label: 'wide', width: 1800, height: 820 },
    { label: 'square', width: 1100, height: 1100 },
    { label: 'laptop', width: 1440, height: 900 }
];
let sizes;
try {
    const w = parsePositiveIntegerEnv('WEBSLIDES_TEST_WIDTH', 0);
    const h = parsePositiveIntegerEnv('WEBSLIDES_TEST_HEIGHT', 0);
    sizes = (w || h) ? [{ label: 'custom', width: w || 900, height: h || 1130 }] : DEFAULT_SIZES;
} catch (error) {
    console.error(`FAIL ${error.message}`);
    process.exit(1);
}

const layoutMode = process.env.WEBSLIDES_LAYOUT_MODE === 'present' ? 'preview' : (process.env.WEBSLIDES_LAYOUT_MODE || 'edit');
const EDIT_INSETS = { insetLeft: 232, insetTop: 0, insetBottom: 72, padding: 28 };
const deckPath = process.argv[2] || 'examples/kitchen-sink/index.html';
const screenshotPath = process.env.WEBSLIDES_TEST_SCREENSHOT || `/tmp/webslides-${layoutMode}-layout.png`;
const chromePath = process.env.CHROME ||
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

class CDP {
    constructor(wsURL) {
        this.ws = new WebSocket(wsURL);
        this.nextId = 1;
        this.pending = new Map();
        this.listeners = new Map();
        this.ws.addEventListener('message', (event) => {
            const msg = JSON.parse(event.data);
            if (msg.id && this.pending.has(msg.id)) {
                const { resolve, reject } = this.pending.get(msg.id);
                this.pending.delete(msg.id);
                if (msg.error) reject(new Error(JSON.stringify(msg.error)));
                else resolve(msg.result || {});
                return;
            }
            const handlers = this.listeners.get(msg.method) || [];
            handlers.forEach((handler) => handler(msg.params || {}));
        });
    }

    async open() {
        if (this.ws.readyState === WebSocket.OPEN) return;
        await new Promise((resolve, reject) => {
            this.ws.addEventListener('open', resolve, { once: true });
            this.ws.addEventListener('error', reject, { once: true });
        });
    }

    send(method, params = {}) {
        const id = this.nextId++;
        this.ws.send(JSON.stringify({ id, method, params }));
        return new Promise((resolve, reject) => {
            this.pending.set(id, { resolve, reject });
        });
    }

    once(method) {
        return new Promise((resolve) => {
            const handlers = this.listeners.get(method) || [];
            handlers.push(resolve);
            this.listeners.set(method, handlers);
        });
    }

    on(method, handler) {
        const handlers = this.listeners.get(method) || [];
        handlers.push(handler);
        this.listeners.set(method, handlers);
    }

    close() {
        this.ws.close();
    }
}

function contentType(filePath) {
    if (filePath.endsWith('.html')) return 'text/html';
    if (filePath.endsWith('.js')) return 'application/javascript';
    if (filePath.endsWith('.css')) return 'text/css';
    if (filePath.endsWith('.svg')) return 'image/svg+xml';
    if (filePath.endsWith('.json')) return 'application/json';
    if (filePath.endsWith('.png')) return 'image/png';
    return 'application/octet-stream';
}

async function startServer() {
    const server = http.createServer(async (req, res) => {
        try {
            const rawPath = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
            const filePath = path.resolve(projectRoot, rawPath.slice(1) || deckPath);
            if (!filePath.startsWith(projectRoot)) {
                res.writeHead(403);
                res.end('Forbidden');
                return;
            }
            const data = await fs.readFile(filePath);
            res.writeHead(200, { 'Content-Type': contentType(filePath) });
            res.end(data);
        } catch {
            res.writeHead(404);
            res.end('Not found');
        }
    });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    return server;
}

function waitForDevTools(chrome) {
    return new Promise((resolve, reject) => {
        let stderr = '';
        const timer = setTimeout(() => reject(new Error('Timed out waiting for Chrome DevTools URL')), 10000);
        chrome.on('error', (error) => {
            clearTimeout(timer);
            reject(new Error(`Chrome failed to launch: ${error.message}`));
        });
        chrome.stderr.on('data', (chunk) => {
            stderr += chunk.toString();
            const match = stderr.match(/DevTools listening on (ws:\/\/[^\s]+)/);
            if (match) {
                clearTimeout(timer);
                resolve(match[1]);
            }
        });
        chrome.on('exit', (code) => {
            clearTimeout(timer);
            reject(new Error(`Chrome exited before DevTools was ready: ${code}\n${stderr}`));
        });
    });
}

async function newPage(browserWsURL, url) {
    const port = new URL(browserWsURL).port;
    const response = await fetch(
        `http://127.0.0.1:${port}/json/new?${encodeURIComponent(url)}`,
        { method: 'PUT' }
    );
    if (!response.ok) throw new Error(`Chrome /json/new failed: ${response.status}`);
    return response.json();
}

async function evaluate(client, expression) {
    const result = await client.send('Runtime.evaluate', {
        expression,
        awaitPromise: true,
        returnByValue: true
    });
    if (result.exceptionDetails) {
        throw new Error(JSON.stringify(result.exceptionDetails));
    }
    return result.result.value;
}

const layoutSource = require('node:fs').readFileSync(path.join(projectRoot, 'presto/edit-layout.js'), 'utf8');
const probeSource = require('node:fs').readFileSync(path.join(projectRoot, 'tests/engine-redesign-layout-probe.js'), 'utf8');

// Inject presto/edit-layout.js and the probe once the engine is up, as Presto
// injects the layout after the deck's DOMContentLoaded.
async function prepare(client) {
    await evaluate(client, `
        (async function() {
            await new Promise((resolve) => {
                (function check() {
                    if (window.WS && WS.slides && WS.slides.length && WS.ready) resolve();
                    else requestAnimationFrame(check);
                })();
            });
            await WS.ready;
            await document.fonts.ready;
            return true;
        })()
    `);
    await evaluate(client, `${layoutSource}\n;true`);
    await evaluate(client, `${probeSource}\n;true`);
    return evaluate(client, `(function() {
        const appendix = WS.appendixSlides.length ? WS.slides.indexOf(WS.appendixSlides[0]) : -1;
        return { appendix, hasNav: !!document.getElementById('ws-section-nav'), hasFooter: !!document.getElementById('ws-footer') };
    })()`);
}

async function setWindow(client, size) {
    await client.send('Emulation.setDeviceMetricsOverride', {
        width: size.width,
        height: size.height,
        deviceScaleFactor: 1,
        mobile: false
    });
    await evaluate(client, `new Promise((resolve) => (function wait(n) {
        if (window.innerWidth === ${size.width} && window.innerHeight === ${size.height} || n > 60) resolve(true);
        else requestAnimationFrame(() => wait(n + 1));
    })(0))`);
}

async function probe(client, cfg) {
    return evaluate(client, `window.__prestoLayoutProbe(${JSON.stringify(cfg)})`);
}

function report(result, size, cfg) {
    const g = result.geometry;
    const where = `${size.label} ${size.width}x${size.height} ${cfg.mode}` +
        (cfg.insetRight ? ` +inspector` : '');
    if (result.failures.length) {
        throw new Error(`${where}: ${result.failures.join('; ')}`);
    }
    const r = result.rects;
    console.log(`PASS ${result.kind}: ${where} slide ${result.index + 1}, scale=${g.scale.toFixed(3)}, ` +
        `slide=${r.slide.left.toFixed(1)},${r.slide.top.toFixed(1)} ${r.slide.width.toFixed(1)}x${r.slide.height.toFixed(1)}` +
        (r.nav ? `, nav top=${r.nav.top.toFixed(1)}` : '') +
        (r.footer ? `, footer bottom=${r.footer.bottom.toFixed(1)}` : ''));
}

async function main() {
    let server;
    let userDataDir;
    let chrome;
    let client;
    try {
        server = await startServer();
        const url = `http://127.0.0.1:${server.address().port}/${deckPath}`;
        userDataDir = await fs.mkdtemp(path.join(os.tmpdir(), 'webslides-chrome-'));
        chrome = spawn(chromePath, [
            '--headless=new',
            '--disable-gpu',
            '--no-first-run',
            '--no-default-browser-check',
            `--user-data-dir=${userDataDir}`,
            '--remote-debugging-port=0',
            `--window-size=${sizes[0].width},${sizes[0].height}`,
            'about:blank'
        ], { stdio: ['ignore', 'ignore', 'pipe'] });

        const browserWsURL = await waitForDevTools(chrome);
        const target = await newPage(browserWsURL, url);
        client = new CDP(target.webSocketDebuggerUrl);
        await client.open();
        await client.send('Page.enable');
        await client.send('Runtime.enable');
        const runtimeErrors = [];
        client.on('Runtime.exceptionThrown', (params) => runtimeErrors.push(params));

        // [page query, layout configs]; preview runs the audience layout and
        // the console's ?mode=preview page.
        const runs = layoutMode === 'edit'
            ? [['', [Object.assign({ mode: 'edit', insetRight: 0 }, EDIT_INSETS),
                     Object.assign({ mode: 'edit', insetRight: 360 }, EDIT_INSETS)]]]
            : [['', [{ mode: 'audience' }]], ['?mode=preview', [{ mode: 'preview' }]]];
        let checked = 0;
        for (const [query, configs] of runs) {
            await setWindow(client, sizes[0]);
            const loaded = client.once('Page.loadEventFired');
            await client.send('Page.navigate', { url: url + query });
            await loaded;
            const deck = await prepare(client);
            if (!deck.hasNav || !deck.hasFooter) throw new Error('fixture deck needs a section nav and a footer');
            const slides = [0, 2, 3].concat(deck.appendix >= 0 ? [deck.appendix] : []);
            for (const size of sizes) {
                await setWindow(client, size);
                for (const cfg of configs) {
                    const out = await probe(client, Object.assign({ slides }, cfg));
                    if (out.viewport.width !== size.width || out.viewport.height !== size.height) {
                        throw new Error(`window is ${out.viewport.width}x${out.viewport.height}, wanted ${size.width}x${size.height}`);
                    }
                    out.results.forEach((result) => { report(result, size, cfg); checked++; });
                }
            }
        }
        if (runtimeErrors.length) {
            const detail = runtimeErrors[0].exceptionDetails?.text || 'unknown runtime error';
            throw new Error(`Uncaught runtime exception: ${detail}`);
        }
        console.log(`PASS ${checked} slide layouts (${layoutMode})`);

        const screenshot = await client.send('Page.captureScreenshot', { format: 'png' });
        await fs.writeFile(screenshotPath, Buffer.from(screenshot.data, 'base64'));
        console.log(`PASS screenshot: ${screenshotPath}`);
    } finally {
        if (client) client.close();
        if (chrome) chrome.kill('SIGKILL');
        if (server) await new Promise((resolve) => server.close(resolve));
        if (userDataDir) spawnSync('trash', [userDataDir], { stdio: 'ignore' });
    }
}

main().catch((error) => {
    console.error(`FAIL ${error.message}`);
    process.exit(1);
});
