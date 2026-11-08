"use strict";

// 状態を変える操作には、サーバー側の CSRF 対策と同じヘッダーを付ける
const CSRF = { "X-Requested-With": "godoc-web" };
const POLL_MS = 2000;
const TASK_LABELS = { doc: "設計書", comment: "コメント追記" };
const LANGUAGE_LABELS = { go: "Go", csharp: "C#", typescript: "TypeScript" };
const $ = (id) => document.getElementById(id);

const state = {
  admin: false,
  tab: "run",
  workspace: { repo: null, out: null },
  running: false,
  logRunId: null,   // ログ欄に表示中の run_id
  logOffset: 0,
  following: true,  // 実行中のジョブのログを追いかけるか（履歴を選ぶと止める）
  outputs: [],
  targets: null,    // サーバーから読んだ対象の設定
};

// --- 共通 ---------------------------------------------------------------------------------

async function api(path, options = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...options });
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const detail = (await res.json()).detail;
      // 入力の形式エラー（422）は配列で返るので、読める形にする
      message = Array.isArray(detail) ? detail.map((d) => `${d.loc?.slice(1).join(".")}: ${d.msg}`).join(" / ") : detail || message;
    } catch (_) { /* 本文が JSON でない */ }
    const err = new Error(message);
    err.status = res.status;
    throw err;
  }
  const type = res.headers.get("content-type") || "";
  return type.includes("application/json") ? res.json() : res.text();
}

function send(method, path, body) {
  return api(path, {
    method,
    headers: { "Content-Type": "application/json", ...CSRF },
    body: JSON.stringify(body ?? {}),
  });
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  for (const child of children) if (child != null) node.append(child);
  return node;
}

function fmtTime(iso) {
  return iso ? iso.replace("T", " ") : "";
}

function fmtSize(bytes) {
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;
}

function message(target, text, ok = false) {
  $(target).className = ok ? "success small" : "error small";
  $(target).textContent = text;
}

function lines(text) {
  return text.split("\n").map((s) => s.trim()).filter(Boolean);
}

// 管理者だけが使える操作（data-admin）と、実行中は変えられない設定の入力欄をまとめて切り替える
function applyPermissions() {
  for (const node of document.querySelectorAll("[data-admin]")) {
    const lockedWhileRunning = node.closest("#tab-targets, #tab-skills") || node.dataset.pick;
    node.disabled = !state.admin || Boolean(lockedWhileRunning && state.running);
  }
  for (const node of document.querySelectorAll(".admin-note")) node.classList.toggle("hidden", state.admin);
  document.querySelector('[data-tab="skills"]').classList.toggle("hidden", !state.admin);
  if (!state.admin && state.tab === "skills") showTab("run");
}

// --- タブ ---------------------------------------------------------------------------------

function showTab(tab) {
  state.tab = tab;
  for (const button of $("tabs").querySelectorAll("button")) button.setAttribute("aria-selected", String(button.dataset.tab === tab));
  for (const name of ["run", "targets", "skills"]) $(`tab-${name}`).classList.toggle("hidden", name !== tab);
  if (tab === "targets") loadTargets();
  if (tab === "skills") loadSkills();
}

$("tabs").addEventListener("click", (e) => {
  const tab = e.target.closest("button")?.dataset.tab;
  if (tab) showTab(tab);
});

// --- ログイン -----------------------------------------------------------------------------

async function refreshMe() {
  const me = await api("/api/me");
  state.admin = me.admin;
  $("login-form").classList.toggle("hidden", me.admin);
  $("logged-in").classList.toggle("hidden", !me.admin);
  const canView = me.admin || !me.view_requires_login;
  $("tabs").classList.toggle("hidden", !canView);
  $("login-required").classList.toggle("hidden", canView);
  if (!canView) for (const name of ["run", "targets", "skills"]) $(`tab-${name}`).classList.add("hidden");
  else showTab(state.tab);
  return canView;
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("auth-error").textContent = "";
  try {
    await send("POST", "/api/login", { password: $("password").value });
    $("password").value = "";
    await start();
  } catch (err) {
    $("auth-error").textContent = err.message;
  }
});

$("logout").addEventListener("click", async () => {
  await send("POST", "/api/logout");
  await start();
});

// --- フォルダ選択ダイアログ ------------------------------------------------------------------

const dialog = { kind: null, path: null };

async function openFolderDialog(kind) {
  dialog.kind = kind;
  $("dlg-title").textContent = kind === "repo" ? "対象フォルダを選択" : "出力先フォルダを選択";
  $("dlg-new").classList.toggle("hidden", kind !== "out");
  $("dlg-new-name").value = "";
  $("dlg-error").textContent = "";
  const current = state.workspace[kind];
  // 出力先がまだない（runner が作る）場合は、親フォルダから開く
  await browse(current).catch(() => browse(current ? current.replace(/[\\/][^\\/]+$/, "") : null)).catch(() => browse(null));
  $("folder-dialog").showModal();
}

async function browse(path) {
  const query = path ? `?path=${encodeURIComponent(path)}` : "";
  const res = await api(`/api/fs${query}`);
  dialog.path = res.path;
  $("dlg-path").textContent = res.path ?? "（起点を選んでください）";
  $("dlg-up").disabled = res.path === null;
  $("dlg-up").onclick = () => browse(res.parent).catch((err) => { $("dlg-error").textContent = err.message; });
  $("dlg-ok").disabled = res.path === null;
  const items = res.dirs.map((d) => el("button", {
    type: "button", className: "mono", textContent: `📁 ${d.name}`,
    onclick: () => browse(d.path).catch((err) => { $("dlg-error").textContent = err.message; }),
  }));
  if (items.length === 0) items.push(el("div", { className: "muted small", textContent: "サブフォルダはありません" }));
  if (res.truncated) items.push(el("div", { className: "muted small", textContent: "（多いため一部だけ表示しています）" }));
  $("dlg-dirs").replaceChildren(...items);
  $("dlg-error").textContent = "";
}

$("dlg-cancel").addEventListener("click", () => $("folder-dialog").close());

$("dlg-ok").addEventListener("click", async () => {
  let path = dialog.path;
  const newName = $("dlg-new-name").value.trim();
  if (dialog.kind === "out" && newName) {
    if (/[\\/]|^\.\.?$/.test(newName)) {
      $("dlg-error").textContent = "フォルダ名に / \\ . .. は使えません";
      return;
    }
    path = `${path.replace(/[\\/]$/, "")}/${newName}`;
  }
  const next = { ...state.workspace, [dialog.kind]: path };
  if (!next.repo || !next.out) {
    // もう一方が未設定の間は画面上だけで覚えておき、両方そろったら保存する
    state.workspace = next;
    renderWorkspace();
    message("workspace-msg", `${next.repo ? "出力先" : "対象フォルダ"}も選んでください`);
    $("folder-dialog").close();
    return;
  }
  try {
    state.workspace = await send("PUT", "/api/workspace", next);
    renderWorkspace();
    message("workspace-msg", "保存しました", true);
    $("folder-dialog").close();
    await Promise.all([refreshHistory(), refreshOutputs()]);
  } catch (err) {
    $("dlg-error").textContent = err.message;
  }
});

for (const button of document.querySelectorAll("[data-pick]")) {
  button.addEventListener("click", () => openFolderDialog(button.dataset.pick).catch((err) => message("workspace-msg", err.message)));
}

function renderWorkspace() {
  $("repo-path").textContent = state.workspace.repo || "未設定";
  $("out-path").textContent = state.workspace.out || "未設定";
}

// --- 実行 ---------------------------------------------------------------------------------

async function loadConfig() {
  const config = await api("/api/config");
  state.workspace = config.workspace;
  renderWorkspace();
  const checked = new Set([...$("tasks").querySelectorAll("input:checked")].map((i) => i.value));
  const first = $("tasks").children.length === 0;
  $("tasks").replaceChildren(...config.tasks.map((task) =>
    el("label", {}, el("input", { type: "checkbox", value: task, checked: first || checked.has(task) }), TASK_LABELS[task] || task)));
  if (!config.tasks.includes("comment")) {
    $("tasks").append(el("span", { className: "muted small", textContent: "（コメント追記はスキル未登録）" }));
  }
}

$("run-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("run-error").textContent = "";
  const tasks = [...$("tasks").querySelectorAll("input:checked")].map((i) => i.value);
  if (tasks.length === 0) {
    $("run-error").textContent = "タスクを1つ以上選んでください";
    return;
  }
  if ($("force").checked && !confirm("変更のないファイルも作り直します（設計書の版が1つ上がります）。よろしいですか？")) return;
  try {
    const job = await send("POST", "/api/runs", { tasks, limit: Number($("limit").value) || 0, force: $("force").checked });
    followRun(job.run_id);
    await refreshStatus();
  } catch (err) {
    $("run-error").textContent = err.message;
  }
});

$("stop").addEventListener("click", async () => {
  if (!confirm("実行を停止しますか？（完了済みのファイルは残り、再実行で続きから処理されます）")) return;
  $("run-error").textContent = "";
  try {
    await send("POST", "/api/runs/stop");
    await refreshStatus();
  } catch (err) {
    $("run-error").textContent = err.message;
  }
});

// --- 状態・ログ -----------------------------------------------------------------------------

function badge(text, cls) {
  return el("span", { className: `badge ${cls}`, textContent: text });
}

async function refreshStatus() {
  const { job, busy_elsewhere: busyElsewhere } = await api("/api/status");
  const running = job?.running ?? false;
  let status;
  if (running) status = badge(job.stop_requested ? "停止中…" : "実行中", "running");
  else if (busyElsewhere) status = badge("他の実行が進行中（夜間バッチ・コマンド）", "warn");
  else if (job && job.exit_code === 0) status = badge("完了", "ok");
  else if (job && job.stop_requested) status = badge("停止しました", "warn");
  else if (job) status = badge(`失敗（終了コード ${job.exit_code}）`, "fail");
  else status = badge("待機中", "idle");
  $("status").replaceChildren(status);
  $("status-detail").textContent = job
    ? `run_id=${job.run_id} / ${job.tasks.map((t) => TASK_LABELS[t] || t).join("・") || "既定のタスク"}`
      + `${job.limit ? ` / 上限 ${job.limit} 件` : ""}${job.force ? " / 再生成" : ""}`
      + ` / 開始 ${fmtTime(job.started_at)}${job.finished_at ? ` / 終了 ${fmtTime(job.finished_at)}` : ""}`
      + `\n対象 ${job.repo}\n出力 ${job.out}`
    : "";
  $("status-detail").style.whiteSpace = "pre-wrap";

  state.running = running;
  applyPermissions();
  for (const input of $("tasks").querySelectorAll("input")) input.disabled = !state.admin;
  $("start").disabled = !state.admin || running || busyElsewhere;
  $("stop").disabled = !state.admin || !running || job.stop_requested;

  if (job && state.following && state.logRunId !== job.run_id) followRun(job.run_id);
  return job;
}

function followRun(runId) {
  state.following = true;
  showLog(runId);
}

function showLog(runId) {
  state.logRunId = runId;
  state.logOffset = 0;
  $("log").textContent = "";
  $("log-title").textContent = runId;
  for (const row of $("history").children) row.classList.toggle("selected", row.dataset.runId === runId);
}

async function pollLog() {
  if (!state.logRunId) return;
  const runId = state.logRunId;
  try {
    // 未読分がなくなるまで続けて読む（長いログを開いたときに一度で追いつく）
    for (;;) {
      const chunk = await api(`/api/runs/${encodeURIComponent(runId)}/log?offset=${state.logOffset}`);
      if (state.logRunId !== runId) return;
      if (chunk.text) {
        const box = $("log");
        const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
        box.append(chunk.text);
        if (atBottom) box.scrollTop = box.scrollHeight;
      }
      const advanced = chunk.offset > state.logOffset;
      state.logOffset = chunk.offset;
      if (chunk.eof || !advanced) return;
    }
  } catch (err) {
    if (err.status === 404) $("log").textContent = "（ログファイルはありません。計測ログ .jsonl だけの実行です）";
  }
}

// --- 履歴・生成物 ---------------------------------------------------------------------------

async function refreshHistory() {
  const runs = await api("/api/runs");
  $("history").replaceChildren(...runs.map((run) => {
    const counts = Object.entries(run.counts).map(([k, v]) => `${k}=${v}`).join(" ");
    const name = run.has_log
      ? el("a", { textContent: run.run_id, onclick: () => { state.following = false; showLog(run.run_id); pollLog(); } })
      : el("span", { textContent: run.run_id });
    const row = el("tr", {},
      el("td", { className: "mono" }, name),
      el("td", { textContent: fmtTime(run.updated_at) }),
      el("td", { className: "mono", textContent: counts || "-" }));
    row.dataset.runId = run.run_id;
    row.classList.toggle("selected", run.run_id === state.logRunId);
    return row;
  }));
}

async function refreshOutputs() {
  state.outputs = await api("/api/outputs");
  renderOutputs();
}

function renderOutputs() {
  const filter = $("filter").value.trim().toLowerCase();
  const items = state.outputs.filter((o) => !filter || o.path.toLowerCase().includes(filter));
  $("outputs").replaceChildren(...items.slice(0, 500).map((o) => el("tr", {},
    el("td", { className: "mono" }, el("a", { textContent: o.path, onclick: () => openOutput(o.path) })),
    el("td", { textContent: fmtTime(o.updated_at) }))));
  if (items.length > 500) {
    $("outputs").append(el("tr", {}, el("td", { colSpan: 2, className: "muted", textContent: `ほか ${items.length - 500} 件（絞り込んでください）` })));
  }
}

$("filter").addEventListener("input", renderOutputs);

async function openOutput(path) {
  const query = `path=${encodeURIComponent(path)}`;
  $("viewer-title").textContent = path;
  $("viewer").textContent = "読み込み中…";
  $("viewer-download").href = `/api/outputs/file?${query}&download=true`;
  $("viewer-section").classList.remove("hidden");
  try {
    $("viewer").textContent = await api(`/api/outputs/file?${query}`);
  } catch (err) {
    $("viewer").textContent = err.message;
  }
  $("viewer-section").scrollIntoView({ behavior: "smooth" });
}

// --- 対象の設定（targets.toml） ----------------------------------------------------------------

async function loadTargets() {
  try {
    state.targets = await api("/api/targets");
    renderTargets(state.targets.config);
    $("targets-path").textContent = state.targets.write_path;
    $("targets-example-note").classList.toggle("hidden", !state.targets.is_example);
    $("targets-msg").textContent = "";
  } catch (err) {
    message("targets-msg", err.message);
  }
}

function renderTargets(config) {
  $("global-exclude").value = (config.exclude || []).join("\n");
  $("languages").replaceChildren(...config.languages.map((lang) => {
    const enabled = el("input", { type: "checkbox", checked: lang.enabled !== false });
    enabled.dataset.admin = "";
    enabled.dataset.field = "enabled";
    const field = (name, rows, value, placeholder) => {
      const area = el("textarea", { rows, value: (value || []).join("\n"), placeholder });
      area.dataset.admin = "";
      area.dataset.field = name;
      return area;
    };
    const card = el("div", { className: "lang" },
      el("div", { className: "row" },
        el("label", { style: "font-weight:600" }, enabled, LANGUAGE_LABELS[lang.name] || lang.name),
        el("span", { className: "muted small mono", textContent: `name = ${lang.name}` })),
      el("div", { className: "lang-body grid3" },
        el("div", {}, el("h3", { textContent: "拡張子" }, el("span", { className: "muted small", textContent: "（1行に1つ。. で始める）" })),
          field("extensions", 4, lang.extensions, ".go")),
        el("div", {}, el("h3", { textContent: "対象（include）" }, el("span", { className: "muted small", textContent: "（どれかに一致すれば対象）" })),
          field("include", 4, lang.include, "**/?*_api/pkg/handler/**")),
        el("div", {}, el("h3", { textContent: "除外（exclude）" }, el("span", { className: "muted small", textContent: "（この言語だけ）" })),
          field("exclude", 4, lang.exclude, "**/*_test.go"))));
    card.dataset.name = lang.name;
    const sync = () => card.classList.toggle("disabled", !enabled.checked);
    enabled.addEventListener("change", sync);
    sync();
    return card;
  }));
  applyPermissions();
}

function collectTargets() {
  return {
    exclude: lines($("global-exclude").value),
    languages: [...$("languages").children].map((card) => {
      const get = (name) => card.querySelector(`[data-field="${name}"]`);
      return {
        name: card.dataset.name,
        enabled: get("enabled").checked,
        extensions: lines(get("extensions").value),
        include: lines(get("include").value),
        exclude: lines(get("exclude").value),
      };
    }),
  };
}

$("targets-preview").addEventListener("click", async () => {
  $("targets-msg").textContent = "";
  try {
    const res = await send("POST", "/api/targets/preview", { config: collectTargets() });
    $("preview-summary").textContent = `${res.total} 件${res.truncated ? `（先頭 ${res.files.length} 件を表示）` : ""} / 対象フォルダ ${state.workspace.repo}`;
    $("preview").replaceChildren(...res.files.map((f) => el("tr", {},
      el("td", { className: "mono", textContent: f.path }), el("td", { textContent: LANGUAGE_LABELS[f.language] || f.language }))));
    if (res.total === 0) $("preview").append(el("tr", {}, el("td", { colSpan: 2, className: "muted", textContent: "対象のファイルはありません" })));
    $("preview-section").classList.remove("hidden");
  } catch (err) {
    message("targets-msg", err.message);
  }
});

$("targets-save").addEventListener("click", async () => {
  if (!confirm("対象の設定を保存しますか？（夜間バッチ・コマンドからの実行にも反映されます。ファイル内のコメントは消えます）")) return;
  try {
    state.targets = await send("PUT", "/api/targets", { config: collectTargets() });
    renderTargets(state.targets.config);
    $("targets-example-note").classList.toggle("hidden", !state.targets.is_example);
    message("targets-msg", `保存しました（${state.targets.write_path}）`, true);
  } catch (err) {
    message("targets-msg", err.message);
  }
});

$("targets-reset").addEventListener("click", loadTargets);

// --- スキル -------------------------------------------------------------------------------

async function fileToBase64(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

async function uploadSkill(kind, fileList, fromFolder) {
  const files = [];
  for (const file of fileList) {
    // フォルダを選ぶと「選んだフォルダ名/…」の形で届くので、先頭のフォルダ名を外す
    const path = fromFolder ? file.webkitRelativePath.split("/").slice(1).join("/") : "SKILL.md";
    files.push({ path, content_b64: await fileToBase64(file) });
  }
  if (fromFolder && !files.some((f) => f.path === "SKILL.md")) throw new Error("選んだフォルダの直下に SKILL.md がありません");
  return send("PUT", `/api/skills/${kind}`, { files });
}

function renderSkill(info) {
  const title = info.kind === "doc" ? "設計書スキル" : "コメント追記スキル";
  const msg = el("div", { className: "small" });
  const run = async (fn, okText) => {
    msg.className = "small";
    msg.textContent = "処理中…";
    try {
      renderSkill(await fn());
      await loadConfig();
      const next = $(`skill-${info.kind}`).querySelector(".skill-msg");
      next.className = "success small skill-msg";
      next.textContent = okText;
    } catch (err) {
      msg.className = "error small";
      msg.textContent = err.message;
    }
  };

  const folderInput = el("input", { type: "file", multiple: true, className: "hidden" });
  folderInput.webkitdirectory = true;
  folderInput.addEventListener("change", () => folderInput.files.length &&
    run(() => uploadSkill(info.kind, folderInput.files, true), "登録しました"));
  const fileInput = el("input", { type: "file", accept: ".md,text/markdown", className: "hidden" });
  fileInput.addEventListener("change", () => fileInput.files.length &&
    run(() => uploadSkill(info.kind, fileInput.files, false), "登録しました"));

  const button = (text, onclick, cls = "") => {
    const b = el("button", { type: "button", textContent: text, onclick, className: cls });
    b.dataset.admin = "";
    return b;
  };
  const buttons = [
    button("フォルダを選んで登録", () => folderInput.click(), "primary"),
    button("SKILL.md だけ登録", () => fileInput.click()),
  ];
  if (info.registered) buttons.push(button("内容を見る", () => viewSkill(info.kind, title)));
  if (info.has_backup) {
    buttons.push(button("直前に戻す", () => confirm("直前に登録されていたスキルに戻しますか？")
      && run(() => send("POST", `/api/skills/${info.kind}/restore`), "直前のスキルに戻しました")));
  }
  if (info.kind === "comment" && info.registered) {
    buttons.push(button("登録を外す", () => confirm("コメント追記スキルの登録を外しますか？（コメント追記は実行されなくなります）")
      && run(() => send("DELETE", "/api/skills/comment"), "登録を外しました"), "danger"));
  }
  msg.classList.add("skill-msg");

  $(`skill-${info.kind}`).replaceChildren(...[
    el("h2", { textContent: title }),
    el("div", { className: "row" },
      info.registered ? badge("登録済み", "ok") : badge(info.kind === "doc" ? "未登録（実行できません）" : "未登録（コメント追記は行いません）", info.kind === "doc" ? "fail" : "idle"),
      info.updated_at ? el("span", { className: "muted small", textContent: `更新 ${fmtTime(info.updated_at)}` }) : null),
    el("div", { className: "muted small mono", textContent: info.dir, style: "margin-bottom:8px" }),
    info.files.length ? el("div", { className: "scroll", style: "max-height:160px;margin-bottom:10px" },
      el("table", {}, el("tbody", {}, ...info.files.map((f) => el("tr", {},
        el("td", { className: "mono", textContent: f.path }),
        el("td", { className: "muted small", textContent: fmtSize(f.size) })))))) : null,
    el("div", { className: "row" }, ...buttons, folderInput, fileInput),
    msg,
  ].filter(Boolean));
  applyPermissions();
}

async function loadSkills() {
  try {
    const list = await api("/api/skills");
    for (const info of list) renderSkill(info);
  } catch (err) {
    $("skill-doc").textContent = err.message;
  }
}

async function viewSkill(kind, title) {
  $("skill-viewer-title").textContent = `${title}（SKILL.md）`;
  $("skill-viewer").textContent = "読み込み中…";
  $("skill-viewer-section").classList.remove("hidden");
  try {
    $("skill-viewer").textContent = await api(`/api/skills/${kind}/content`);
  } catch (err) {
    $("skill-viewer").textContent = err.message;
  }
  $("skill-viewer-section").scrollIntoView({ behavior: "smooth" });
}

// --- 起動・定期更新 --------------------------------------------------------------------------

let wasRunning = false;
let timer = null;

async function tick() {
  try {
    const job = await refreshStatus();
    await pollLog();
    const running = job?.running ?? false;
    // 実行が終わったら、履歴と生成物を読み直す
    if (wasRunning && !running) await Promise.all([refreshHistory(), refreshOutputs()]);
    wasRunning = running;
  } catch (err) {
    if (err.status === 401) await start();
  }
}

async function start() {
  clearInterval(timer);
  if (!(await refreshMe())) return;
  await loadConfig();
  await Promise.all([refreshHistory(), refreshOutputs()]);
  await tick();
  timer = setInterval(tick, POLL_MS);
}

start().catch((err) => { $("auth-error").textContent = err.message; });
