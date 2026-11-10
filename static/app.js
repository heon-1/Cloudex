"use strict";
const $ = id => document.getElementById(id);
const telegram = window.Telegram?.WebApp;
telegram?.ready();
telegram?.expand();
const auth = telegram?.initData || "";
let selected = null, state = null, cursor = 0, server = "", models = [], historyCursor = null;
let showArchived = false, approvalSignature = "", currentTab = "chat", stopped = false;
let settingsDirty = false;
const transcriptNodes = new Map(), logs = new Map(), messageTexts = new Map();
const drafts = new Map(), sending = new Set(), uploading = new Set();
function saveDraft(sid = selected) {
  if (!sid) return;
  drafts.set(sid, {text: $("prompt").value, attachment: $("attachment-note").hidden ? "" : $("attachment-note").textContent});
}
function restoreDraft(sid = selected) {
  const draft = drafts.get(sid) || {text: "", attachment: ""};
  $("prompt").value = draft.text;
  $("attachment-note").textContent = draft.attachment;
  $("attachment-note").hidden = !draft.attachment;
  fitPrompt();
}
let sessionQuery = "";
let sessionListSignature = "", selectionSequence = 0;
function storedSession() {
  try { return localStorage.getItem("cloudex-session"); } catch { return null; }
}
function rememberSession(sid) {
  try { localStorage.setItem("cloudex-session", sid); } catch { /* WebView storage may be unavailable. */ }
}
function fitPrompt() {
  const input = $("prompt");
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 180) + "px";
  input.disabled = sending.has(selected);
  $("send").disabled = !auth || !input.value.trim() || sending.has(selected) || uploading.has(selected);
  $("attach").disabled = sending.has(selected) || uploading.has(selected);
}
function setApprovalPanel(open) {
  $("approval-panel").hidden = !open;
  $("approval-tab").setAttribute("aria-expanded", String(open));
}

function error(message) { $("error").textContent = String(message); $("error").hidden = !message; }
function el(tag, text, className) { const node = document.createElement(tag); if (text != null) node.textContent = text; if (className) node.className = className; return node; }
function button(text, handler, className = "") { const node = el("button", text, className); node.type = "button"; node.onclick = () => Promise.resolve().then(handler).catch(e => error(e.message)); return node; }
async function api(path, data = {}, post = false) {
  if (!auth) throw new Error("텔레그램의 Cloudex 봇에서 미니앱을 열어주세요.");
  const global = ["/api/status", "/api/events", "/api/models"].includes(path);
  const values = { ...(global ? {} : {session: selected || state?.selected || "main"}), ...data };
  const controller = new AbortController();
  const slow = ["/api/history", "/api/models", "/api/command", "/api/upload", "/api/download"].includes(path);
  const timeout = setTimeout(() => controller.abort(), slow ? 100000 : 30000);
  let response;
  try {
    response = await fetch(path + (post ? "" : "?" + new URLSearchParams(values)), {
      signal: controller.signal,
      method: post ? "POST" : "GET", headers: { "X-Telegram-Init-Data": auth, ...(post ? { "Content-Type": "application/json" } : {}) },
      ...(post ? { body: JSON.stringify(values) } : {})
    });
    if (path === "/api/download" && response.ok) return await response.blob();
    let result;
    try { result = await response.json(); }
    catch { throw new Error(`서버 연결을 확인해주세요. 미니앱을 다시 열어주세요. (${response.status})`); }
    if (!response.ok) throw new Error(result.error || `요청 실패 (${response.status})`);
    return result;
  } catch (e) {
    if (e.name === "AbortError") throw new Error("서버 응답이 지연되고 있습니다. 잠시 후 다시 시도해주세요.");
    throw e;
  } finally { clearTimeout(timeout); }
}
async function command(text, sid = selected) {
  const result = await api("/api/command", { text, session: sid }, true);
  error("");
  await refresh();
  return result;
}

let dropdownSequence = 0;
const customSelects = new Map();
class CustomSelect {
  static active = null;
  constructor(select) {
    this.select = select;
    this.id = "dropdown-" + (++dropdownSequence);
    this.wrapper = el("div", null, "custom-select");
    this.trigger = el("button", null, "custom-select-trigger");
    this.trigger.type = "button";
    this.trigger.id = this.id + "-trigger";
    this.trigger.setAttribute("role", "combobox");
    this.trigger.setAttribute("aria-haspopup", "listbox");
    this.trigger.setAttribute("aria-expanded", "false");
    this.trigger.setAttribute("aria-controls", this.id + "-list");
    this.valueLabel = el("span", "", "custom-select-value");
    const caret = el("span", "", "custom-select-caret");
    caret.setAttribute("aria-hidden", "true");
    this.trigger.append(this.valueLabel, caret);
    this.popup = el("div", null, "custom-select-popup");
    this.popup.id = this.id + "-list";
    this.popup.setAttribute("role", "listbox");
    this.popup.hidden = true;
    this.labels = Array.from(select.labels || []);
    for (const label of this.labels) label.htmlFor = this.trigger.id;
    const name = this.labels.map(label => label.textContent.trim()).join(" ") || select.getAttribute("aria-label") || "선택";
    this.trigger.setAttribute("aria-label", name);
    this.popup.setAttribute("aria-label", name);
    select.before(this.wrapper);
    this.wrapper.append(select, this.trigger);
    document.body.append(this.popup);
    select.hidden = true;
    select.setAttribute("aria-hidden", "true");
    this.onChange = () => this.sync();
    select.addEventListener("change", this.onChange);
    this.trigger.onclick = () => this.popup.hidden ? this.open() : this.close();
    this.trigger.onkeydown = event => this.keydown(event);
    this.observer = new MutationObserver(() => this.sync());
    this.observer.observe(select, {childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["disabled", "label", "value", "selected", "data-description"]});
    customSelects.set(select, this);
    this.sync();
  }
  sync() {
    const options = Array.from(this.select.options);
    const selectedOption = options.find(option => option.value === this.select.value);
    this.valueLabel.textContent = selectedOption?.textContent || "선택하세요";
    this.trigger.disabled = this.select.disabled || !options.some(option => !option.disabled);
    this.trigger.title = this.valueLabel.textContent;
    const signature = JSON.stringify(options.map(option => [option.value, option.textContent, option.dataset.description, option.disabled]));
    if (signature !== this.signature) {
      this.signature = signature;
      this.items = options.map((option, index) => {
        const node = el("button", null, "custom-select-option");
        node.type = "button";
        node.id = this.id + "-option-" + index;
        node.tabIndex = -1;
        node.disabled = option.disabled;
        node.setAttribute("role", "option");
        const text = el("span", null, "custom-select-option-text");
        text.append(el("span", option.textContent));
        if (option.dataset.description) text.append(el("small", option.dataset.description));
        const check = el("span", "✓", "custom-select-check");
        check.setAttribute("aria-hidden", "true");
        node.append(text, check);
        node.onclick = () => this.choose(index);
        return node;
      });
      this.popup.replaceChildren(...this.items);
    }
    options.forEach((option, index) => {
      const selected = option.value === this.select.value;
      this.items[index].setAttribute("aria-selected", String(selected));
      this.items[index].classList.toggle("is-selected", selected);
    });
    if (this.trigger.disabled) this.close();
    else if (!this.popup.hidden) {
      if (signature !== this.openSignature) {
        this.highlight(selectedOption && !selectedOption.disabled ? options.indexOf(selectedOption) : this.enabled()[0]);
        this.openSignature = signature;
      }
      this.position();
    }
  }
  position() {
    const rect = this.trigger.getBoundingClientRect();
    const viewport = window.visualViewport;
    const top = viewport?.offsetTop || 0, left = viewport?.offsetLeft || 0;
    const width = viewport?.width || window.innerWidth, height = viewport?.height || window.innerHeight;
    const below = top + height - rect.bottom - 14, above = rect.top - top - 14;
    const up = below < 220 && above > below;
    const maxHeight = Math.max(60, Math.min(320, up ? above : below));
    const popupWidth = Math.min(Math.max(rect.width, 220), width - 24);
    this.popup.style.width = popupWidth + "px";
    this.popup.style.maxHeight = maxHeight + "px";
    this.popup.style.left = Math.max(left + 12, Math.min(rect.left, left + width - popupWidth - 12)) + "px";
    const popupHeight = Math.min(this.popup.scrollHeight, maxHeight);
    this.popup.style.top = Math.max(top + 12, up ? rect.top - popupHeight - 6 : rect.bottom + 6) + "px";
  }
  open() {
    if (this.trigger.disabled) return;
    CustomSelect.active?.close();
    this.sync();
    CustomSelect.active = this;
    this.popup.hidden = false;
    this.trigger.setAttribute("aria-expanded", "true");
    this.openSignature = this.signature;
    const index = Array.from(this.select.options).findIndex(option => option.value === this.select.value && !option.disabled);
    this.position();
    this.highlight(index < 0 ? this.enabled()[0] : index);
  }
  close() {
    this.popup.hidden = true;
    this.trigger.setAttribute("aria-expanded", "false");
    this.trigger.removeAttribute("aria-activedescendant");
    if (CustomSelect.active === this) CustomSelect.active = null;
  }
  enabled() { return Array.from(this.select.options).flatMap((option, index) => option.disabled ? [] : [index]); }
  highlight(index) {
    if (index == null || !this.items[index] || this.items[index].disabled) return;
    this.index = index;
    this.items.forEach((node, i) => node.classList.toggle("is-highlighted", i === index));
    const node = this.items[index];
    this.trigger.setAttribute("aria-activedescendant", node.id);
    if (node.offsetTop < this.popup.scrollTop) this.popup.scrollTop = node.offsetTop;
    else if (node.offsetTop + node.offsetHeight > this.popup.scrollTop + this.popup.clientHeight) {
      this.popup.scrollTop = node.offsetTop + node.offsetHeight - this.popup.clientHeight;
    }
  }
  choose(index) {
    const option = this.select.options[index];
    if (!option || option.disabled || this.select.disabled) return;
    const changed = this.select.value !== option.value;
    this.select.value = option.value;
    this.close();
    this.sync();
    this.trigger.focus({preventScroll: true});
    if (changed) this.select.dispatchEvent(new Event("change", {bubbles: true}));
  }
  keydown(event) {
    const keys = ["ArrowDown", "ArrowUp", "Home", "End", "Enter", " ", "Escape"];
    if (event.key === "Tab") { this.close(); return; }
    if (keys.includes(event.key)) {
      event.preventDefault();
      if (event.key === "Escape") { event.stopPropagation(); this.close(); return; }
      if (this.popup.hidden) { this.open(); return; }
      const enabled = this.enabled(), current = enabled.indexOf(this.index);
      if (event.key === "Enter" || event.key === " ") this.choose(this.index);
      else if (event.key === "Home") this.highlight(enabled[0]);
      else if (event.key === "End") this.highlight(enabled.at(-1));
      else this.highlight(enabled[(current + (event.key === "ArrowDown" ? 1 : -1) + enabled.length) % enabled.length]);
    } else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      this.search = (Date.now() - (this.searchTime || 0) > 600 ? "" : this.search || "") + event.key.toLocaleLowerCase();
      this.searchTime = Date.now();
      if (this.popup.hidden) this.open();
      const index = Array.from(this.select.options).findIndex(option => !option.disabled && option.textContent.toLocaleLowerCase().startsWith(this.search));
      if (index >= 0) { event.preventDefault(); this.highlight(index); }
    }
  }
  destroy() {
    this.close();
    this.observer.disconnect();
    this.select.removeEventListener("change", this.onChange);
    for (const label of this.labels) label.htmlFor = this.select.id;
    this.select.hidden = false;
    this.select.removeAttribute("aria-hidden");
    this.wrapper.before(this.select);
    this.wrapper.remove();
    this.popup.remove();
    customSelects.delete(this.select);
  }
}
function enhanceSelect(select) { return customSelects.get(select) || new CustomSelect(select); }
function syncCustomSelects() { for (const control of customSelects.values()) control.sync(); }
document.addEventListener("pointerdown", event => {
  const active = CustomSelect.active;
  if (active && !active.wrapper.contains(event.target) && !active.popup.contains(event.target)) active.close();
});
document.addEventListener("scroll", event => {
  const active = CustomSelect.active;
  if (active && !active.popup.contains(event.target)) active.close();
}, true);
window.addEventListener("resize", () => CustomSelect.active?.close());
window.visualViewport?.addEventListener("resize", () => CustomSelect.active?.close());

let toastTimer = null;
function showToast(text) {
  clearTimeout(toastTimer);
  $("toast").hidden = false;
  $("toast-message").textContent = text;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 3200);
}
let settingsSaving = false;
async function saveSettings() {
  if (settingsSaving) return;
  const sid = selected;
  const values = {model: $("model-select").value, effort: $("effort-select").value, approvalPolicy: $("policy-select").value};
  settingsSaving = true;
  clearTimeout(toastTimer);
  $("toast").hidden = true;
  $("save-settings").disabled = true;
  $("exit-settings").disabled = true;
  $("save-settings").textContent = "저장 중…";
  const controls = ["model-select", "effort-select", "policy-select"].map($);
  const disabled = controls.map(control => control.disabled);
  controls.forEach(control => { control.disabled = true; });
  syncCustomSelects();
  try {
    await api("/api/settings", {...values, session: sid}, true);
    const session = state?.sessions.find(entry => entry.id === sid);
    if (session) {
      if (values.model) session.model = values.model;
      if (values.effort) session.effort = values.effort;
      if (values.approvalPolicy === "full-access") {
        session.policy = "never"; session.sandbox = "danger-full-access";
      } else if (values.approvalPolicy) {
        session.policy = values.approvalPolicy;
        if (session.sandbox === "danger-full-access") session.sandbox = "workspace-write";
      }
    }
    if (selected === sid) {
      settingsDirty = false;
      render();
      changeTab("chat");
      document.querySelector('[data-tab="chat"]')?.focus({preventScroll: true});
    }
    error("");
    showToast(selected === sid ? "설정이 저장됐어요" : (session?.name || "대화") + " 설정이 저장됐어요");
    refresh().catch(e => error(e.message));
  } catch (e) {
    error(e.message);
  } finally {
    settingsSaving = false;
    $("save-settings").disabled = false;
    $("exit-settings").disabled = false;
    $("save-settings").textContent = "설정 저장";
    controls.forEach((control, index) => { control.disabled = disabled[index]; });
    syncCustomSelects();
  }
}

function getSession() { return state?.sessions.find(s => s.id === selected); }
function changeTab(tab) {
  CustomSelect.active?.close();
  currentTab = tab;
  document.querySelectorAll(".view").forEach(n => n.classList.toggle("active", n.id === tab));
  document.querySelectorAll("[data-tab]").forEach(n => {
    const active = n.dataset.tab === tab;
    n.classList.toggle("selected", active);
    if (active) n.setAttribute("aria-current", "page"); else n.removeAttribute("aria-current");
  });
  hideSidebar();
  $("composer").hidden = tab !== "chat";
  if (tab === "files") loadFiles().catch(e => error(e.message));
  if (tab === "settings") loadModels().catch(e => error(e.message));
}
function hideSidebar() {
  document.body.classList.remove("sidebar-open");
  $("open-sidebar").setAttribute("aria-expanded", String(!window.matchMedia("(max-width: 760px)").matches && !document.body.classList.contains("sidebar-collapsed")));
}
async function selectSession(sid, notify = true) {
  const sequence = ++selectionSequence;
  saveDraft();
  selected = sid;
  restoreDraft(sid);
  settingsDirty = false;
  rememberSession(sid);
  transcriptNodes.clear(); messageTexts.clear(); $("transcript").replaceChildren(); $("empty").hidden = false;
  approvalSignature = ""; historyCursor = null;
  hideSidebar(); render();
  if (notify) command(`/use ${sid}`, sid).catch(e => {
    if (sequence === selectionSequence) error(e.message);
  });
  await Promise.all([
    loadHistory().catch(e => { if (sequence === selectionSequence) error(e.message); }),
    currentTab === "files" ? loadFiles().catch(e => { if (sequence === selectionSequence) error(e.message); }) : Promise.resolve()
  ]);
}
async function refresh() {
  const next = await api("/api/status");
  state = next;
  if (!selected || !state.sessions.some(s => s.id === selected && !s.archived)) {
    saveDraft();
    selected = state.selected;
    rememberSession(selected);
    restoreDraft(selected);
  }
  $("connection").textContent = "Ubuntu에 연결됨";
  document.body.classList.add("connected");
  render();
}
function render() {
  if (!state) return;
  const session = getSession(); if (!session) return;
  $("session-name").textContent = session.name;
  const running = state.sessions.filter(entry => entry.active && !entry.archived).length;
  $("running-sessions").textContent = running > 1 ? running + "개 동시 실행" : running ? "1개 실행 중" : "로컬";
  $("session-status").replaceChildren(el("i", "", "dot"), document.createTextNode(session.active ? " 작업 실행 중" : " 대기 중"));
  $("queue-status").textContent = session.queued ? `${session.queued}개 대기` : "";
  const model = models.find(m => (m.model || m.id) === session.model);
  $("composer-model").textContent = model?.displayName || session.model || "Codex 기본 모델";
  const effort = session.effort || session.effective?.reasoningEffort;
  $("composer-effort").textContent = ({minimal: "최소", low: "낮음", medium: "보통", high: "높음", xhigh: "매우 높음", max: "최대", ultra: "최상"})[effort] || effort || "기본 추론";
  const fullAccess = session.policy === "never" && session.sandbox === "danger-full-access";
  $("composer-policy").textContent = fullAccess ? "항상 승인 · 전체 액세스" : ({"on-request": "필요 시 승인", untrusted: "작업 승인 필요", never: "추가 권한 요청 거절"})[session.policy] || "기본 권한";
  document.body.classList.toggle("working", session.active);
  $("stop-job").hidden = !session.active;
  $("effective-settings").textContent = JSON.stringify(session.effective || {}, null, 2);
  $("retry-job").hidden = !session.interrupted;
  $("console-output").textContent = logs.get(selected) || "실행 로그가 여기에 표시됩니다.";
  const entries = state.sessions.filter(s => (showArchived ? s.archived : !s.archived) && (s.name + " " + s.id).toLocaleLowerCase().includes(sessionQuery));
  const navSignature = JSON.stringify([selected, showArchived, sessionQuery, entries.map(s => [s.id, s.name, s.archived, s.active, s.queued])]);
  if (navSignature !== sessionListSignature) {
    sessionListSignature = navSignature;
    const nav = $("session-list"); nav.replaceChildren();
    for (const entry of entries) {
      const node = button("", async () => {
        if (entry.archived) { await command(`/restore ${entry.id}`); await selectSession(entry.id, false); }
        else await selectSession(entry.id);
      }, "session" + (entry.id === selected ? " selected" : ""));
      node.append(el("strong", entry.name));
      node.title = entry.name + (entry.active ? " · 실행 중" : "");
      if (entry.id === selected) node.setAttribute("aria-current", "page");
      const info = el("small", entry.archived ? "복원" : entry.active ? "●" : entry.queued ? String(entry.queued) : "", entry.active ? "running" : "");
      if (entry.active) info.setAttribute("aria-label", "작업 실행 중");
      node.append(info); nav.append(node);
    }
    if (!entries.length) nav.append(el("p", sessionQuery ? "검색 결과가 없습니다." : showArchived ? "보관된 대화가 없습니다." : "새 대화를 시작하세요.", "session-empty"));
  }
  const approvals = state.approvals.filter(a => a.session === selected);
  $("approval-tab").classList.toggle("has-approvals", approvals.length > 0);
  $("approval-count").textContent = approvals.length;
  const signature = selected + ":" + approvals.map(a => a.key).join(",");
  if (signature !== approvalSignature) { approvalSignature = signature; renderApprovals(approvals); }
  if (!settingsDirty && document.activeElement?.tagName !== "SELECT") {
    $("model-select").value = session.model || "";
    renderEfforts(session.effort);
    $("policy-select").value = fullAccess ? "full-access" : typeof session.policy === "string" ? session.policy : "";
  }
  syncCustomSelects();
}
function inlineMarkdown(node, text) {
  // Construct only text and known elements; model output never becomes HTML.
  const tokens = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g;
  let offset = 0;
  for (const match of text.matchAll(tokens)) {
    node.append(document.createTextNode(text.slice(offset, match.index)));
    const code = match[0].startsWith("`");
    node.append(el(code ? "code" : "strong", match[0].slice(code ? 1 : 2, code ? -1 : -2)));
    offset = match.index + match[0].length;
  }
  node.append(document.createTextNode(text.slice(offset)));
}
function renderMarkdown(container, text) {
  const fragment = document.createDocumentFragment();
  const lines = text.split("\n");
  let paragraph = [], code = null, language = "", list = null, listType = "";
  const flush = () => {
    if (paragraph.length) { const p = el("p"); inlineMarkdown(p, paragraph.join("\n")); fragment.append(p); paragraph = []; }
  };
  const codeBlock = () => {
    const block = el("div", null, "code-block"), bar = el("div", null, "code-bar"), content = code.join("\n");
    bar.append(el("span", language || "코드"));
    if (navigator.clipboard?.writeText) bar.append(button("복사", () => navigator.clipboard.writeText(content)));
    const pre = el("pre"); pre.append(el("code", content)); block.append(bar, pre); fragment.append(block);
  };
  for (const line of lines) {
    if (/^\s*```/.test(line)) {
      flush(); list = null;
      if (code !== null) { codeBlock(); code = null; } else { code = []; language = line.trim().slice(3).trim(); }
      continue;
    }
    if (code !== null) { code.push(line); continue; }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    const bullet = line.match(/^\s*(?:([-*])|\d+[.)])\s+(.+)$/);
    if (heading) {
      flush(); list = null;
      const h = el("h" + Math.min(heading[1].length + 2, 6)); inlineMarkdown(h, heading[2]); fragment.append(h);
    } else if (bullet) {
      flush();
      const kind = bullet[1] ? "ul" : "ol";
      if (!list || listType !== kind) { list = el(kind); listType = kind; fragment.append(list); }
      const li = el("li"); inlineMarkdown(li, bullet[2]); list.append(li);
    } else if (!line.trim()) { flush(); list = null; }
    else { list = null; paragraph.push(line); }
  }
  flush();
  if (code !== null) codeBlock();
  container.replaceChildren(fragment);
}
function message(item) {
  const type = item.type;
  if (!["agentMessage", "userMessage", "commandExecution", "fileChange"].includes(type)) return;
  $("empty").hidden = true;
  let node = transcriptNodes.get(item.id);
  const view = $("chat"), atBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 180;
  if (!node) {
    const tool = type === "commandExecution" || type === "fileChange";
    node = el(tool ? "details" : "article", null, "message " + (type === "userMessage" ? "user" : tool ? "command" : "assistant"));
    const role = el(tool ? "summary" : "div", null, tool ? "" : "role");
    if (tool) role.append(el("span", "", "role"));
    else role.textContent = type === "userMessage" ? "나" : "Codex";
    node.append(role, el("div", "", "text"));
    transcriptNodes.set(item.id, node); $("transcript").append(node);
  }
  let text = item.text || "";
  if (type === "userMessage") text = (item.content || []).map(c => c.text || c.path || "첨부 파일").join("\n");
  if (type === "commandExecution") text = `$ ${item.command || ""}\n${item.aggregatedOutput || ""}`;
  if (type === "fileChange") text = (item.changes || []).map(c => c.path).join("\n");
  if (type === "commandExecution") node.querySelector(".role").textContent = (item.status === "inProgress" ? "실행 중 · " : "명령 · ") + (item.command || "터미널");
  if (type === "fileChange") node.querySelector(".role").textContent = "파일 변경 · " + (item.changes || []).length + "개";
  messageTexts.set(item.id, text);
  if (type === "agentMessage") {
    node.querySelector(".text").classList.add("rich-text");
    renderMarkdown(node.querySelector(".text"), text);
  } else node.querySelector(".text").textContent = text;
  if (atBottom) view.scrollTop = view.scrollHeight;
}
async function loadHistory(older = false) {
  const sid = selected;
  const result = await api("/api/history", { session: sid, ...(older && historyCursor ? { cursor: historyCursor } : {}) });
  if (sid !== selected) return;
  const entries = (result.data || []).slice().reverse();
  if (older) {
    const existing = Array.from($("transcript").children);
    for (const entry of entries) message(entry.item || entry);
    const newNodes = Array.from($("transcript").children).filter(n => !existing.includes(n));
    $("transcript").replaceChildren(...newNodes, ...existing);
  } else for (const entry of entries) message(entry.item || entry);
  historyCursor = result.nextCursor || null; $("older-history").hidden = !historyCursor;
}
function log(sid, line) {
  logs.set(sid, ((logs.get(sid) || "") + line + "\n").slice(-200000));
  if (sid === selected) { $("console-output").textContent = logs.get(sid); if (currentTab === "console") $("console").scrollTop = $("console").scrollHeight; }
}
function nativeEvent(event) {
  const sid = event.cloudex_session || selected, params = event.params || {};
  if (event.method === "item/commandExecution/outputDelta") log(sid, params.delta || "");
  else if (["item/started", "item/completed"].includes(event.method)) {
    const item = params.item || {};
    if (sid === selected) message(item);
    if (item.type === "commandExecution") log(sid, `$ ${item.command || ""}\n${item.aggregatedOutput || item.status || ""}`);
  } else if (event.method === "item/agentMessage/delta" && sid === selected) {
    const previous = messageTexts.get(params.itemId) || "";
    message({ id: params.itemId, type: "agentMessage", text: previous + (params.delta || "") });
  } else if (event.method === "error") log(sid, "오류: " + JSON.stringify(params.error || params));
  else if (event.method === "turn/completed") log(sid, "작업 " + (params.turn?.status || "종료"));
  else if (event.method === "cloudex/fileUploaded" && sid === selected && currentTab === "files") loadFiles().catch(e => error(e.message));
}
async function pollEvents() {
  while (!stopped) {
    try {
      const result = await api("/api/events", { after: cursor, server });
      if (server && server !== result.server) { cursor = 0; await loadHistory(); }
      server = result.server; cursor = result.cursor;
      for (const event of result.events) nativeEvent(event);
    } catch (e) { error(e.message); await new Promise(resolve => setTimeout(resolve, 3000)); }
  }
}
function renderApprovals(approvals) {
  const container = $("approvals");
  for (const select of container.querySelectorAll("select")) customSelects.get(select)?.destroy();
  container.replaceChildren();
  for (const request of approvals) {
    const card = el("article", null, "approval-card"); card.append(el("h3", request.method), el("pre", JSON.stringify(request.params, null, 2)));
    const submit = async text => { await command(text, request.session); approvalSignature = ""; await refresh(); };
    if (request.method === "item/tool/requestUserInput") {
      for (const question of request.params.questions || []) {
        card.append(el("label", question.question || question.header || question.id));
        const input = question.options?.length ? el("select") : el("input");
        if (question.options?.length) for (const option of question.options) { const o = el("option", option.label + (option.description ? " — " + option.description : "")); o.value = option.label; input.append(o); }
        input.setAttribute("aria-label", question.id); card.append(input, button("답변 보내기", () => submit(`/answer ${request.key} ${question.id} ${input.value}`)));
      }
    } else if (request.method === "mcpServer/elicitation/request" && request.params.mode !== "url") {
      const input = el("textarea"); input.value = JSON.stringify(Object.fromEntries(Object.entries(request.params.requestedSchema?.properties || {}).map(([key, value]) => [key, value.default ?? ""])), null, 2);
      input.setAttribute("aria-label", "MCP 양식 JSON"); card.append(input, button("양식 보내기", () => { JSON.parse(input.value); return submit(`/form ${request.key} ${input.value}`); }));
    } else {
      if (request.params.mode === "url" && /^https:\/\//.test(request.params.url || "")) card.append(button("요청 링크 열기", () => telegram?.openLink(request.params.url)));
      card.append(button("이번 요청 승인", () => api("/api/decision", { key: request.key, decision: "accept" }, true).then(refresh)));
      if (["item/commandExecution/requestApproval", "item/fileChange/requestApproval"].includes(request.method) && (!request.params.availableDecisions || request.params.availableDecisions.includes("acceptForSession"))) card.append(button("세션 승인", () => api("/api/decision", { key: request.key, decision: "acceptForSession" }, true).then(refresh)));
    }
    card.append(button("거절", () => api("/api/decision", { key: request.key, decision: "decline" }, true).then(refresh)));
    container.append(card);
    card.querySelectorAll("select").forEach(enhanceSelect);
  }
  if (!approvals.length) container.append(el("p", "대기 중인 요청이 없습니다.", "hint"));
}
async function loadModels() {
  if (!models.length) {
    let next = null;
    do { const response = await api("/api/models", next ? { cursor: next } : {}); models.push(...(response.data || [])); next = response.nextCursor; } while (next);
    for (const model of models) { const option = el("option", model.displayName || model.model || model.id); option.value = model.model || model.id; $("model-select").append(option); }
  }
  render();
}
function renderEfforts(preferred) {
  const model = models.find(m => (m.model || m.id) === $("model-select").value);
  const select = $("effort-select"), prev = preferred ?? select.value;
  select.replaceChildren(); const fallback = el("option", "기본값"); fallback.value = ""; select.append(fallback);
  for (const effort of model?.supportedReasoningEfforts || []) { const option = el("option", effort.reasoningEffort + (effort.description ? " · " + effort.description : "")); option.value = effort.reasoningEffort; select.append(option); }
  if (prev && !Array.from(select.options).some(o => o.value === prev)) { const option = el("option", prev); option.value = prev; select.append(option); }
  select.value = prev || "";
  customSelects.get(select)?.sync();
}
async function loadFiles() {
  const sid = selected, result = await api("/api/files", { session: sid }); if (sid !== selected) return;
  const container = $("file-list"); container.replaceChildren();
  for (const file of result.files || []) {
    const row = el("div", null, "file-row"); row.append(el("span", file.path, "file-name"), el("span", file.size < 1024 ? `${file.size} B` : `${(file.size / 1024).toFixed(1)} KB`, "file-size"));
    row.append(button("받기", async () => { const blob = await api("/api/download", { session: sid, path: file.path }); const url = URL.createObjectURL(blob), link = document.createElement("a"); link.href = url; link.download = file.path.split("/").pop(); link.click(); setTimeout(() => URL.revokeObjectURL(url), 30000); }));
    row.append(button("텔레그램", () => command(`/get ${file.path}`, sid))); container.append(row);
  }
  if (!result.files?.length) container.append(el("p", "파일을 첨부하거나 Codex에 작업을 요청하세요.", "hint"));
}
$("open-sidebar").onclick = () => {
  if (window.matchMedia("(max-width: 760px)").matches) document.body.classList.add("sidebar-open");
  else document.body.classList.remove("sidebar-collapsed");
  $("open-sidebar").setAttribute("aria-expanded", "true");
};
$("close-sidebar").onclick = () => {
  if (window.matchMedia("(max-width: 760px)").matches) {
    hideSidebar();
    $("open-sidebar").focus();
  } else {
    document.body.classList.add("sidebar-collapsed");
    $("open-sidebar").setAttribute("aria-expanded", "false");
    $("open-sidebar").focus();
  }
};
$("scrim").onclick = hideSidebar;
document.addEventListener("keydown", event => {
  if (event.key === "Escape") { hideSidebar(); setApprovalPanel(false); $("open-sidebar").focus(); }
});
window.matchMedia("(max-width: 760px)").addEventListener("change", () => {
  document.body.classList.remove("sidebar-collapsed");
  hideSidebar();
});
$("session-search").oninput = event => { sessionQuery = event.target.value.trim().toLocaleLowerCase(); render(); };
$("archived-toggle").onclick = () => { showArchived = !showArchived; $("archived-toggle").textContent = showArchived ? "모든 대화" : "보관함"; $("archived-toggle").setAttribute("aria-pressed", String(showArchived)); render(); };
$("new-session").onclick = async () => { try { const name = prompt("새 세션 이름", "새 대화"); if (name === null) return; const result = await command(`/new ${name}`); await selectSession(result.selected, false); } catch(e) { error(e.message); } };
$("rename-session").onclick = async () => { const name = prompt("세션 이름", getSession()?.name); if (name) await command(`/rename ${name}`).catch(e => error(e.message)); };
$("archive-session").onclick = async () => { if (confirm("이 세션을 보관할까요? 대화와 파일은 유지됩니다.")) { try { const result = await command(`/archive ${selected}`); await selectSession(result.selected, false); } catch(e) { error(e.message); } } };
document.querySelectorAll("[data-tab]").forEach(n => n.onclick = () => changeTab(n.dataset.tab));
$("model-shortcut").onclick = $("effort-shortcut").onclick = $("policy-shortcut").onclick = () => changeTab("settings");
$("approval-tab").onclick = () => setApprovalPanel($("approval-panel").hidden);
$("hide-approvals").onclick = () => setApprovalPanel(false);
$("clear-console").onclick = () => { logs.set(selected, ""); render(); };
$("refresh-files").onclick = () => loadFiles().catch(e => error(e.message));
$("older-history").onclick = () => loadHistory(true).catch(e => error(e.message));
$("model-select").onchange = () => { settingsDirty = true; renderEfforts(""); };
$("effort-select").onchange = $("policy-select").onchange = () => { settingsDirty = true; };
$("save-settings").onclick = saveSettings;
$("exit-settings").onclick = () => {
  if (settingsSaving) return;
  settingsDirty = false;
  render();
  changeTab("chat");
  error("");
  document.querySelector('[data-tab="chat"]')?.focus({preventScroll: true});
};
$("retry-job").onclick = () => command("/retry").catch(e => error(e.message));
$("stop-job").onclick = () => command("/stop").catch(e => error(e.message));
$("prompt-form").onsubmit = async event => {
  event.preventDefault();
  const input = $("prompt"), raw = input.value, text = raw.trim(), sid = selected;
  if (!sid || !text || !auth || sending.has(sid) || uploading.has(sid)) return;
  saveDraft(sid);
  sending.add(sid);
  fitPrompt();
  try {
    await api("/api/command", {text, session: sid}, true);
    if (selected === sid) saveDraft(sid);
    if (drafts.get(sid)?.text === raw) drafts.set(sid, {text: "", attachment: ""});
    if (selected === sid) error("");
    refresh().catch(e => error(e.message));
  } catch (e) {
    const name = state?.sessions.find(entry => entry.id === sid)?.name || sid;
    error(selected === sid ? e.message : name + " 전송 실패: " + e.message);
  } finally {
    sending.delete(sid);
    if (selected === sid) restoreDraft(sid);
  }
};
$("prompt").oninput = () => { saveDraft(); fitPrompt(); };
$("prompt").onkeydown = event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("prompt-form").requestSubmit(); } };
document.querySelectorAll(".suggestions button").forEach(n => n.onclick = () => { if (sending.has(selected)) return; $("prompt").value = n.dataset.prompt; saveDraft(); fitPrompt(); $("prompt").focus(); });
$("attach").onclick = () => $("file-input").click();
$("file-input").onchange = async event => {
  const file = event.target.files[0];
  event.target.value = "";
  if (!file) return;
  if (file.size > 20 * 1024 * 1024) { error("첨부 파일은 20MB 이하여야 합니다."); return; }
  const sid = selected;
  if (uploading.has(sid) || sending.has(sid)) return;
  saveDraft(sid);
  uploading.add(sid);
  fitPrompt();
  try {
    const content = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(",")[1]); reader.onerror = reject; reader.readAsDataURL(file); });
    const result = await api("/api/upload", {session: sid, name: file.name, content}, true);
    if (selected === sid) saveDraft(sid);
    const draft = drafts.get(sid) || {text: "", attachment: ""};
    draft.text += "\n첨부 파일: " + result.path + "\n";
    draft.attachment = file.name + " 첨부됨";
    drafts.set(sid, draft);
  } catch (e) {
    const name = state?.sessions.find(entry => entry.id === sid)?.name || sid;
    error(selected === sid ? e.message : name + " 첨부 실패: " + e.message);
  } finally {
    uploading.delete(sid);
    if (selected === sid) restoreDraft(sid);
  }
};
async function init() {
  ["model-select", "effort-select", "policy-select"].map($).forEach(enhanceSelect);
  fitPrompt();
  hideSidebar();
  if (!auth) { error("텔레그램의 Cloudex 봇에서 미니앱을 열어주세요. 아직 공개 주소가 없다면 서버 연결 설정을 먼저 완료하세요."); $("connection").textContent = "텔레그램 인증 필요"; return; }
  selected = storedSession();
  let refreshing = false, connectionFailed = false;
  const reconnect = async () => {
    if (refreshing) return;
    refreshing = true;
    const firstConnection = !state;
    try {
      await refresh();
      if (connectionFailed) { error(""); connectionFailed = false; }
      if (firstConnection) loadHistory().catch(e => error(e.message));
    } catch (e) {
      connectionFailed = true;
      $("connection").textContent = "연결 재시도 중";
      document.body.classList.remove("connected");
      error(e.message);
    } finally { refreshing = false; }
  };
  setInterval(reconnect, 2500);
  pollEvents();
  await reconnect();
}
init().catch(e => error(e.message));
