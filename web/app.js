"use strict";

const ui = {
  form: document.querySelector("#chat-form"),
  message: document.querySelector("#message"),
  requireApproval: document.querySelector("#require-approval"),
  submit: document.querySelector("#submit-button"),
  progress: document.querySelector("#progress"),
  approval: document.querySelector("#approval-panel"),
  approve: document.querySelector("#approve-button"),
  reject: document.querySelector("#reject-button"),
  result: document.querySelector("#result"),
  resultStatus: document.querySelector("#result-status"),
  answer: document.querySelector("#answer"),
  badges: document.querySelector("#badges"),
  planBlock: document.querySelector("#plan-block"),
  plan: document.querySelector("#plan"),
  warningBlock: document.querySelector("#warning-block"),
  warnings: document.querySelector("#warnings"),
  citationBlock: document.querySelector("#citation-block"),
  citations: document.querySelector("#citations"),
  trace: document.querySelector("#trace"),
  serviceState: document.querySelector("#service-state"),
  runtimeMode: document.querySelector("#runtime-mode"),
  runtimeScenic: document.querySelector("#runtime-scenic"),
  runtimeTools: document.querySelector("#runtime-tools"),
  runtimeLlm: document.querySelector("#runtime-llm"),
};

const state = { threadId: null, busy: false };

function uniqueId(prefix) {
  const value = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
  return `${prefix}-${value}`;
}

function setBusy(value) {
  state.busy = value;
  ui.submit.disabled = value;
  ui.approve.disabled = value;
  ui.reject.disabled = value;
  ui.submit.querySelector("span").textContent = value ? "正在处理" : "开始规划";
}

function setProgress(stage) {
  const order = ["retrieve", "plan", "verify"];
  const active = Math.max(0, order.indexOf(stage));
  ui.progress.hidden = false;
  ui.progress.querySelectorAll("li").forEach((item, index) => {
    item.classList.toggle("done", index < active || stage === "done");
    item.classList.toggle("active", index === active && stage !== "done");
  });
}

function resetOutput() {
  ui.approval.hidden = true;
  ui.result.hidden = true;
  ui.planBlock.hidden = true;
  ui.warningBlock.hidden = true;
  ui.citationBlock.hidden = true;
  ui.answer.textContent = "";
  ui.badges.replaceChildren();
  ui.plan.replaceChildren();
  ui.warnings.replaceChildren();
  ui.citations.replaceChildren();
  ui.trace.textContent = "";
}

function appendTextElement(parent, tag, text, className = "") {
  const element = document.createElement(tag);
  element.textContent = String(text);
  if (className) element.className = className;
  parent.append(element);
  return element;
}

function addBadge(label, value) {
  if (value === undefined || value === null || value === "") return;
  appendTextElement(ui.badges, "span", `${label}: ${value}`, "badge");
}

function renderPlan(plan) {
  const stops = Array.isArray(plan?.stops) ? plan.stops : [];
  ui.plan.replaceChildren();
  ui.planBlock.hidden = stops.length === 0;
  stops.forEach((stop) => {
    const item = document.createElement("article");
    item.className = "timeline-item";
    appendTextElement(item, "strong", stop.name ?? "未命名地点");
    const schedule = stop.start_time && stop.end_time
      ? `${stop.start_time}–${stop.end_time} · `
      : "";
    const travel = Number(stop.travel_from_previous_minutes || 0) > 0
      ? ` · 前序交通约 ${stop.travel_from_previous_minutes} 分钟`
      : "";
    appendTextElement(
      item,
      "span",
      `${schedule}${stop.duration_minutes ?? "?"} 分钟 · ${stop.reason ?? "行程候选"}${travel}`,
    );
    ui.plan.append(item);
  });
}

function renderWarnings(result) {
  const violations = Array.isArray(result?.violations) ? result.violations : [];
  const assumptions = Array.isArray(result?.plan?.assumptions) ? result.plan.assumptions : [];
  const messages = [
    ...violations.map((item) => item.message ?? item.code).filter(Boolean),
    ...assumptions,
  ];
  ui.warnings.replaceChildren();
  ui.warningBlock.hidden = messages.length === 0;
  messages.forEach((message) => appendTextElement(ui.warnings, "li", message));
}

function renderCitations(citations) {
  const items = Array.isArray(citations) ? citations : [];
  ui.citations.replaceChildren();
  ui.citationBlock.hidden = items.length === 0;
  items.forEach((citation, index) => {
    const link = document.createElement("a");
    link.className = "citation";
    link.href = citation.url || "#";
    if (citation.url) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
    appendTextElement(link, "span", String(index + 1).padStart(2, "0"), "citation-index");
    const copy = document.createElement("span");
    appendTextElement(copy, "strong", citation.title || citation.source_id || "来源");
    appendTextElement(copy, "small", citation.verified_at || citation.url || "本地演示资料");
    link.append(copy);
    appendTextElement(link, "span", "↗", "citation-arrow");
    ui.citations.append(link);
  });
}

function renderTrace(envelope, result) {
  const details = {
    thread_id: envelope?.thread_id ?? state.threadId,
    idempotency_key: envelope?.idempotency_key,
    idempotency_replayed: envelope?.idempotency_replayed ?? false,
    trace: result?.trace ?? [],
    retrieval_trace: result?.retrieval_trace ?? {},
    answer_trace: result?.answer_trace ?? {},
    planning_trace: result?.planning_trace ?? {},
    governance_trace: result?.governance_trace ?? {},
  };
  ui.trace.textContent = JSON.stringify(details, null, 2);
}

function renderResult(envelope) {
  const result = envelope?.result ?? envelope;
  const status = result?.answer_status ?? envelope?.status ?? "completed";
  ui.result.hidden = false;
  ui.resultStatus.textContent = status;
  ui.resultStatus.className = `result-status ${status}`;
  ui.answer.textContent = result?.answer || "执行完成，但没有返回自然语言答案。";
  ui.badges.replaceChildren();
  addBadge("intent", result?.intent);
  addBadge("constraint", result?.constraint_status);
  addBadge("tool calls", `${result?.tool_call_count ?? 0}/${result?.tool_call_limit ?? 0}`);
  addBadge("revisions", result?.revision_count ?? 0);
  if (result?.plan) {
    addBadge("duration", `${result.plan.duration_minutes ?? 0} min`);
    addBadge("estimated cost", `¥${result.plan.estimated_cost ?? 0}`);
  }
  renderPlan(result?.plan);
  renderWarnings(result);
  renderCitations(result?.citations);
  renderTrace(envelope, result);
  setProgress("done");
  ui.result.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderFailure(payload) {
  ui.result.hidden = false;
  ui.resultStatus.textContent = "failed";
  ui.resultStatus.className = "result-status failed";
  ui.answer.textContent = payload?.message || payload?.error?.message || "请求执行失败，请稍后重试。";
  ui.trace.textContent = JSON.stringify(payload, null, 2);
  setProgress("done");
}

function showApproval(envelope) {
  state.threadId = envelope.thread_id;
  ui.approval.hidden = false;
  setProgress("verify");
  setBusy(false);
}

function parseEventBlock(block) {
  let event = "message";
  const data = [];
  block.split("\n").forEach((line) => {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  });
  if (data.length === 0) return null;
  try {
    return { event, payload: JSON.parse(data.join("\n")) };
  } catch {
    return { event: "failed", payload: { message: "服务返回了无法解析的流式数据。" } };
  }
}

async function consumeSSE(response, handle) {
  if (!response.ok || !response.body) {
    let message = `HTTP ${response.status}`;
    try {
      const error = await response.json();
      message = error.detail || message;
    } catch { /* response is not JSON */ }
    throw new Error(message);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    buffer = buffer.replaceAll("\r\n", "\n");
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() || "";
    blocks.map(parseEventBlock).filter(Boolean).forEach(handle);
    if (done) break;
  }
  const finalEvent = parseEventBlock(buffer.trim());
  if (finalEvent) handle(finalEvent);
}

async function startSession(message) {
  resetOutput();
  setBusy(true);
  setProgress("retrieve");
  state.threadId = uniqueId("web");
  const response = await fetch("/v1/sessions/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({
      message,
      scenic_id: "zhuhai-chimelong",
      thread_id: state.threadId,
      idempotency_key: uniqueId("start"),
      require_approval: ui.requireApproval.checked,
    }),
  });
  await consumeSSE(response, ({ event, payload }) => {
    if (event === "started") setProgress("retrieve");
    if (event === "approval_required") showApproval(payload);
    if (event === "completed") renderResult(payload);
    if (event === "failed") renderFailure(payload);
  });
  if (ui.approval.hidden) setBusy(false);
}

async function decideApproval(approved) {
  if (!state.threadId) return;
  setBusy(true);
  setProgress("verify");
  try {
    const response = await fetch("/v1/sessions/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        thread_id: state.threadId,
        idempotency_key: uniqueId("resume"),
        approved,
        reason: approved ? "用户在演示页面确认" : "用户在演示页面拒绝",
      }),
    });
    const envelope = await response.json();
    if (!response.ok) throw new Error(envelope.detail || `HTTP ${response.status}`);
    ui.approval.hidden = true;
    renderResult(envelope);
  } catch (error) {
    renderFailure({ message: error.message });
  } finally {
    setBusy(false);
  }
}

async function loadHealth() {
  try {
    const response = await fetch("/health/ready", { headers: { Accept: "application/json" } });
    const health = await response.json();
    ui.serviceState.className = `service-state ${response.ok ? "ready" : "failed"}`;
    ui.serviceState.lastElementChild.textContent = response.ok ? "服务已就绪" : "服务未就绪";
    ui.runtimeMode.textContent = health.mode ?? "—";
    ui.runtimeScenic.textContent = health.scenic_id ?? "—";
    ui.runtimeTools.textContent = health.tool_mode ?? "—";
    ui.runtimeLlm.textContent = health.llm_provider ?? "—";
  } catch {
    ui.serviceState.className = "service-state failed";
    ui.serviceState.lastElementChild.textContent = "服务不可用";
  }
}

ui.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy) return;
  try {
    await startSession(ui.message.value.trim());
  } catch (error) {
    renderFailure({ message: error.message });
    setBusy(false);
  }
});

document.querySelectorAll("[data-query]").forEach((button) => {
  button.addEventListener("click", () => {
    ui.message.value = button.dataset.query;
    ui.message.focus();
  });
});
ui.approve.addEventListener("click", () => decideApproval(true));
ui.reject.addEventListener("click", () => decideApproval(false));
loadHealth();
