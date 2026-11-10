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
const drafts = new Map(), sending = new Set(), uploading = new Set(), deliveryModes = new Map();
const deletingSessions = new Set(), deletedSessions = new Set();
function saveDraft(sid = selected) {
  if (!sid || deletedSessions.has(sid)) return;
  drafts.set(sid, {text: $("prompt").value, attachment: $("attachment-note").hidden ? "" : $("attachment-note").textContent});
}
function restoreDraft(sid = selected) {
  const draft = drafts.get(sid) || {text: "", attachment: ""};
  $("prompt").value = draft.text;
  $("attachment-note").textContent = draft.attachment;
  $("attachment-note").hidden = !draft.attachment;
  $("delivery-mode").value = deliveryModes.get(sid) || "queue";
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
  const removing = deletingSessions.has(selected) || deletedSessions.has(selected) || getSession()?.deleting;
  input.disabled = sending.has(selected) || !!removing;
  $("send").disabled = !auth || !input.value.trim() || sending.has(selected) || uploading.has(selected) || !!removing;
  $("attach").disabled = sending.has(selected) || uploading.has(selected) || !!removing;
  const mode = $("delivery-mode"), immediate = mode.value === "steer";
  const supported = state?.messageModes?.includes("steer") === true;
  Array.from(mode.options).find(option => option.value === "steer").disabled = !supported;
  mode.disabled = sending.has(selected) || !!removing;
  if (immediate && !supported) $("send").disabled = true;
  const label = immediate ? "바로 적용" : getSession()?.active ? "대기열에 추가" : "메시지 보내기";
  $("send").title = label;
  $("send").setAttribute("aria-label", label);
  $("composer-hint").textContent = (immediate ? "Enter 바로 적용" : "Enter 대기열 추가") + " · Shift+Enter 줄바꿈";
  customSelects.get(mode)?.sync();
}
function setApprovalPanel(open) {
  $("approvals").hidden = !open;
  $("hide-approvals").textContent = open ? "접기" : "펼치기";
  $("hide-approvals").setAttribute("aria-expanded", String(open));
}

function error(message) {
  for (const id of ["error", "settings-error"]) { $(id).textContent = String(message); $(id).hidden = !message; }
}
function el(tag, text, className) { const node = document.createElement(tag); if (text != null) node.textContent = text; if (className) node.className = className; return node; }
function button(text, handler, className = "") { const node = el("button", text, className); node.type = "button"; node.onclick = () => Promise.resolve().then(handler).catch(e => error(e.message)); return node; }
async function api(path, data = {}, post = false) {
  if (!auth) throw new Error("텔레그램의 Cloudex 봇에서 미니앱을 열어주세요.");
  const global = ["/api/status", "/api/events", "/api/models", "/api/account", "/api/account/save", "/api/account/login", "/api/account/cancel"].includes(path);
  const values = { ...(global ? {} : {session: selected || state?.selected || "main"}), ...data };
  const controller = new AbortController();
  const slow = path.startsWith("/api/account") || path.startsWith("/api/queue") || ["/api/history", "/api/models", "/api/command", "/api/upload", "/api/download"].includes(path);
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
    (select.closest("dialog") || document.body).append(this.popup);
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
function showToast(text, abovePrompt = false) {
  clearTimeout(toastTimer);
  const toast = $("toast");
  (abovePrompt ? $("prompt-container") : $("settings").open ? $("settings") : $("queue-modal").open ? $("queue-modal") : document.body).append(toast);
  toast.classList.toggle("above-prompt", abovePrompt);
  toast.hidden = false;
  $("toast-message").textContent = text;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 3200);
}
let settingsSaving = false;
let accountState = null, accountLoading = false, accountAction = false, accountError = "";
const ACCOUNT_REFRESH_MS = 5 * 60 * 1000;
let accountLastAttempt = null;
function usageWindowLabel(minutes, fallback) {
  if (!Number.isFinite(minutes) || minutes <= 0) return fallback;
  if (minutes % 1440 === 0) return `${minutes / 1440}일 한도`;
  if (minutes % 60 === 0) return `${minutes / 60}시간 한도`;
  return `${minutes}분 한도`;
}
function resetDescription(timestamp) {
  if (!Number.isFinite(timestamp) || timestamp <= 0) return "리셋 시간 제공 안 됨";
  const date = new Date(timestamp * 1000);
  if (!Number.isFinite(date.getTime())) return "리셋 시간 제공 안 됨";
  const absolute = new Intl.DateTimeFormat("ko-KR", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short"}).format(date);
  const minutes = Math.ceil((date.getTime() - Date.now()) / 60000);
  let relative = "리셋 확인 중";
  if (minutes > 0) {
    const days = Math.floor(minutes / 1440), hours = Math.floor(minutes / 60) % 24;
    relative = days ? `${days}일 ${hours}시간 뒤` : hours ? `${hours}시간 ${minutes % 60}분 뒤` : `${minutes}분 뒤`;
  }
  return `리셋 ${absolute} · ${relative}`;
}
function renderUsage() {
  const usage = accountState?.usage, list = $("account-usage-windows");
  $("account-usage").hidden = !accountState?.account;
  list.replaceChildren();
  for (const bucket of usage?.buckets || []) {
    const group = el("div", null, "usage-bucket");
    group.append(el("h5", bucket.name === "codex" ? "Codex" : bucket.name || "Codex"));
    for (const key of ["primary", "secondary"]) {
      const window = bucket[key];
      if (!window) continue;
      const label = usageWindowLabel(window.windowDurationMins, key === "primary" ? "단기 한도" : "장기 한도");
      const remaining = window.remainingPercent;
      const known = Number.isFinite(remaining);
      const row = el("div", null, "usage-window"), header = el("div", null, "usage-window-heading");
      header.append(el("span", label), el("strong", known ? `${new Intl.NumberFormat("ko-KR", {maximumFractionDigits: 1}).format(remaining)}% 남음` : "잔량 확인 불가"));
      row.append(header);
      if (known) {
        const progress = el("progress", null, remaining <= 20 ? "usage-low" : "");
        progress.max = 100; progress.value = Math.max(0, Math.min(100, remaining));
        progress.setAttribute("aria-label", label + " 남은 사용량");
        row.append(progress);
      }
      const reset = el("time", resetDescription(window.resetsAt), "usage-reset");
      if (Number.isFinite(window.resetsAt) && window.resetsAt > 0 && window.resetsAt <= 253402300799) reset.dateTime = new Date(window.resetsAt * 1000).toISOString();
      row.append(reset); group.append(row);
    }
    list.append(group);
  }
  $("account-usage-note").textContent = usage?.error || (usage?.updatedAt ? "마지막 확인 " + new Date(usage.updatedAt * 1000).toLocaleTimeString("ko-KR") : "사용량 정보를 확인 중입니다.");
  $("account-usage-note").classList.toggle("account-error", !!usage?.error && !!usage?.buckets?.length);
}
function renderAccount() {
  const account = accountState?.account, login = accountState?.login;
  const pending = ["starting", "pending"].includes(login?.status);
  const hasWork = state ? state.sessions.some(session => session.active || session.queued || session.deleting) : !accountState?.canLogin;
  const canLogin = !!accountState && !hasWork && !pending && !queueAction;
  $("account-name").textContent = account?.email || (account?.type === "apiKey" ? "API 키 계정" : account ? "연결된 계정" : "로그인된 계정 없음");
  $("account-plan").textContent = [account?.planType, account?.workspaceName].filter(Boolean).join(" · ");
  const saved = accountState?.savedProfile;
  const sameAccount = account && saved && ["type", "email", "workspaceName"].every(key => account[key] === saved[key]);
  $("account-status").textContent = accountError || login?.message || (pending ? "외부 브라우저에서 로그인한 뒤 아래 코드를 입력하세요." : sameAccount ? "계정 저장됨 · " + new Date(saved.savedAt * 1000).toLocaleString() : account ? "로그인 정보는 이 서버에 자동 저장됩니다." : "ChatGPT 계정으로 로그인해주세요.");
  $("account-status").classList.toggle("account-error", !!accountError || login?.status === "failed");
  $("account-save").disabled = accountAction || accountLoading || !account || pending;
  $("account-login").disabled = accountAction || accountLoading || !canLogin;
  $("account-login").textContent = account ? "다시 로그인" : "ChatGPT 로그인";
  $("account-refresh").disabled = accountAction || accountLoading;
  $("account-login-pending").hidden = !pending;
  $("account-cancel").disabled = accountAction || login?.status !== "pending";
  $("account-code").textContent = login?.userCode || "준비 중…";
  const link = $("account-login-link");
  link.hidden = !login?.verificationUrl;
  if (login?.verificationUrl) link.href = login.verificationUrl;
  else link.removeAttribute("href");
  $("account-busy").hidden = !accountState || canLogin || pending;
  renderUsage();
}
async function loadAccount(refreshUsage = false, force = false) {
  if (accountLoading || accountAction) return;
  if (!refreshUsage && !force && accountLastAttempt !== null && performance.now() - accountLastAttempt < ACCOUNT_REFRESH_MS) {
    renderAccount(); return;
  }
  accountLastAttempt = performance.now();
  accountLoading = true;
  $("account-refresh").disabled = true;
  try {
    accountState = await api("/api/account", refreshUsage ? {refreshUsage: "1"} : {});
    accountError = "";
  } catch (e) { accountError = e.message; }
  finally { accountLoading = false; renderAccount(); }
}
async function accountRequest(action) {
  if (accountAction || accountLoading) return;
  accountAction = true; accountError = ""; renderAccount();
  try {
    const result = await api("/api/account/" + action, {}, true);
    accountState = {...accountState, ...result};
    if (action === "save") showToast("계정을 저장했어요");
  } catch (e) { accountError = e.message; }
  finally { accountAction = false; renderAccount(); }
  if (!accountError) await loadAccount(false, true);
}
async function saveSettings() {
  if (settingsSaving) return;
  const sid = selected;
  const values = {model: $("model-select").value, effort: $("effort-select").value, approvalPolicy: $("policy-select").value};
  settingsSaving = true;
  clearTimeout(toastTimer);
  $("toast").hidden = true;
  $("save-settings").disabled = true;
  $("exit-settings").disabled = true;
  $("settings-back").disabled = true;
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
      closeSettings(true);
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
    $("settings-back").disabled = false;
    $("save-settings").textContent = "설정 저장";
    controls.forEach((control, index) => { control.disabled = disabled[index]; });
    syncCustomSelects();
  }
}

function getSession() { return state?.sessions.find(s => s.id === selected); }
let queueSession = null, queueState = null, queueLoading = false, queueAction = null, queueVersion = 0, queueSignature = "";
function renderQueue() {
  const snapshot = queueState;
  $("queue-hint").textContent = snapshot?.activeTurnId ? "바로 적용하면 현재 작업에 전달됩니다." : "바로 적용하면 선택한 요청부터 실행합니다.";
  const signature = JSON.stringify([snapshot, queueAction?.id]);
  if (signature === queueSignature) return;
  queueSignature = signature;
  const list = $("queue-list"); list.replaceChildren();
  for (const [index, item] of (snapshot?.items || []).entries()) {
    const row = el("li", null, "queue-item"); row.dataset.id = item.id;
    row.append(el("h3", `${index + 1}번째 대기 요청`), el("pre", item.text));
    const actions = el("div", null, "queue-item-actions");
    const remove = button("삭제", () => editQueue("delete", item.id), "subtle queue-remove");
    const apply = button(queueAction?.id === item.id ? "처리 중…" : "바로 적용", () => editQueue("apply", item.id), "primary");
    remove.disabled = apply.disabled = !!queueAction;
    actions.append(remove, apply); row.append(actions); list.append(row);
  }
  $("queue-empty").hidden = !snapshot || !!snapshot.items.length;
}
async function loadQueue() {
  if (!$("queue-modal").open || queueLoading || queueAction) return;
  const sid = queueSession, version = queueVersion;
  queueLoading = true;
  try {
    const result = await api("/api/queue", {session: sid});
    if (version !== queueVersion || !$("queue-modal").open) return;
    queueState = result; $("queue-message").hidden = true; renderQueue();
  } catch (e) {
    if (version === queueVersion) { $("queue-message").textContent = e.message; $("queue-message").hidden = false; }
  } finally { queueLoading = false; }
}
function openQueue() {
  if (!selected || $("queue-modal").open) return;
  queueSession = selected; queueState = null; queueSignature = ""; queueVersion++;
  $("queue-message").textContent = "대기열을 불러오는 중…"; $("queue-message").hidden = false;
  renderQueue(); $("queue-modal").showModal(); $("queue-close").focus({preventScroll: true});
  loadQueue();
}
function closeQueue() {
  if (!$("queue-modal").open) return;
  $("queue-modal").close(); queueVersion++;
  queueSession = null; queueState = null;
  if (!$("queue-status").hidden) $("queue-status").focus({preventScroll: true});
  else $("prompt").focus({preventScroll: true});
}
async function editQueue(action, id) {
  if (queueAction || !queueState) return;
  const sid = queueSession, version = queueVersion, turn = queueState.activeTurnId;
  const sessionName = state?.sessions.find(session => session.id === sid)?.name || "대화";
  queueAction = {id}; renderQueue();
  try {
    const result = await api("/api/queue/" + action, {session: sid, id, ...(action === "apply" && turn ? {expectedTurnId: turn} : {})}, true);
    if (version === queueVersion) { queueState = result; $("queue-message").hidden = true; }
    const confirmation = action === "delete" ? "대기 요청을 삭제했어요" : result.delivery === "steer" ? "현재 작업에 바로 적용했어요" : "선택한 요청을 먼저 실행합니다";
    showToast(selected === sid ? confirmation : sessionName + " · " + confirmation);
    await refresh().catch(e => error(e.message));
  } catch (e) {
    if (version === queueVersion) { $("queue-message").textContent = e.message; $("queue-message").hidden = false; }
  } finally {
    queueAction = null;
    if (version === queueVersion) renderQueue();
  }
}
let settingsReturnTab = "chat", settingsOpener = null;
function openSettings() {
  if ($("settings").open) return;
  settingsReturnTab = currentTab;
  settingsOpener = document.activeElement;
  currentTab = "settings";
  CustomSelect.active?.close();
  hideSidebar();
  $("settings").showModal();
  document.querySelector('[data-tab="settings"]').setAttribute("aria-expanded", "true");
  $("settings-back").focus({preventScroll: true});
  loadModels().catch(e => error(e.message));
  loadAccount();
}
function restoreSettingsView() {
  CustomSelect.active?.close();
  currentTab = settingsReturnTab;
  settingsDirty = false;
  render(); error("");
  document.querySelector('[data-tab="settings"]').setAttribute("aria-expanded", "false");
  const target = settingsOpener?.isConnected && !settingsOpener.closest("#sidebar") ? settingsOpener : document.querySelector(`[data-tab="${currentTab}"]`);
  target?.focus({preventScroll: true});
  settingsOpener = null;
}
function closeSettings(force = false) {
  if (!$("settings").open || (settingsSaving && !force)) return;
  $("settings").close();
  restoreSettingsView();
}
function changeTab(tab) {
  if (tab === "settings") { openSettings(); return; }
  if ($("settings").open) { closeSettings(); if ($("settings").open) return; }
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
}
function showSidebar() {
  CustomSelect.active?.close();
  document.body.classList.remove("sidebar-collapsed");
  if (window.matchMedia("(max-width: 760px)").matches) document.body.classList.add("sidebar-open");
  $("open-sidebar").setAttribute("aria-expanded", "true");
}
function hideSidebar() {
  document.body.classList.remove("sidebar-open");
  $("open-sidebar").setAttribute("aria-expanded", String(!window.matchMedia("(max-width: 760px)").matches && !document.body.classList.contains("sidebar-collapsed")));
}
async function selectSession(sid, notify = true) {
  if (deletedSessions.has(sid)) return;
  if (sid !== queueSession) closeQueue();
  if ($("toast").classList.contains("above-prompt")) {
    clearTimeout(toastTimer);
    $("toast").hidden = true;
  }
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
function forgetSession(sid) {
  deletedSessions.add(sid);
  drafts.delete(sid);
  logs.delete(sid);
  deliveryModes.delete(sid);
}
function mayDeleteSession(entry) {
  return !!state?.canDeleteSessions && !!entry && !entry.active && !entry.queued && !entry.transferring &&
    !entry.deleting && !deletingSessions.has(entry.id) && !deletedSessions.has(entry.id) && !sending.has(entry.id) && !uploading.has(entry.id);
}
async function deleteSession(sid) {
  const entry = state?.sessions.find(session => session.id === sid);
  if (!entry || deletingSessions.has(sid)) return;
  if (!mayDeleteSession(entry)) {
    error(state?.canDeleteSessions ? "작업과 파일 첨부를 끝내고 대기열을 비운 뒤 삭제하세요." : "현재 연결에서는 세션 삭제를 사용할 수 없어요. 미니앱을 다시 열어주세요.");
    return;
  }
  CustomSelect.active?.close();
  if (!confirm("‘" + entry.name + "’ 세션을 삭제할까요?\n이 앱에서는 복원할 수 없습니다. 작업 파일은 서버에 남습니다.")) return;
  deletingSessions.add(sid);
  clearTimeout(toastTimer);
  $("toast").hidden = true;
  render();
  try {
    const result = await api("/api/session/delete", {session: sid, confirm: true}, true);
    forgetSession(sid);
    state = result;
    error("");
    if (selected === sid || !state.sessions.some(session => session.id === selected && !session.archived)) {
      await selectSession(result.selected, false);
    } else render();
    showToast("세션을 삭제했어요");
  } catch (e) {
    error(e.message);
  } finally {
    deletingSessions.delete(sid);
    render();
  }
}
async function refresh() {
  const initial = !state;
  const next = await api("/api/status");
  for (const previous of state?.sessions || []) {
    if (!next.sessions.some(session => session.id === previous.id)) forgetSession(previous.id);
  }
  state = next;
  $("connection").textContent = "Ubuntu에 연결됨";
  document.body.classList.add("connected");
  if (!selected || !state.sessions.some(session => session.id === selected && !session.archived)) {
    if (initial) {
      selected = state.selected;
      rememberSession(selected);
      restoreDraft(selected);
      render();
    } else await selectSession(state.selected, false);
  } else render();
}
function render() {
  if (!state) return;
  const session = getSession(); if (!session) return;
  $("session-name").textContent = session.name;
  const running = state.sessions.filter(entry => entry.active && !entry.archived).length;
  $("running-sessions").textContent = running > 1 ? running + "개 동시 실행" : running ? "1개 실행 중" : "로컬";
  $("session-status").hidden = !session.active;
  $("session-activity-text").textContent = session.name + " · 작업 실행 중";
  $("empty").hidden = !!session.active || $("transcript").childElementCount > 0;
  const queued = Math.max(0, Number(session.queued) || 0), showQueue = queued > 0 || !session.active;
  $("queue-status").textContent = queued ? `${queued}개 대기` : "대기열 보기";
  $("queue-status").hidden = !showQueue;
  $("queue-status").setAttribute("aria-label", queued ? `대기 요청 ${queued}개 보기` : "대기열 보기");
  $("prompt-container").classList.toggle("has-queue-trigger", showQueue);
  document.body.classList.toggle("has-queue", showQueue);
  const model = models.find(m => (m.model || m.id) === session.model);
  $("composer-model").textContent = model?.displayName || session.model || "Codex 기본 모델";
  const effort = session.effort || session.effective?.reasoningEffort;
  $("composer-effort").textContent = ({minimal: "최소", low: "낮음", medium: "보통", high: "높음", xhigh: "매우 높음", max: "최대", ultra: "최상"})[effort] || effort || "기본 추론";
  const fullAccess = session.policy === "never" && session.sandbox === "danger-full-access";
  $("composer-policy").textContent = fullAccess ? "항상 승인 · 전체 액세스" : ({"on-request": "필요 시 승인", untrusted: "작업 승인 필요", never: "추가 권한 요청 거절"})[session.policy] || "기본 권한";
  document.body.classList.toggle("working", session.active);
  $("stop-job").hidden = !session.active;
  $("delete-session").disabled = !mayDeleteSession(session);
  $("delete-session").title = session.active || session.queued ? "작업을 중단하고 대기열을 비운 뒤 삭제하세요" : "세션 삭제";
  $("effective-settings").textContent = JSON.stringify(session.effective || {}, null, 2);
  $("retry-job").hidden = !session.interrupted;
  $("console-output").textContent = logs.get(selected) || "실행 로그가 여기에 표시됩니다.";
  const entries = state.sessions.filter(s => (showArchived ? s.archived : !s.archived) && (s.name + " " + s.id).toLocaleLowerCase().includes(sessionQuery));
  const navSignature = JSON.stringify([selected, showArchived, sessionQuery, entries.map(s => [s.id, s.name, s.archived, s.active, s.queued, mayDeleteSession(s)])]);
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
      node.append(info);
      const row = el("div", null, "session-row");
      const remove = button("삭제", () => deleteSession(entry.id), "session-delete");
      remove.disabled = !mayDeleteSession(entry);
      remove.setAttribute("aria-label", entry.name + " 세션 삭제");
      remove.title = entry.active || entry.queued ? "작업을 중단하고 대기열을 비운 뒤 삭제하세요" : "세션 삭제";
      row.append(node, remove);
      nav.append(row);
    }
    if (!entries.length) nav.append(el("p", sessionQuery ? "검색 결과가 없습니다." : showArchived ? "보관된 대화가 없습니다." : "새 대화를 시작하세요.", "session-empty"));
  }
  const approvals = state.approvals.filter(a => a.session === selected);
  const signature = selected + ":" + approvals.map(a => a.key).join(",");
  if (signature !== approvalSignature) {
    approvalSignature = signature;
    renderApprovals(approvals);
    $("approval-panel").hidden = !approvals.length;
    setApprovalPanel(approvals.length > 0);
  }
  if (!settingsDirty && document.activeElement?.tagName !== "SELECT") {
    $("model-select").value = session.model || "";
    renderEfforts(session.effort);
    $("policy-select").value = fullAccess ? "full-access" : typeof session.policy === "string" ? session.policy : "";
  }
  syncCustomSelects();
  fitPrompt();
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
  const params = event.params || {};
  if (event.method === "cloudex/accountChanged") {
    if (params.usage && accountState) { accountState.usage = params.usage; renderAccount(); }
    if (params.authChanged && (currentTab === "settings" || accountState?.login?.status === "pending")) loadAccount(false, true);
    return;
  }
  const sid = event.cloudex_session || selected;
  if (event.method === "cloudex/sessionDeleted") {
    forgetSession(params.session || sid);
    fitPrompt();
    refresh().catch(e => error(e.message));
    return;
  }
  if (deletedSessions.has(sid)) return;
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
$("open-sidebar").onclick = showSidebar;
let sidebarSwipe = null;
document.addEventListener("touchstart", event => {
  sidebarSwipe = null;
  if (!window.matchMedia("(max-width: 760px)").matches || document.body.classList.contains("sidebar-open") || event.touches.length !== 1) return;
  const target = event.target;
  if (!target.closest?.("#workspace") || target.closest('input, textarea, select, button, a, [role="combobox"], [role="listbox"], [role="slider"], [contenteditable]:not([contenteditable="false"])') || window.getSelection()?.toString()) return;
  for (let node = target; node && node !== document.body; node = node.parentElement) {
    if (node.scrollWidth > node.clientWidth + 1 && ["auto", "scroll"].includes(getComputedStyle(node).overflowX)) return;
  }
  const touch = event.touches[0];
  sidebarSwipe = {id: touch.identifier, x: touch.clientX, y: touch.clientY, started: performance.now()};
}, {passive: true});
document.addEventListener("touchmove", event => {
  if (!sidebarSwipe) return;
  const touch = Array.from(event.touches).find(touch => touch.identifier === sidebarSwipe.id);
  if (!touch || event.touches.length !== 1) { sidebarSwipe = null; return; }
  const dx = touch.clientX - sidebarSwipe.x, dy = Math.abs(touch.clientY - sidebarSwipe.y);
  if (dx < -12 || (dy > 12 && dy >= Math.abs(dx)) || performance.now() - sidebarSwipe.started > 800) { sidebarSwipe = null; return; }
  if (dx > 12 && dx > dy * 1.5 && event.cancelable) event.preventDefault();
}, {passive: false});
document.addEventListener("touchend", event => {
  const swipe = sidebarSwipe;
  sidebarSwipe = null;
  if (!swipe || event.touches.length || !window.matchMedia("(max-width: 760px)").matches || window.getSelection()?.toString()) return;
  const touch = Array.from(event.changedTouches).find(touch => touch.identifier === swipe.id);
  if (!touch || performance.now() - swipe.started > 800) return;
  const dx = touch.clientX - swipe.x, dy = Math.abs(touch.clientY - swipe.y);
  if (dx >= 64 && dx > dy * 1.5) showSidebar();
}, {passive: true});
document.addEventListener("touchcancel", () => { sidebarSwipe = null; }, {passive: true});
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
  if (event.key === "Escape" && $("queue-modal").open) { event.preventDefault(); closeQueue(); return; }
  if (event.key === "Escape" && $("settings").open) {
    event.preventDefault();
    if (CustomSelect.active) CustomSelect.active.close(); else closeSettings();
    return;
  }
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
$("delete-session").onclick = () => deleteSession(selected);
$("archive-session").onclick = async () => { if (confirm("이 세션을 보관할까요? 대화와 파일은 유지됩니다.")) { try { const result = await command(`/archive ${selected}`); await selectSession(result.selected, false); } catch(e) { error(e.message); } } };
document.querySelectorAll("[data-tab]").forEach(n => n.onclick = () => changeTab(n.dataset.tab));
$("model-shortcut").onclick = $("effort-shortcut").onclick = $("policy-shortcut").onclick = () => changeTab("settings");
$("hide-approvals").onclick = () => setApprovalPanel($("approvals").hidden);
$("clear-console").onclick = () => { logs.set(selected, ""); render(); };
$("refresh-files").onclick = () => loadFiles().catch(e => error(e.message));
$("older-history").onclick = () => loadHistory(true).catch(e => error(e.message));
$("model-select").onchange = () => { settingsDirty = true; renderEfforts(""); };
$("effort-select").onchange = $("policy-select").onchange = () => { settingsDirty = true; };
$("save-settings").onclick = saveSettings;
$("queue-status").onclick = openQueue;
$("queue-close").onclick = closeQueue;
$("queue-modal").addEventListener("cancel", event => { event.preventDefault(); closeQueue(); });
$("account-save").onclick = () => accountRequest("save");
$("account-login").onclick = () => accountRequest("login");
$("account-cancel").onclick = () => accountRequest("cancel");
$("account-refresh").onclick = () => loadAccount(true);
$("account-login-link").onclick = event => {
  if (telegram?.openLink) { event.preventDefault(); telegram.openLink(event.currentTarget.href); }
};
$("settings-back").onclick = $("exit-settings").onclick = () => closeSettings();
$("settings").addEventListener("cancel", event => {
  event.preventDefault();
  if (CustomSelect.active) CustomSelect.active.close(); else closeSettings();
});
$("settings").addEventListener("close", () => {
  if (!$("settings").open && currentTab === "settings") restoreSettingsView();
});
$("retry-job").onclick = () => command("/retry").catch(e => error(e.message));
$("stop-job").onclick = () => command("/stop").catch(e => error(e.message));
$("prompt-form").onsubmit = async event => {
  event.preventDefault();
  const input = $("prompt"), raw = input.value, text = raw.trim(), sid = selected;
  if (!sid || !text || !auth || sending.has(sid) || uploading.has(sid) || deletingSessions.has(sid) || deletedSessions.has(sid)) return;
  const mode = $("delivery-mode").value;
  if (mode === "steer" && !state?.messageModes?.includes("steer")) {
    error("현재 연결에서는 바로 적용을 사용할 수 없어요. 미니앱을 다시 열어주세요.");
    return;
  }
  const expectedTurnId = mode === "steer" ? state?.sessions.find(entry => entry.id === sid)?.activeTurnId || null : null;
  saveDraft(sid);
  sending.add(sid);
  fitPrompt();
  try {
    const result = await api("/api/command", {text, session: sid, mode, ...(expectedTurnId ? {expectedTurnId} : {})}, true);
    if (selected === sid) saveDraft(sid);
    if (drafts.get(sid)?.text === raw) drafts.set(sid, {text: "", attachment: ""});
    if (selected === sid) {
      error("");
      const confirmation = {queue: "대기열에 추가했어요", steer: "현재 작업에 전달했어요", start: "새 작업을 접수했어요", command: "명령을 적용했어요"}[result.delivery];
      if (confirmation) showToast(confirmation, result.delivery === "queue");
    }
    refresh().catch(e => error(e.message));
  } catch (e) {
    const name = state?.sessions.find(entry => entry.id === sid)?.name || sid;
    error(selected === sid ? e.message : name + " 전송 실패: " + e.message);
  } finally {
    sending.delete(sid);
    if (selected === sid) restoreDraft(sid);
  }
};
$("delivery-mode").onchange = () => {
  deliveryModes.set(selected, $("delivery-mode").value);
  fitPrompt();
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
  ["model-select", "effort-select", "policy-select", "delivery-mode"].map($).forEach(enhanceSelect);
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
      if ($("queue-modal").open) loadQueue();
      if (currentTab === "settings" || accountState?.login?.status === "pending") await loadAccount();
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
