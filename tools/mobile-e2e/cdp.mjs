#!/usr/bin/env node
// CDP client for the zcharlie mobile E2E harness. Reads DOM state and element
// rects from the app's debug WebView (Tauri debug builds expose remote
// debugging). Requires `adb forward tcp:9222 localabstract:webview_devtools_remote_<pid>`.
//
// Usage:
//   cdp.mjs eval '<js expression>'     print JSON value
//   cdp.mjs text '<selector>'          print textContent ("" when missing)
//   cdp.mjs lasttext '<selector>'      print textContent of the LAST matching element
//   cdp.mjs jsclick '<selector>'       el.click() on the LAST match (coordinate-free fallback
//                                      for elements that move while the list auto-scrolls)
//   cdp.mjs exists '<selector>'        print yes/no
//   cdp.mjs rect '<selector>'          print {x,y,w,h,dpr} CSS px (scrolls into view), exit 2 when missing
//   cdp.mjs insert '<selector>' <text> focus via JS, commit text as IME input (CJK-safe)
//   cdp.mjs clear '<selector>'         empty a text field (React-friendly: value setter + input event)

const CDP_HTTP = process.env.M2E_CDP ?? "http://127.0.0.1:9222";

async function pageWsUrl() {
  const pages = await (await fetch(`${CDP_HTTP}/json`)).json();
  const page = pages.find((entry) => entry.type === "page");
  if (!page) throw new Error("no debuggable page (app running? adb forward set?)");
  return page.webSocketDebuggerUrl;
}

function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let nextId = 0;
  const pending = new Map();
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.id && pending.has(msg.id)) {
      pending.get(msg.id)(msg);
      pending.delete(msg.id);
    }
  };
  const call = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const id = ++nextId;
      pending.set(id, (msg) =>
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result),
      );
      ws.send(JSON.stringify({ id, method, params }));
    });
  const ready = new Promise((resolve, reject) => {
    ws.onopen = () => resolve();
    ws.onerror = () => reject(new Error("websocket error"));
  });
  return { ready, call, close: () => ws.close() };
}

async function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  if (!cmd) throw new Error("usage: cdp.mjs <eval|text|exists|rect|insert> ...");
  const conn = connect(await pageWsUrl());
  await conn.ready;
  const evaluate = async (expression) => {
    const result = await conn.call("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description ?? "page exception");
    }
    return result.result?.value;
  };
  const selectorLiteral = JSON.stringify(rest[0] ?? "");

  switch (cmd) {
    case "eval": {
      console.log(JSON.stringify(await evaluate(rest[0])));
      break;
    }
    case "text": {
      const value = await evaluate(`document.querySelector(${selectorLiteral})?.textContent ?? ""`);
      console.log(value);
      break;
    }
    case "lasttext": {
      const value = await evaluate(
        `(() => { const list = document.querySelectorAll(${selectorLiteral}); return list.length ? list[list.length - 1].textContent : ""; })()`,
      );
      console.log(value);
      break;
    }
    case "lastrect": {
      await evaluate(
        `(() => { const list = document.querySelectorAll(${selectorLiteral}); if (list.length) list[list.length - 1].scrollIntoView({ block: "center" }); })()`,
      );
      const rect = await evaluate(`(() => {
        const list = document.querySelectorAll(${selectorLiteral});
        if (!list.length) return null;
        const b = list[list.length - 1].getBoundingClientRect();
        return { x: b.x, y: b.y, w: b.width, h: b.height, dpr: window.devicePixelRatio };
      })()`);
      if (!rect) process.exit(2);
      console.log(JSON.stringify(rect));
      break;
    }
    case "jsclick": {
      const clicked = await evaluate(`(() => {
        const list = document.querySelectorAll(${selectorLiteral});
        if (!list.length) return false;
        list[list.length - 1].click();
        return true;
      })()`);
      if (!clicked) process.exit(2);
      break;
    }
    case "exists": {
      const found = await evaluate(`document.querySelector(${selectorLiteral}) ? "yes" : "no"`);
      console.log(found);
      break;
    }
    case "rect": {
      await evaluate(
        `document.querySelector(${selectorLiteral})?.scrollIntoView({ block: "center" })`,
      );
      const rect = await evaluate(`(() => {
        const el = document.querySelector(${selectorLiteral});
        if (!el) return null;
        const b = el.getBoundingClientRect();
        return { x: b.x, y: b.y, w: b.width, h: b.height, dpr: window.devicePixelRatio };
      })()`);
      if (!rect) process.exit(2);
      console.log(JSON.stringify(rect));
      break;
    }
    case "clear": {
      const cleared = await evaluate(`(() => {
        const el = document.querySelector(${selectorLiteral});
        if (!el) return false;
        const proto = el instanceof HTMLTextAreaElement
          ? HTMLTextAreaElement.prototype
          : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, "value").set.call(el, "");
        el.dispatchEvent(new Event("input", { bubbles: true }));
        return true;
      })()`);
      if (!cleared) process.exit(2);
      break;
    }
    case "insert": {
      const text = rest[1] ?? "";
      const focused = await evaluate(`(() => {
        const el = document.querySelector(${selectorLiteral});
        if (!el) return false;
        el.focus();
        return document.activeElement === el;
      })()`);
      if (!focused) process.exit(2);
      await conn.call("Input.insertText", { text });
      break;
    }
    default:
      throw new Error(`unknown command: ${cmd}`);
  }
  conn.close();
}

main().catch((error) => {
  console.error(String(error));
  process.exit(1);
});
