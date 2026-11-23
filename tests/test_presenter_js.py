#!/usr/bin/env python3
import re
import subprocess
import textwrap
import unittest
from pathlib import Path


class PresenterJSTests(unittest.TestCase):
    def presenter_js_path(self):
        return Path(__file__).resolve().parents[1] / "presto" / "presenter.js"

    def test_timer_message_channel_is_paced_instead_of_busy_looping(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const vm = require('vm');
            let timerClick = null;
            let channel = null;
            let posted = 0;
            const scheduled = [];
            const element = (id) => ({{
              textContent: '',
              title: '',
              style: {{}},
              classList: {{ add() {{}}, remove() {{}} }},
              addEventListener(event, handler) {{
                if (id === 'timer-btn' && event === 'click') timerClick = handler;
              }},
              querySelector() {{ return null; }},
              clientWidth: 1280,
              clientHeight: 720
            }});
            const elements = new Map();
            const context = {{
              window: {{}},
              document: {{
                getElementById(id) {{
                  if (!elements.has(id)) elements.set(id, element(id));
                  return elements.get(id);
                }},
                querySelectorAll() {{ return []; }},
                addEventListener() {{}},
                body: {{ style: {{}} }}
              }},
              MessageChannel: class {{
                constructor() {{
                  this.port1 = {{}};
                  this.port2 = {{ postMessage() {{ posted += 1; }} }};
                  channel = this;
                }}
              }},
              setTimeout(fn, delay) {{ scheduled.push({{ fn, delay }}); }},
              Date,
              URLSearchParams,
              console
            }};
            context.window.addEventListener = () => {{}};
            context.window.innerWidth = 1280;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync({str(self.presenter_js_path())!r}, 'utf8'), context);

            if (!timerClick) throw new Error('timer click handler missing');
            timerClick();
            if (posted !== 1) throw new Error('timer should post exactly once when started');
            channel.port1.onmessage();
            if (posted !== 1) throw new Error('timer channel reposted synchronously');
            if (scheduled.length !== 1 || scheduled[0].delay < 200) {{
              throw new Error('timer channel was not paced with a bounded delay');
            }}
            scheduled.shift().fn();
            if (posted !== 2) throw new Error('paced timer poll did not repost');
            """
        )
        subprocess.run(["node", "-e", script], check=True)

    def test_init_presenter_attaches_load_listener_before_setting_iframe_src(self):
        presenter_js = self.presenter_js_path().read_text(encoding="utf-8")
        match = re.search(
            r"window\.initPresenter\s*=\s*function\s*\([^)]*\)\s*\{(?P<body>.*?)\n\s*\};",
            presenter_js,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "initPresenter function not found")
        body = match.group("body")
        listener_pos = body.find("currentIframe.addEventListener('load', onIframeLoad")
        src_pos = body.find("currentIframe.src =")
        self.assertGreaterEqual(listener_pos, 0, "load listener not found")
        self.assertGreaterEqual(src_pos, 0, "iframe src assignment not found")
        self.assertLess(
            listener_pos,
            src_pos,
            "initPresenter must attach the load listener before setting iframe src",
        )

    def test_preview_url_preserves_existing_query_and_hash(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const vm = require('vm');
            const element = () => ({{
              textContent: '',
              title: '',
              style: {{}},
              classList: {{ add() {{}}, remove() {{}} }},
              addEventListener() {{}},
              querySelector() {{ return null; }},
              clientWidth: 1280,
              clientHeight: 720
            }});
            const context = {{
              window: {{}},
              document: {{
                getElementById() {{ return element(); }},
                querySelectorAll() {{ return []; }},
                addEventListener() {{}},
                body: {{ style: {{}} }}
              }},
              MessageChannel: class {{
                constructor() {{
                  this.port1 = {{}};
                  this.port2 = {{ postMessage() {{}} }};
                }}
              }},
              Date,
              URLSearchParams,
              console
            }};
            context.window.addEventListener = () => {{}};
            context.window.innerWidth = 1280;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync({str(self.presenter_js_path())!r}, 'utf8'), context);
            const built = context.window.buildPresenterPreviewURL('file:///deck/index.html?slide=3#appendix');
            if (built !== 'file:///deck/index.html?slide=3&mode=preview#appendix') {{
              throw new Error('unexpected preview URL: ' + built);
            }}
            const replaced = context.window.buildPresenterPreviewURL('file:///deck/index.html?mode=audience&slide=3#appendix');
            if (replaced !== 'file:///deck/index.html?slide=3&mode=preview#appendix') {{
              throw new Error('unexpected replaced preview URL: ' + replaced);
            }}
            """
        )
        subprocess.run(["node", "-e", script], check=True)

    def test_init_presenter_warns_when_preview_mode_setup_fails(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const vm = require('vm');
            let iframeLoadHandler = null;
            let warnings = [];
            const element = () => ({{
              textContent: '',
              title: '',
              style: {{}},
              classList: {{ add() {{}}, remove() {{}} }},
              addEventListener() {{}},
              querySelector() {{ return null; }},
              clientWidth: 1280,
              clientHeight: 720
            }});
            const iframe = element();
            iframe.addEventListener = (event, handler) => {{
              if (event === 'load') iframeLoadHandler = handler;
            }};
            Object.defineProperty(iframe, 'contentWindow', {{
              get() {{ throw new Error('blocked frame'); }}
            }});
            const context = {{
              window: {{}},
              document: {{
                getElementById(id) {{
                  return id === 'current-slide' ? iframe : element();
                }},
                querySelectorAll() {{ return []; }},
                addEventListener() {{}},
                body: {{ style: {{}} }}
              }},
              MessageChannel: class {{
                constructor() {{
                  this.port1 = {{}};
                  this.port2 = {{ postMessage() {{}} }};
                }}
              }},
              Date,
              URLSearchParams,
              setTimeout(fn) {{ fn(); }},
              console: {{
                warn(...args) {{ warnings.push(args.join(' ')); }}
              }}
            }};
            context.window.addEventListener = () => {{}};
            context.window.innerWidth = 1280;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync({str(self.presenter_js_path())!r}, 'utf8'), context);
            context.window.initPresenter('file:///deck/index.html');
            if (!iframeLoadHandler) throw new Error('load handler was not attached');
            iframeLoadHandler();
            if (!warnings.some((message) => message.includes('failed to initialize preview iframe'))) {{
              throw new Error('preview iframe setup failure was not warned');
            }}
            """
        )
        subprocess.run(["node", "-e", script], check=True)

    def test_init_presenter_does_not_signal_ready_before_preview_engine_exists(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const vm = require('vm');
            let iframeLoadHandler = null;
            let warnings = [];
            let messages = [];
            const element = () => ({{
              textContent: '',
              title: '',
              style: {{}},
              classList: {{ add() {{}}, remove() {{}} }},
              addEventListener() {{}},
              querySelector() {{ return null; }},
              clientWidth: 1280,
              clientHeight: 720
            }});
            const iframe = element();
            iframe.addEventListener = (event, handler) => {{
              if (event === 'load') iframeLoadHandler = handler;
            }};
            iframe.contentWindow = {{}};
            const context = {{
              window: {{
                webkit: {{ messageHandlers: {{ presto: {{
                  postMessage(message) {{ messages.push(message); }}
                }} }} }}
              }},
              document: {{
                getElementById(id) {{
                  return id === 'current-slide' ? iframe : element();
                }},
                querySelectorAll() {{ return []; }},
                addEventListener() {{}},
                body: {{ style: {{}} }}
              }},
              MessageChannel: class {{
                constructor() {{
                  this.port1 = {{}};
                  this.port2 = {{ postMessage() {{}} }};
                }}
              }},
              Date,
              URLSearchParams,
              setTimeout(fn) {{ fn(); }},
              console: {{
                warn(...args) {{ warnings.push(args.join(' ')); }}
              }}
            }};
            context.window.addEventListener = () => {{}};
            context.window.innerWidth = 1280;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync({str(self.presenter_js_path())!r}, 'utf8'), context);
            context.window.initPresenter('file:///deck/index.html');
            if (!iframeLoadHandler) throw new Error('load handler was not attached');
            iframeLoadHandler();
            if (messages.some((message) => message.type === 'ready')) {{
              throw new Error('presenter signaled ready before preview WS existed');
            }}
            if (!warnings.some((message) => message.includes('preview iframe did not become ready'))) {{
              throw new Error('missing warning for preview iframe without WS');
            }}
            """
        )
        subprocess.run(["node", "-e", script], check=True)

    def test_presenter_update_ignores_malformed_native_state(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const vm = require('vm');
            let warnings = [];
            let goToSlideCalls = [];
            const element = () => ({{
              textContent: '',
              title: '',
              style: {{}},
              classList: {{ add() {{}}, remove() {{}} }},
              addEventListener() {{}},
              querySelector() {{ return null; }},
              clientWidth: 1280,
              clientHeight: 720
            }});
            const slideCounter = element();
            const slideCounterTotal = element();
            const counterText = () => (slideCounter.textContent + ' ' + slideCounterTotal.textContent).trim();
            const iframe = element();
            iframe.contentWindow = {{
              WS: {{
                goToSlide(index, step) {{ goToSlideCalls.push([index, step]); }}
              }}
            }};
            const context = {{
              window: {{}},
              document: {{
                getElementById(id) {{
                  if (id === 'current-slide') return iframe;
                  if (id === 'slide-counter-num') return slideCounter;
                  if (id === 'slide-counter-total') return slideCounterTotal;
                  return element();
                }},
                querySelectorAll() {{ return []; }},
                addEventListener() {{}},
                body: {{ style: {{}} }}
              }},
              MessageChannel: class {{
                constructor() {{
                  this.port1 = {{}};
                  this.port2 = {{ postMessage() {{}} }};
                }}
              }},
              Date,
              URLSearchParams,
              console: {{
                warn(...args) {{ warnings.push(args.join(' ')); }}
              }}
            }};
            context.window.addEventListener = () => {{}};
            context.window.innerWidth = 1280;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync({str(self.presenter_js_path())!r}, 'utf8'), context);

            context.window.presenterUpdate(null);
            context.window.presenterUpdate('bad-state');

            if (goToSlideCalls.length !== 0) {{
              throw new Error('malformed presenter state should not navigate the preview iframe');
            }}
            if (counterText() !== '') {{
              throw new Error('malformed presenter state should not overwrite slide counter');
            }}
            if (!warnings.some((message) => message.includes('ignored malformed presenter update'))) {{
              throw new Error('missing warning for malformed presenter update');
            }}
            """
        )
        subprocess.run(["node", "-e", script], check=True)

    def test_presenter_update_ignores_non_integer_or_negative_native_state(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const vm = require('vm');
            let warnings = [];
            let goToSlideCalls = [];
            const element = () => ({{
              textContent: '',
              title: '',
              style: {{}},
              classList: {{ add() {{}}, remove() {{}} }},
              addEventListener() {{}},
              querySelector() {{ return null; }},
              clientWidth: 1280,
              clientHeight: 720
            }});
            const slideCounter = element();
            const slideCounterTotal = element();
            const counterText = () => (slideCounter.textContent + ' ' + slideCounterTotal.textContent).trim();
            const iframe = element();
            iframe.contentWindow = {{
              WS: {{
                goToSlide(index, step) {{ goToSlideCalls.push([index, step]); }}
              }}
            }};
            const context = {{
              window: {{}},
              document: {{
                getElementById(id) {{
                  if (id === 'current-slide') return iframe;
                  if (id === 'slide-counter-num') return slideCounter;
                  if (id === 'slide-counter-total') return slideCounterTotal;
                  return element();
                }},
                querySelectorAll() {{ return []; }},
                addEventListener() {{}},
                body: {{ style: {{}} }}
              }},
              MessageChannel: class {{
                constructor() {{
                  this.port1 = {{}};
                  this.port2 = {{ postMessage() {{}} }};
                }}
              }},
              Date,
              URLSearchParams,
              console: {{
                warn(...args) {{ warnings.push(args.join(' ')); }}
              }}
            }};
            context.window.addEventListener = () => {{}};
            context.window.innerWidth = 1280;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync({str(self.presenter_js_path())!r}, 'utf8'), context);

            context.window.presenterUpdate({{ index: 1.5, step: 0 }});
            context.window.presenterUpdate({{ index: -1, step: 0 }});
            context.window.presenterUpdate({{ index: 1, step: -1 }});
            context.window.presenterUpdate({{ index: 1, step: 0.5 }});

            if (goToSlideCalls.length !== 0) {{
              throw new Error('invalid presenter state should not navigate the preview iframe');
            }}
            if (counterText() !== '') {{
              throw new Error('invalid presenter state should not overwrite slide counter');
            }}
            if (warnings.filter((message) => message.includes('ignored malformed presenter update')).length !== 4) {{
              throw new Error('missing warnings for invalid presenter updates: ' + warnings.join('\\n'));
            }}
            """
        )
        subprocess.run(["node", "-e", script], check=True)

    def test_presenter_update_ignores_boolean_native_state_numbers(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const vm = require('vm');
            let warnings = [];
            let goToSlideCalls = [];
            const element = () => ({{
              textContent: '',
              title: '',
              style: {{}},
              classList: {{ add() {{}}, remove() {{}} }},
              addEventListener() {{}},
              querySelector() {{ return null; }},
              clientWidth: 1280,
              clientHeight: 720
            }});
            const slideCounter = element();
            const slideCounterTotal = element();
            const counterText = () => (slideCounter.textContent + ' ' + slideCounterTotal.textContent).trim();
            const iframe = element();
            iframe.contentWindow = {{
              WS: {{
                goToSlide(index, step) {{ goToSlideCalls.push([index, step]); }}
              }}
            }};
            const context = {{
              window: {{}},
              document: {{
                getElementById(id) {{
                  if (id === 'current-slide') return iframe;
                  if (id === 'slide-counter-num') return slideCounter;
                  if (id === 'slide-counter-total') return slideCounterTotal;
                  return element();
                }},
                querySelectorAll() {{ return []; }},
                addEventListener() {{}},
                body: {{ style: {{}} }}
              }},
              MessageChannel: class {{
                constructor() {{
                  this.port1 = {{}};
                  this.port2 = {{ postMessage() {{}} }};
                }}
              }},
              Date,
              URLSearchParams,
              console: {{
                warn(...args) {{ warnings.push(args.join(' ')); }}
              }}
            }};
            context.window.addEventListener = () => {{}};
            context.window.innerWidth = 1280;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync({str(self.presenter_js_path())!r}, 'utf8'), context);

            context.window.presenterUpdate({{ index: true, step: 0 }});
            context.window.presenterUpdate({{ index: 1, step: false }});

            if (goToSlideCalls.length !== 0) {{
              throw new Error('boolean presenter state should not navigate the preview iframe');
            }}
            if (counterText() !== '') {{
              throw new Error('boolean presenter state should not overwrite slide counter');
            }}
            if (warnings.filter((message) => message.includes('ignored malformed presenter update')).length !== 2) {{
              throw new Error('missing warnings for boolean presenter updates: ' + warnings.join('\\n'));
            }}
            """
        )
        subprocess.run(["node", "-e", script], check=True)

    def test_presenter_update_rejects_string_index_and_step_values(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const vm = require('vm');
            let warnings = [];
            let goToSlideCalls = [];
            const element = () => ({{
              textContent: '',
              title: '',
              style: {{}},
              classList: {{ add() {{}}, remove() {{}} }},
              addEventListener() {{}},
              querySelector() {{ return null; }},
              clientWidth: 1280,
              clientHeight: 720
            }});
            const slideCounter = element();
            const slideCounterTotal = element();
            const counterText = () => (slideCounter.textContent + ' ' + slideCounterTotal.textContent).trim();
            const iframe = element();
            iframe.contentWindow = {{
              WS: {{
                goToSlide(index, step) {{ goToSlideCalls.push([index, step]); }}
              }}
            }};
            const context = {{
              window: {{}},
              document: {{
                getElementById(id) {{
                  if (id === 'current-slide') return iframe;
                  if (id === 'slide-counter-num') return slideCounter;
                  if (id === 'slide-counter-total') return slideCounterTotal;
                  return element();
                }},
                querySelectorAll() {{ return []; }},
                addEventListener() {{}},
                body: {{ style: {{}} }}
              }},
              MessageChannel: class {{
                constructor() {{
                  this.port1 = {{}};
                  this.port2 = {{ postMessage() {{}} }};
                }}
              }},
              Date,
              URLSearchParams,
              console: {{
                warn(...args) {{ warnings.push(args.join(' ')); }}
              }}
            }};
            context.window.addEventListener = () => {{}};
            context.window.innerWidth = 1280;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync({str(self.presenter_js_path())!r}, 'utf8'), context);

            context.window.presenterUpdate({{ index: '1', step: 0 }});
            context.window.presenterUpdate({{ index: 1, step: '0' }});

            if (goToSlideCalls.length !== 0) {{
              throw new Error('string presenter state should not navigate the preview iframe');
            }}
            if (counterText() !== '') {{
              throw new Error('string presenter state should not overwrite slide counter');
            }}
            if (warnings.filter((message) => message.includes('ignored malformed presenter update')).length !== 2) {{
              throw new Error('missing warnings for string presenter updates: ' + warnings.join('\\n'));
            }}
            """
        )
        subprocess.run(["node", "-e", script], check=True)

    def test_presenter_update_rejects_non_boolean_appendix_flag(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const vm = require('vm');
            let warnings = [];
            let goToSlideCalls = [];
            const element = () => ({{
              textContent: '',
              title: '',
              style: {{}},
              classList: {{ add() {{}}, remove() {{}} }},
              addEventListener() {{}},
              querySelector() {{ return null; }},
              clientWidth: 1280,
              clientHeight: 720
            }});
            const slideCounter = element();
            const slideCounterTotal = element();
            const counterText = () => (slideCounter.textContent + ' ' + slideCounterTotal.textContent).trim();
            const iframe = element();
            iframe.contentWindow = {{
              WS: {{
                goToSlide(index, step) {{ goToSlideCalls.push([index, step]); }}
              }}
            }};
            const context = {{
              window: {{}},
              document: {{
                getElementById(id) {{
                  if (id === 'current-slide') return iframe;
                  if (id === 'slide-counter-num') return slideCounter;
                  if (id === 'slide-counter-total') return slideCounterTotal;
                  return element();
                }},
                querySelectorAll() {{ return []; }},
                addEventListener() {{}},
                body: {{ style: {{}} }}
              }},
              MessageChannel: class {{
                constructor() {{
                  this.port1 = {{}};
                  this.port2 = {{ postMessage() {{}} }};
                }}
              }},
              Date,
              URLSearchParams,
              console: {{
                warn(...args) {{ warnings.push(args.join(' ')); }}
              }}
            }};
            context.window.addEventListener = () => {{}};
            context.window.innerWidth = 1280;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync({str(self.presenter_js_path())!r}, 'utf8'), context);

            context.window.presenterUpdate({{ index: 1, step: 0, isAppendix: 'false', slideNumber: 2, mainSlides: 10 }});

            if (goToSlideCalls.length !== 0) {{
              throw new Error('invalid appendix flag should not navigate the preview iframe');
            }}
            if (counterText() !== '') {{
              throw new Error('invalid appendix flag should not overwrite slide counter');
            }}
            if (!warnings.some((message) => message.includes('ignored malformed presenter update'))) {{
              throw new Error('missing warning for invalid appendix flag');
            }}
            """
        )
        subprocess.run(["node", "-e", script], check=True)

    def test_presenter_update_rejects_index_beyond_reported_total_slides(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const vm = require('vm');
            let warnings = [];
            let goToSlideCalls = [];
            const element = () => ({{
              textContent: '',
              title: '',
              style: {{}},
              classList: {{ add() {{}}, remove() {{}} }},
              addEventListener() {{}},
              querySelector() {{ return null; }},
              clientWidth: 1280,
              clientHeight: 720
            }});
            const slideCounter = element();
            const slideCounterTotal = element();
            const counterText = () => (slideCounter.textContent + ' ' + slideCounterTotal.textContent).trim();
            const iframe = element();
            iframe.contentWindow = {{
              WS: {{
                goToSlide(index, step) {{ goToSlideCalls.push([index, step]); }}
              }}
            }};
            const context = {{
              window: {{}},
              document: {{
                getElementById(id) {{
                  if (id === 'current-slide') return iframe;
                  if (id === 'slide-counter-num') return slideCounter;
                  if (id === 'slide-counter-total') return slideCounterTotal;
                  return element();
                }},
                querySelectorAll() {{ return []; }},
                addEventListener() {{}},
                body: {{ style: {{}} }}
              }},
              MessageChannel: class {{
                constructor() {{
                  this.port1 = {{}};
                  this.port2 = {{ postMessage() {{}} }};
                }}
              }},
              Date,
              URLSearchParams,
              console: {{
                warn(...args) {{ warnings.push(args.join(' ')); }}
              }}
            }};
            context.window.addEventListener = () => {{}};
            context.window.innerWidth = 1280;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync({str(self.presenter_js_path())!r}, 'utf8'), context);

            context.window.presenterUpdate({{ index: 99, step: 0, totalSlides: 3, isAppendix: false, slideNumber: 100, mainSlides: 3 }});

            if (goToSlideCalls.length !== 0) {{
              throw new Error('out-of-range presenter state should not navigate the preview iframe');
            }}
            if (counterText() !== '') {{
              throw new Error('out-of-range presenter state should not overwrite slide counter');
            }}
            if (!warnings.some((message) => message.includes('ignored malformed presenter update'))) {{
              throw new Error('missing warning for out-of-range presenter update');
            }}
            """
        )
        subprocess.run(["node", "-e", script], check=True)

    def test_presenter_update_falls_back_from_non_positive_slide_counts(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const vm = require('vm');
            const element = () => ({{
              textContent: '',
              title: '',
              style: {{}},
              classList: {{ add() {{}}, remove() {{}} }},
              addEventListener() {{}},
              querySelector() {{ return null; }},
              clientWidth: 1280,
              clientHeight: 720
            }});
            const slideCounter = element();
            const slideCounterTotal = element();
            const counterText = () => (slideCounter.textContent + ' ' + slideCounterTotal.textContent).trim();
            const iframe = element();
            iframe.contentWindow = {{
              WS: {{ goToSlide() {{}} }}
            }};
            const context = {{
              window: {{}},
              document: {{
                getElementById(id) {{
                  if (id === 'current-slide') return iframe;
                  if (id === 'slide-counter-num') return slideCounter;
                  if (id === 'slide-counter-total') return slideCounterTotal;
                  return element();
                }},
                querySelectorAll() {{ return []; }},
                addEventListener() {{}},
                body: {{ style: {{}} }}
              }},
              MessageChannel: class {{
                constructor() {{
                  this.port1 = {{}};
                  this.port2 = {{ postMessage() {{}} }};
                }}
              }},
              Date,
              URLSearchParams,
              console
            }};
            context.window.addEventListener = () => {{}};
            context.window.innerWidth = 1280;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync({str(self.presenter_js_path())!r}, 'utf8'), context);

            context.window.presenterUpdate({{ index: 1, step: 0, isAppendix: false, slideNumber: 0, mainSlides: 0 }});

            // Without a usable main-slide total the counter shows the number alone.
            if (counterText() !== '2') {{
              throw new Error('unexpected slide counter fallback: ' + counterText());
            }}
            """
        )
        subprocess.run(["node", "-e", script], check=True)

    def test_presenter_update_falls_back_from_impossible_slide_number_above_total(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const vm = require('vm');
            const goToSlideCalls = [];
            const element = () => ({{
              textContent: '',
              title: '',
              style: {{}},
              classList: {{ add() {{}}, remove() {{}} }},
              addEventListener() {{}},
              querySelector() {{ return null; }},
              clientWidth: 1280,
              clientHeight: 720
            }});
            const slideCounter = element();
            const slideCounterTotal = element();
            const counterText = () => (slideCounter.textContent + ' ' + slideCounterTotal.textContent).trim();
            const iframe = element();
            iframe.contentWindow = {{
              WS: {{ goToSlide(index, step) {{ goToSlideCalls.push([index, step]); }} }}
            }};
            const context = {{
              window: {{}},
              document: {{
                getElementById(id) {{
                  if (id === 'current-slide') return iframe;
                  if (id === 'slide-counter-num') return slideCounter;
                  if (id === 'slide-counter-total') return slideCounterTotal;
                  return element();
                }},
                querySelectorAll() {{ return []; }},
                addEventListener() {{}},
                body: {{ style: {{}} }}
              }},
              MessageChannel: class {{
                constructor() {{
                  this.port1 = {{}};
                  this.port2 = {{ postMessage() {{}} }};
                }}
              }},
              Date,
              URLSearchParams,
              console
            }};
            context.window.addEventListener = () => {{}};
            context.window.innerWidth = 1280;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync({str(self.presenter_js_path())!r}, 'utf8'), context);

            context.window.presenterUpdate({{ index: 1, step: 0, isAppendix: false, slideNumber: 9, mainSlides: 3 }});

            if (goToSlideCalls.length !== 1 || goToSlideCalls[0][0] !== 1 || goToSlideCalls[0][1] !== 0) {{
              throw new Error('valid navigation state should still update the preview iframe');
            }}
            if (counterText() !== '2 / 3') {{
              throw new Error('unexpected slide counter fallback: ' + counterText());
            }}
            """
        )
        subprocess.run(["node", "-e", script], check=True)

    def test_presenter_update_accepts_string_slide_display_counts_from_engine_state(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const vm = require('vm');
            const element = () => ({{
              textContent: '',
              title: '',
              style: {{}},
              classList: {{ add() {{}}, remove() {{}} }},
              addEventListener() {{}},
              querySelector() {{ return null; }},
              clientWidth: 1280,
              clientHeight: 720
            }});
            const slideCounter = element();
            const slideCounterTotal = element();
            const counterText = () => (slideCounter.textContent + ' ' + slideCounterTotal.textContent).trim();
            const iframe = element();
            iframe.contentWindow = {{
              WS: {{ goToSlide() {{}} }}
            }};
            const context = {{
              window: {{}},
              document: {{
                getElementById(id) {{
                  if (id === 'current-slide') return iframe;
                  if (id === 'slide-counter-num') return slideCounter;
                  if (id === 'slide-counter-total') return slideCounterTotal;
                  return element();
                }},
                querySelectorAll() {{ return []; }},
                addEventListener() {{}},
                body: {{ style: {{}} }}
              }},
              MessageChannel: class {{
                constructor() {{
                  this.port1 = {{}};
                  this.port2 = {{ postMessage() {{}} }};
                }}
              }},
              Date,
              URLSearchParams,
              console
            }};
            context.window.addEventListener = () => {{}};
            context.window.innerWidth = 1280;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync({str(self.presenter_js_path())!r}, 'utf8'), context);

            context.window.presenterUpdate({{ index: 3, step: 0, isAppendix: false, slideNumber: '2', mainSlides: '10' }});

            if (counterText() !== '2 / 10') {{
              throw new Error('unexpected slide counter for string counts: ' + counterText());
            }}
            """
        )
        subprocess.run(["node", "-e", script], check=True)

    def test_presenter_view_navigation_is_reported_without_echoing_native_updates(self):
        # Appendix/back clicks and overview picks in the presenter view must move
        # the audience; Presto's own mirror updates must not bounce back.
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const vm = require('vm');
            let iframeLoadHandler = null;
            const messages = [];
            const element = () => ({{
              textContent: '',
              title: '',
              style: {{}},
              classList: {{ add() {{}}, remove() {{}} }},
              addEventListener() {{}},
              querySelector() {{ return null; }},
              clientWidth: 1280,
              clientHeight: 720
            }});
            const listeners = [];
            const previewWS = {{
              mode: 'preview',
              slides: [1, 2, 3, 4, 5, 6, 7, 8],
              returnStack: [],
              currentIndex: 0,
              currentStep: 0,
              on(event, cb) {{ if (event === 'navigate') listeners.push(cb); }},
              emit() {{ listeners.forEach((cb) => cb({{ index: this.currentIndex, step: this.currentStep }})); }},
              goToSlide(index, step) {{ this.currentIndex = index; this.currentStep = step; this.emit(); }},
              goToAppendix(index) {{
                this.returnStack.push({{ index: this.currentIndex, step: this.currentStep }});
                this.goToSlide(index, 0);
              }}
            }};
            const iframe = element();
            iframe.addEventListener = (event, handler) => {{ if (event === 'load') iframeLoadHandler = handler; }};
            iframe.contentWindow = {{ WS: previewWS, document: {{ body: {{ classList: {{ add() {{}} }} }} }} }};
            const context = {{
              window: {{
                webkit: {{ messageHandlers: {{ presto: {{ postMessage(m) {{ messages.push(m); }} }} }} }}
              }},
              document: {{
                getElementById(id) {{ return id === 'current-slide' ? iframe : element(); }},
                querySelectorAll() {{ return []; }},
                addEventListener() {{}},
                body: {{ style: {{}} }}
              }},
              MessageChannel: class {{
                constructor() {{ this.port1 = {{}}; this.port2 = {{ postMessage() {{}} }}; }}
              }},
              Date,
              URLSearchParams,
              setTimeout(fn) {{ fn(); }},
              console
            }};
            context.window.addEventListener = () => {{}};
            context.window.innerWidth = 1280;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync({str(self.presenter_js_path())!r}, 'utf8'), context);
            context.window.initPresenter('file:///deck/index.html');
            iframeLoadHandler();
            const navigated = () => messages.filter((m) => m.type === 'presenterNavigated');

            context.window.presenterUpdate({{ index: 2, step: 1, returnStack: [] }});
            if (navigated().length !== 0) throw new Error('native mirror update was echoed back to Presto');

            previewWS.goToAppendix(7);
            const jump = navigated();
            if (jump.length !== 1) throw new Error('appendix click in the presenter view was not reported');
            if (jump[0].index !== 7 || jump[0].step !== 0) throw new Error('wrong reported target ' + JSON.stringify(jump[0]));
            if (JSON.stringify(jump[0].returnStack) !== JSON.stringify([{{ index: 2, step: 1 }}])) {{
              throw new Error('return stack not reported: ' + JSON.stringify(jump[0].returnStack));
            }}

            context.window.presenterUpdate({{ index: 7, step: 0, returnStack: [{{ index: 4, step: 2 }}] }});
            if (navigated().length !== 1) throw new Error('mirror update after a jump was echoed back');
            if (JSON.stringify(previewWS.returnStack) !== JSON.stringify([{{ index: 4, step: 2 }}])) {{
              throw new Error('audience return stack was not mirrored into the presenter view');
            }}

            previewWS.goToSlide(5, 0);  // overview pick
            if (navigated().length !== 2 || navigated()[1].index !== 5) {{
              throw new Error('overview selection in the presenter view was not reported');
            }}
            """
        )
        subprocess.run(["node", "-e", script], check=True)


if __name__ == "__main__":
    unittest.main()
