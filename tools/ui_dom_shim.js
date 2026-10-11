/* tools/ui_dom_shim.js -- a minimal DOM shim so app.js can be *rendered* (not just parsed) under
 * node, which is how tests/test_wp3_ui_render.py checks the settings IA without a browser.
 * Implements exactly the surface app.js uses:
 * createElement/createTextNode, getElementById, querySelector(All) for simple selectors,
 * className/classList, textContent, dataset, set/removeAttribute, append/removeChild,
 * addEventListener/dispatch, value/checked/disabled/hidden, focus, and an HTML serializer.
 */
"use strict";

let DOC_ROOT = null;

function makeDom() {
  let uid = 0;

  class Node {
    constructor() {
      this.nodeType = 1;
      this.parentNode = null;
      this.childNodes = [];
      this._listeners = {};
    }
    get children() {
      return this.childNodes.filter((n) => n.nodeType === 1);
    }
    get firstChild() {
      return this.childNodes.length ? this.childNodes[0] : null;
    }
    get previousElementSibling() {
      if (!this.parentNode) return null;
      const siblings = this.parentNode.children;
      const i = siblings.indexOf(this);
      return i > 0 ? siblings[i - 1] : null;
    }
    get nextElementSibling() {
      if (!this.parentNode) return null;
      const siblings = this.parentNode.children;
      const i = siblings.indexOf(this);
      return i >= 0 && i + 1 < siblings.length ? siblings[i + 1] : null;
    }
    get isConnected() {
      let node = this;
      while (node.parentNode) node = node.parentNode;
      return node === DOC_ROOT;
    }
    contains(other) {
      let node = other;
      while (node) {
        if (node === this) return true;
        node = node.parentNode;
      }
      return false;
    }
    append(...nodes) {
      nodes.forEach((n) => this.appendChild(typeof n === "string" ? new Text(n) : n));
    }
    appendChild(child) {
      if (child.parentNode) child.parentNode.removeChild(child);
      child.parentNode = this;
      this.childNodes.push(child);
      return child;
    }
    removeChild(child) {
      const i = this.childNodes.indexOf(child);
      if (i >= 0) this.childNodes.splice(i, 1);
      child.parentNode = null;
      return child;
    }
    replaceChildren(...kids) {
      this.childNodes.slice().forEach((k) => this.removeChild(k));
      kids.forEach((k) => this.appendChild(k));
    }
    remove() {
      if (this.parentNode) this.parentNode.removeChild(this);
    }
    addEventListener(type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    }
    removeEventListener(type, fn) {
      const list = this._listeners[type] || [];
      const i = list.indexOf(fn);
      if (i >= 0) list.splice(i, 1);
    }
    dispatchEvent(evt) {
      const type = evt && evt.type ? evt.type : String(evt);
      if (type === "click" && this.tagName === "SUMMARY" && this.parentNode && this.parentNode.tagName === "DETAILS") {
        this.parentNode.open = !this.parentNode.open;   // the one native behaviour the UI relies on
      }
      (this._listeners[type] || []).slice().forEach((fn) => fn.call(this, { type, target: this, preventDefault() {} }));
      return true;
    }
    get textContent() {
      return this.childNodes.map((n) => n.textContent).join("");
    }
    set textContent(value) {
      this.childNodes.slice().forEach((k) => this.removeChild(k));
      if (value !== "" && value !== null && value !== undefined) this.appendChild(new Text(String(value)));
    }
    get dataset() {
      const el = this;
      const out = {};
      Object.keys(el._attrs || {}).forEach((k) => {
        if (k.indexOf("data-") === 0) out[k.slice(5).replace(/-([a-z])/g, (m, c) => c.toUpperCase())] = el._attrs[k];
      });
      return new Proxy(out, {
        set(_t, key, value) {
          el.setAttribute("data-" + String(key).replace(/[A-Z]/g, (c) => "-" + c.toLowerCase()), String(value));
          return true;
        },
      });
    }
    get classList() {
      const el = this;
      return {
        add(...names) {
          const set = new Set(el.className.split(/\s+/).filter(Boolean));
          names.forEach((n) => set.add(n));
          el.className = [...set].join(" ");
        },
        remove(...names) {
          const set = new Set(el.className.split(/\s+/).filter(Boolean));
          names.forEach((n) => set.delete(n));
          el.className = [...set].join(" ");
        },
        toggle(name, force) {
          const has = el.classList.contains(name);
          const want = force === undefined ? !has : !!force;
          if (want) el.classList.add(name);
          else el.classList.remove(name);
          return want;
        },
        contains(name) {
          return el.className.split(/\s+/).includes(name);
        },
      };
    }
    matches(sel) {
      return matchCompound(this, sel.trim());
    }
    closest(sel) {
      let node = this;
      while (node && node.nodeType === 1) {
        if (node.matches(sel)) return node;
        node = node.parentNode;
      }
      return null;
    }
    querySelector(sel) {
      return this.querySelectorAll(sel)[0] || null;
    }
    querySelectorAll(sel) {
      const found = [];
      walk(this, (node) => {
        if (node !== this && matchSelector(node, sel)) found.push(node);
      });
      return found;
    }
    focus() {
      this.ownerDocument.activeElement = this;
    }
    get outerHTML() {
      return serialize(this);
    }
  }

  class Element extends Node {
    constructor(tag) {
      super();
      this.tagName = String(tag).toUpperCase();
      this._attrs = {};
      this._props = { value: "", checked: false, disabled: false, hidden: false, selected: false, open: false };
      this.ownerDocument = null;
      this._uid = ++uid;
    }
    get id() {
      return this._attrs.id || "";
    }
    get className() {
      return this._attrs.class || "";
    }
    set className(value) {
      this._attrs.class = String(value);
    }
    setAttribute(name, value) {
      this._attrs[name] = String(value);
    }
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null;
    }
    removeAttribute(name) {
      delete this._attrs[name];
    }
    hasAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this._attrs, name);
    }
    /* A real CSSStyleDeclaration: property writes land in the serialized style attribute, so
       app.js/charts.js can set `el.style.height` exactly as they do in the browser. */
    get style() {
      if (!this._style) {
        const attrs = this._attrs;
        this._style = new Proxy({}, {
          get: (target, prop) => {
            if (prop === "cssText") return attrs.style || "";
            const css = attrs.style || "";
            const hit = new RegExp("(?:^|;)\\s*" + String(prop) + "\\s*:\\s*([^;]+)").exec(css);
            return hit ? hit[1].trim() : "";
          },
          set: (target, prop, value) => {
            if (prop === "cssText") { attrs.style = String(value); return true; }
            const css = attrs.style || "";
            const stripped = css.replace(new RegExp("(?:^|;)\\s*" + String(prop) + "\\s*:\\s*[^;]+;?"), "").trim();
            attrs.style = (stripped ? stripped.replace(/;?$/, ";") + " " : "") + String(prop) + ": " + String(value);
            return true;
          },
        });
      }
      return this._style;
    }
    set style(value) {
      this._attrs.style = String(value);
    }
    get value() {
      return this._props.value;
    }
    set value(v) {
      this._props.value = v;
    }
    get checked() {
      return this._props.checked;
    }
    set checked(v) {
      this._props.checked = !!v;
    }
    get disabled() {
      return this._props.disabled;
    }
    set disabled(v) {
      this._props.disabled = !!v;
    }
    get hidden() {
      return this._props.hidden;
    }
    set hidden(v) {
      this._props.hidden = !!v;
    }
    get open() {
      return this._props.open;
    }
    set open(v) {
      this._props.open = !!v;
    }
    get htmlFor() {
      return this._attrs.for || "";
    }
    set htmlFor(v) {
      this._attrs.for = String(v);
    }
    set onclick(fn) {
      this.addEventListener("click", fn);
    }
  }

  class Text {
    constructor(text) {
      this.nodeType = 3;
      this.textContent = String(text);
      this.parentNode = null;
    }
  }

  function walk(node, fn) {
    node.childNodes.forEach((child) => {
      fn(child);
      if (child.childNodes) walk(child, fn);
    });
  }

  function matchCompound(node, sel) {
    if (!sel || node.nodeType !== 1) return false;
    const parts = sel.match(/(^[a-zA-Z][\w-]*)|(#[\w-]+)|(\.[\w-]+)|(\[[^\]]+\])/g) || [];
    if (!parts.length) return false;
    return parts.every((part) => {
      if (part[0] === "#") return node.id === part.slice(1);
      if (part[0] === ".") return node.classList.contains(part.slice(1));
      if (part[0] === "[") {
        const name = part.slice(1, -1).split("=")[0].trim();
        return node.hasAttribute(name) || (name.indexOf("data-") === 0 && node.dataset && node.getAttribute(name) !== null);
      }
      return node.tagName === part.toUpperCase();
    });
  }

  function matchSelector(node, sel) {
    const chain = String(sel).trim().split(/\s+/);
    if (chain.length === 1) return matchCompound(node, chain[0]);
    // descendant combinator, right-to-left
    if (!matchCompound(node, chain[chain.length - 1])) return false;
    let ancestor = node.parentNode;
    for (let i = chain.length - 2; i >= 0; i -= 1) {
      let found = false;
      while (ancestor && ancestor.nodeType === 1) {
        if (matchCompound(ancestor, chain[i])) {
          found = true;
          ancestor = ancestor.parentNode;
          break;
        }
        ancestor = ancestor.parentNode;
      }
      if (!found) return false;
    }
    return true;
  }

  function shown(node) {
    let current = node;
    while (current && current.nodeType === 1) {
      if (current.tagName === "DETAILS" && !current.open) return false;
      if (current._props && current._props.hidden) return false;
      if (current.getAttribute && current.getAttribute("hidden") !== null) return false;
      current = current.parentNode;
    }
    return true;
  }

  function serialize(node) {
    if (node.nodeType === 3) return escapeText(node.textContent);
    const attrs = Object.keys(node._attrs)
      .filter((k) => k !== "class" || node._attrs[k])
      .map((k) => ` ${k}="${String(node._attrs[k]).replace(/"/g, "&quot;")}"`)
      .join("");
    const kids = node.childNodes.map(serialize).join("");
    const tag = node.tagName.toLowerCase();
    const voidTags = ["input", "br", "img", "hr", "meta", "link"];
    if (voidTags.includes(tag)) return `<${tag}${attrs}>`;
    return `<${tag}${attrs}>${kids}</${tag}>`;
  }

  function escapeText(text) {
    return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  const documentElement = new Element("html");
  DOC_ROOT = documentElement;
  const body = new Element("body");
  const registry = {};

  function element(tag, attrs = {}, kids = []) {
    const el = new Element(tag);
    Object.keys(attrs).forEach((k) => el.setAttribute(k, attrs[k]));
    kids.forEach((kid) => el.appendChild(typeof kid === "string" ? new Text(kid) : kid));
    return el;
  }

  // Shell: only the nodes app.js looks up by id / selector, mirroring index.html.
  const view = element("main", { id: "view" });
  const nav = element("nav", { id: "nav" });
  ["dashboard", "plan", "forecasts", "batteries", "savings", "settings", "log"].forEach((name) => {
    const li = element("li");
    li.appendChild(element("a", { href: "#/" + name }, [name]));
    nav.appendChild(li);
  });
  const live = element("p", { id: "live" });
  const conn = element("p", { id: "conn-status" });
  const build = element("p", { id: "build-info" });
  const dialog = element("dialog", { id: "confirm-dialog" });
  const form = element("form", { id: "confirm-form" });
  const title = element("h2", { id: "confirm-title" });
  const cbody = element("p", { id: "confirm-body" });
  const checkWrap = element("p", { id: "confirm-check-wrap" });
  const check = element("input", { id: "confirm-check", type: "checkbox" });
  const checkLabel = element("span", { id: "confirm-check-label" });
  const hint = element("p", { id: "confirm-hint" });
  const cancel = element("button", { id: "confirm-cancel" });
  const ok = element("button", { id: "confirm-ok" });
  const skip = element("a", { class: "skip-link" });
  const topbarActions = element("div", { class: "topbar-actions" });
  const themeToggle = element("button", { id: "theme-toggle", class: "btn btn-ghost", type: "button", "aria-label": "Theme" });
  themeToggle.appendChild(element("span", { class: "theme-state" }, ["Theme: auto"]));
  topbarActions.appendChild(themeToggle);

  const shell = [skip, topbarActions, nav, view, live, conn, build, dialog];
  [title, cbody, checkWrap, check, checkLabel, hint, cancel, ok].forEach((el) => form.appendChild(el));
  dialog.appendChild(form);
  shell.forEach((el) => {
    body.appendChild(el);
    if (el.id) registry[el.id] = el;
    el.querySelectorAll("[id]").forEach((kid) => { registry[kid.id] = kid; });
  });
  documentElement.appendChild(body);

  const document = {
    documentElement,
    body,
    hidden: false,
    activeElement: null,
    createElement: (tag) => {
      const el = new Element(tag);
      el.ownerDocument = document;
      return el;
    },
    createTextNode: (text) => new Text(text),
    getElementById: (id) => {
      // Faithful to the DOM: ids created at runtime are found too, not only the shell nodes.
      if (registry[id]) return registry[id];
      const hit = documentElement.querySelector("#" + id);
      if (hit) registry[id] = hit;
      return hit || null;
    },
    querySelector: (sel) => documentElement.querySelector(sel),
    querySelectorAll: (sel) => documentElement.querySelectorAll(sel),
    addEventListener: () => {},
    removeEventListener: () => {},
  };
  body.ownerDocument = documentElement.ownerDocument = document;
  [nav, view, live, conn, build, dialog].forEach((el) => (el.ownerDocument = document));

  return { document, documentElement, body, registry, serialize, walk, shown, Text, Element };
}

module.exports = { makeDom };
