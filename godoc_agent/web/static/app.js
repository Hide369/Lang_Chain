"use strict";

// 状態を変える操作には、サーバー側の CSRF 対策と同じヘッダーを付ける
const CSRF = { "X-Requested-With": "godoc-web" };
const POLL_MS = 2000;
const LANGUAGE_LABELS = { go: "Go", csharp: "C#", typescript: "TypeScript" };
const $ = (id) => document.getElementById(id);

const state = {
  admin: false,
  tab: "run",
  workspace: { repo: null, out: null },
  running: false,    // このブラウザで選んでいる出力先の実行が動いているか
  anyRunning: false, // 他の人の実行を含め、Web から起動した実行が動いているか
  jobRunId: null,    // このブラウザで選んでいる出力先の、最後の実行の run_id（停止の対象）
  logRunId: null,   // ログ欄に表示中の run_id
  logOffset: 0,
  sessionSlot: null, // セッションログを表示中の枠の番号（null は非表示）
  following: true,  // 実行中のジョブのログを追いかけるか（履歴を選ぶと止める）
  outputs: [],
  targets: null,    // サーバーから読んだ対象の設定
  taskOptions: [],  // 実行に使える（登録済みの）スキル [{name, label, type}]
};

function taskLabel(name) {
  return state.taskOptions.find((t) => t.name === name)?.label ?? name;
}

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
    // スキル・対象の設定は全員で共有するため、誰かの実行中は変えられない（フォルダはブラウザごとなので、いつでも選べる）
    const lockedWhileAnyRunning = node.closest("#tab-targets, #tab-skills");
    node.disabled = !state.admin || Boolean(lockedWhileAnyRunning && state.anyRunning);
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

// --- クライアント PC からのアップロード ----------------------------------------------------------

// 送らないフォルダ（大きく、設計書の対象にもならない）
const UPLOAD_SKIP_DIRS = new Set([".git", "node_modules"]);
// 1回のリクエストで送る量の目安（大きなフォルダでもサーバーのメモリを圧迫しないよう分けて送る）
const UPLOAD_BATCH_BYTES = 4 * 1024 * 1024;
const UPLOAD_BATCH_FILES = 200;

async function uploadFolder(fileList) {
  // フォルダを選ぶと「選んだフォルダ名/…」の形で届く。先頭のフォルダ名がサーバー上の置き場所の名前になる
  const name = fileList[0].webkitRelativePath.split("/")[0];
  const files = fileList.filter((f) => !f.webkitRelativePath.split("/").slice(1, -1).some((p) => UPLOAD_SKIP_DIRS.has(p)));
  if (files.length === 0) throw new Error("送るファイルがありません");
  const totalMb = (files.reduce((sum, f) => sum + f.size, 0) / 1024 / 1024).toFixed(1);
  if (!confirm(`${name}（${files.length} ファイル・${totalMb}MB）をサーバーにアップロードし、対象フォルダにします。よろしいですか？`)) return;

  const { upload_id: uploadId } = await send("POST", "/api/uploads", { name });
  const base = `/api/uploads/${encodeURIComponent(uploadId)}`;
  try {
    let batch = [];
    let batchBytes = 0;
    let sent = 0;
    const flush = async () => {
      if (batch.length === 0) return;
      await send("POST", `${base}/files`, { files: batch });
      sent += batch.length;
      batch = [];
      batchBytes = 0;
      message("upload-msg", `アップロード中… ${sent} / ${files.length} ファイル`, true);
    };
    for (const file of files) {
      batch.push({ path: file.webkitRelativePath.split("/").slice(1).join("/"), content_b64: await fileToBase64(file) });
      batchBytes += file.size;
      if (batchBytes >= UPLOAD_BATCH_BYTES || batch.length >= UPLOAD_BATCH_FILES) await flush();
    }
    await flush();
    state.workspace = await send("POST", `${base}/complete`);
  } catch (err) {
    // 途中で失敗したら、サーバーに残った途中のファイルを消す（消せなくても次のアップロードで片付く）
    send("DELETE", base).catch(() => {});
    throw err;
  }
  renderWorkspace();
  message("upload-msg", `${name} をアップロードしました（${files.length} ファイル）。このまま「実行」できます`, true);
  await Promise.all([refreshHistory(), refreshOutputs()]);
}

$("upload-folder").addEventListener("click", () => $("upload-input").click());
$("upload-input").addEventListener("change", () => {
  // 同じフォルダを続けて選び直せるよう入力欄はすぐ空に戻す（中身は先に配列へ写す）
  const files = [...$("upload-input").files];
  $("upload-input").value = "";
  if (files.length) uploadFolder(files).catch((err) => message("upload-msg", err.message));
});

function renderWorkspace() {
  $("repo-path").textContent = state.workspace.repo || "未設定";
  $("out-path").textContent = state.workspace.out || "未設定";
}

// --- スキルの複数選択プルダウン --------------------------------------------------------------

const TYPE_SHORT_LABELS = { document: "文書", comment: "コメント", rewrite: "コード変更" };

// options: [{name, label, type, missing?}]。選んだスキル名は pickerValues() で取り出す
function renderPicker(container, options, selected) {
  const summary = el("summary", { className: "picker-summary" });
  const menu = el("div", { className: "picker-menu" });
  const update = () => {
    const names = pickerValues(container);
    summary.textContent = names.length ? names.map(taskLabel).join("、") : "スキルを選んでください";
    summary.classList.toggle("muted", names.length === 0);
  };
  for (const opt of options) {
    const input = el("input", { type: "checkbox", value: opt.name, checked: selected.includes(opt.name) });
    input.dataset.admin = "";
    input.addEventListener("change", update);
    const type = opt.missing ? "未登録" : TYPE_SHORT_LABELS[opt.type] || opt.type;
    menu.append(el("label", {}, input, el("span", { textContent: opt.label }),
      el("span", { className: `muted small${opt.missing ? " error" : ""}`, textContent: type })));
  }
  if (options.length === 0) menu.append(el("div", { className: "muted small", textContent: "登録済みのスキルがありません（「スキル」タブから登録）" }));
  container.replaceChildren(el("details", { className: "picker" }, summary, menu));
  update();
}

function pickerValues(container) {
  return [...container.querySelectorAll("input:checked")].map((i) => i.value);
}

// プルダウンの外をクリックしたら閉じる
document.addEventListener("click", (e) => {
  for (const open of document.querySelectorAll("details.picker[open]")) if (!open.contains(e.target)) open.open = false;
});

// --- 実行 ---------------------------------------------------------------------------------

async function loadConfig() {
  const config = await api("/api/config");
  state.workspace = config.workspace;
  state.taskOptions = config.tasks;
  renderWorkspace();
  const names = config.tasks.map((t) => t.name);
  const before = $("tasks").children.length ? pickerValues($("tasks")) : null;
  // 初回は組み込みのスキル（設計書・コメント追記）を選んでおく。2回目以降は選択を引き継ぐ
  const selected = (before ?? ["doc", "comment"]).filter((n) => names.includes(n));
  renderPicker($("tasks"), config.tasks, selected);
  renderConcurrency(config.concurrency);
}

function renderConcurrency({ default: fallback, max }) {
  const input = $("concurrency");
  input.max = String(max);
  // 入力中の値は残し、未入力のときだけ既定値を入れる
  if (input.value === "" && fallback != null) input.value = String(fallback);
}

$("run-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("run-error").textContent = "";
  const tasks = pickerValues($("tasks"));
  if (tasks.length === 0) {
    $("run-error").textContent = "スキルを1つ以上選んでください";
    return;
  }
  if ($("force").checked && !confirm("変更のないファイルも作り直します（設計書の版が1つ上がります）。よろしいですか？")) return;
  try {
    const job = await send("POST", "/api/runs", {
      tasks, limit: Number($("limit").value) || 0, force: $("force").checked, git_pull: $("git-pull").checked,
      concurrency: Number($("concurrency").value) || null,  // 未入力なら既定値（サーバー全体の上限と同じ）
    });
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
    await send("POST", "/api/runs/stop", { run_id: state.jobRunId });
    await refreshStatus();
  } catch (err) {
    $("run-error").textContent = err.message;
  }
});

// --- 定期実行 -----------------------------------------------------------------------------

async function loadSchedule() {
  const schedule = await api("/api/schedule");
  $("schedule-enabled").checked = schedule.enabled;
  $("schedule-time").value = schedule.time;
  $("schedule-git-pull").checked = schedule.git_pull;
  // 保存済みの選択に、その後登録を外したスキルがあれば「未登録」として見せる（実行時には除かれる）
  const missing = schedule.tasks.filter((n) => !state.taskOptions.some((t) => t.name === n))
    .map((name) => ({ name, label: name, type: "", missing: true }));
  renderPicker($("schedule-tasks"), [...state.taskOptions, ...missing], schedule.tasks);
  renderScheduleInfo(schedule);
  applyPermissions();
}

function renderScheduleInfo(schedule) {
  $("schedule-badge").replaceChildren(schedule.enabled ? badge(`毎日 ${schedule.time}`, "ok") : badge("無効", "idle"));
  const parts = [];
  if (schedule.next_run_at) parts.push(`次回 ${fmtTime(schedule.next_run_at)}`);
  const last = schedule.last_run;
  if (last?.run_id) parts.push(`前回 ${last.run_id}`);
  else if (last?.error) parts.push(`前回 ${fmtTime(last.at)} 開始できず: ${last.error}`);
  $("schedule-info").textContent = parts.join(" / ");
}

$("schedule-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("schedule-msg").textContent = "";
  const tasks = pickerValues($("schedule-tasks")).filter((n) => state.taskOptions.some((t) => t.name === n));
  if (tasks.length === 0) {
    message("schedule-msg", "登録済みのスキルを1つ以上選んでください");
    return;
  }
  try {
    const schedule = await send("PUT", "/api/schedule", {
      enabled: $("schedule-enabled").checked, time: $("schedule-time").value, tasks, git_pull: $("schedule-git-pull").checked,
    });
    renderScheduleInfo(schedule);
    message("schedule-msg", schedule.enabled ? `保存しました（次回 ${fmtTime(schedule.next_run_at)}）` : "保存しました（定期実行は無効）", true);
  } catch (err) {
    message("schedule-msg", err.message);
  }
});

// --- 状態・ログ -----------------------------------------------------------------------------

function badge(text, cls) {
  return el("span", { className: `badge ${cls}`, textContent: text });
}

async function refreshStatus() {
  const { job, busy_elsewhere: busyElsewhere, others = [], server } = await api("/api/status");
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
    ? `${job.trigger === "scheduled" ? "定期実行" : "手動"} / run_id=${job.run_id} / ${job.tasks.map(taskLabel).join("・") || "既定のタスク"}`
      + `${job.limit ? ` / 上限 ${job.limit} 件` : ""}${job.force ? " / 再生成" : ""}${job.git_pull ? " / git pull" : ""}`
      + ` / 同時 ${job.concurrency ?? "既定数の"} セッション`
      + ` / 開始 ${fmtTime(job.started_at)}${job.finished_at ? ` / 終了 ${fmtTime(job.finished_at)}` : ""}`
      + `\n対象 ${job.repo}\n出力 ${job.out}`
    : "";
  $("status-detail").style.whiteSpace = "pre-wrap";
  renderServerLoad(others, server);

  state.running = running;
  state.anyRunning = running || others.length > 0;
  state.jobRunId = job?.run_id ?? null;
  applyPermissions();
  $("start").disabled = !state.admin || running || busyElsewhere;
  $("stop").disabled = !state.admin || !running || job.stop_requested;

  if (job && state.following && state.logRunId !== job.run_id) followRun(job.run_id);
  return job;
}

// 他の人の実行（別の出力先）と、サーバー全体の Claude Code のセッションの使用状況
function renderServerLoad(others, server) {
  const lines = [];
  if (server) lines.push(`サーバー全体の Claude Code セッション: 使用中 ${server.active_sessions} / ${server.max_sessions}`);
  for (const other of others) {
    lines.push(`他の実行: ${other.trigger === "scheduled" ? "定期実行" : "手動"} / run_id=${other.run_id}`
      + ` / 開始 ${fmtTime(other.started_at)} / 出力 ${other.out}`);
  }
  $("server-load").textContent = lines.join("\n");
}

function followRun(runId) {
  state.following = true;
  showLog(runId);
}

function showLog(runId) {
  if (state.logRunId !== runId) state.sessionSlot = null;
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

// --- セッション（同時に動く Claude Code の枠）------------------------------------------------------

function fmtDuration(seconds) {
  if (seconds == null) return "";
  const m = Math.floor(seconds / 60);
  return m ? `${m}分${String(seconds % 60).padStart(2, "0")}秒` : `${seconds}秒`;
}

function fmtClock(iso) {
  return iso ? iso.slice(11, 19) : "";
}

async function refreshSessions() {
  const runId = state.logRunId;
  if (!runId) {
    renderSessionsEmpty("実行を開始するか、実行履歴から選ぶと表示します");
    return;
  }
  let data;
  try {
    data = await api(`/api/runs/${encodeURIComponent(runId)}/sessions`);
  } catch (err) {
    if (state.logRunId !== runId) return;
    // runner が枠を用意する前（対象の列挙中）や、この機能より前の実行は記録がない
    renderSessionsEmpty(err.status === 404 ? "この実行にはセッションの記録がありません（開始直後・対象が0件・以前の版での実行）" : err.message);
    return;
  }
  if (state.logRunId !== runId) return;
  renderSessions(data);
}

function renderSessionsEmpty(text) {
  $("sessions-title").textContent = state.logRunId ?? "";
  $("sessions-summary").textContent = "";
  $("sessions-meter").replaceChildren();
  $("sessions-grid").replaceChildren();
  $("sessions-empty").textContent = text;
  $("session-log-wrap").classList.add("hidden");
}

function renderSessions(data) {
  const stale = data.interrupted;
  const waiting = Math.max(data.total - data.finished - data.active, 0);
  $("sessions-title").textContent = data.run_id;
  $("sessions-empty").textContent = stale ? "runner が途中で止まったため、処理中だった枠は中断されています" : "";
  $("sessions-summary").replaceChildren(
    badge(`使用中 ${data.active} / ${data.concurrency}`, data.running ? "running" : stale ? "warn" : "idle"),
    ` 完了 ${data.finished} / ${data.total} 件${data.running ? ` ・ 待ち ${waiting} 件` : ""}`,
  );

  // 占有メーター：1マス = 1セッション枠
  const meter = $("sessions-meter");
  meter.style.setProperty("--slots", data.concurrency);
  meter.setAttribute("aria-label", `使用中 ${data.active} / ${data.concurrency} セッション`);
  meter.replaceChildren(...data.sessions.map((s) => {
    const cell = el("span", { title: `S${String(s.slot).padStart(2, "0")}${s.busy ? ` ${s.rel_path}` : " 空き"}` });
    if (s.busy) cell.classList.add(stale ? "stale" : "on");
    return cell;
  }));

  $("sessions-grid").replaceChildren(...data.sessions.map((s) => sessionCard(s, stale)));
  renderSessionLog(data.sessions.find((s) => s.slot === state.sessionSlot));
}

function sessionCard(s, stale) {
  const name = `S${String(s.slot).padStart(2, "0")}`;
  const status = s.busy ? (stale ? badge("中断", "warn") : badge("実行中", "running")) : badge("空き", "idle");
  const last = s.events.at(-1);
  const card = el("button", { type: "button", className: `slot${s.busy ? " busy" : ""}${s.busy && stale ? " stale" : ""}` },
    el("span", { className: "slot-head" }, el("span", { className: "mono", textContent: name }), status),
    s.busy
      ? el("span", { className: "mono small ellipsis", textContent: s.rel_path, title: s.rel_path })
      : el("span", { className: "muted small", textContent: `処理済み ${s.handled} 件` }),
    s.busy
      ? el("span", { className: "small", textContent:
          `${taskLabel(s.task)} ・ 生成 ${s.attempt || "-"} 回目${s.elapsed_s != null ? ` ・ ${fmtDuration(s.elapsed_s)}` : ""}` })
      : null,
    last ? el("span", { className: "muted small ellipsis", textContent: `${fmtClock(last.at)} ${last.text}`, title: last.text }) : null);
  card.setAttribute("aria-pressed", String(state.sessionSlot === s.slot));
  card.addEventListener("click", () => {
    state.sessionSlot = state.sessionSlot === s.slot ? null : s.slot;
    refreshSessions();
  });
  return card;
}

function renderSessionLog(session) {
  $("session-log-wrap").classList.toggle("hidden", !session);
  if (!session) return;
  $("session-log-title").textContent = `S${String(session.slot).padStart(2, "0")}（直近 ${session.events.length} 件）`;
  const box = $("session-log");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const text = session.events.map((e) => `${fmtClock(e.at)}  ${e.text}`).join("\n") || "（まだ記録はありません）";
  if (box.textContent !== text) {
    box.textContent = text;
    if (atBottom) box.scrollTop = box.scrollHeight;
  }
}

// --- 履歴・生成物 ---------------------------------------------------------------------------

async function refreshHistory() {
  const runs = await api("/api/runs");
  $("history").replaceChildren(...runs.map((run) => {
    const counts = Object.entries(run.counts).map(([k, v]) => `${k}=${v}`).join(" ");
    const name = run.has_log
      ? el("a", { textContent: run.run_id, onclick: () => { state.following = false; showLog(run.run_id); pollLog(); refreshSessions(); } })
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

async function uploadSkill(name, fileList, fromFolder, type = null, label = null) {
  const files = [];
  for (const file of fileList) {
    // フォルダを選ぶと「選んだフォルダ名/…」の形で届くので、先頭のフォルダ名を外す
    const path = fromFolder ? file.webkitRelativePath.split("/").slice(1).join("/") : "SKILL.md";
    files.push({ path, content_b64: await fileToBase64(file) });
  }
  if (fromFolder && !files.some((f) => f.path === "SKILL.md")) throw new Error("選んだフォルダの直下に SKILL.md がありません");
  return send("PUT", `/api/skills/${encodeURIComponent(name)}`, { files, type, label });
}

function adminButton(text, onclick, cls = "") {
  const b = el("button", { type: "button", textContent: text, onclick, className: cls });
  b.dataset.admin = "";
  return b;
}

// フォルダ選択・ファイル選択の隠し入力。選ぶと onFiles(files, fromFolder) を呼ぶ
function filePickers(onFiles) {
  // 同じフォルダを続けて選び直せるよう入力欄はすぐ空に戻す。input.files は空に戻すと中身も消えるため、先に配列へ写す
  const handle = (input, fromFolder) => () => {
    const files = [...input.files];
    input.value = "";
    if (files.length) onFiles(files, fromFolder);
  };
  const folderInput = el("input", { type: "file", multiple: true, className: "hidden" });
  folderInput.webkitdirectory = true;
  folderInput.addEventListener("change", handle(folderInput, true));
  const fileInput = el("input", { type: "file", accept: ".md,text/markdown", className: "hidden" });
  fileInput.addEventListener("change", handle(fileInput, false));
  return { folderInput, fileInput };
}

// スキルの一覧を読み直し、実行・定期実行のプルダウンも更新する。完了メッセージは該当スキルの欄に出す
async function afterSkillChange(name, okText) {
  await loadSkills();
  await loadConfig();
  await loadSchedule();
  const msg = document.querySelector(`[data-skill="${CSS.escape(name)}"] .skill-msg`) || $("new-skill-msg");
  message(msg.id || (msg.id = `skill-msg-${name}`), okText, true);
}

function renderSkill(info, types) {
  const section = el("section", {});
  section.dataset.skill = info.name;
  const msg = el("div", { className: "small skill-msg" });
  const act = async (fn, okText) => {
    msg.className = "small skill-msg";
    msg.textContent = "処理中…";
    try {
      await fn();
      await afterSkillChange(info.name, okText);
    } catch (err) {
      msg.className = "error small skill-msg";
      msg.textContent = err.message;
    }
  };
  const { folderInput, fileInput } = filePickers((files, fromFolder) =>
    act(() => uploadSkill(info.name, files, fromFolder), "登録しました"));

  const buttons = [
    adminButton("フォルダを選んで登録", () => folderInput.click(), "primary"),
    adminButton("SKILL.md だけ登録", () => fileInput.click()),
  ];
  if (info.registered) buttons.push(adminButton("内容を見る", () => viewSkill(info.name, info.label)));
  if (info.has_backup) {
    buttons.push(adminButton("直前に戻す", () => confirm("直前に登録されていたスキルに戻しますか？")
      && act(() => send("POST", `/api/skills/${encodeURIComponent(info.name)}/restore`), "直前のスキルに戻しました")));
  }
  if (info.name !== "doc" && info.registered) {
    const note = info.builtin ? "（コメント追記は実行されなくなります）" : "（「スキル」の一覧からも消えます。フォルダは .prev に残ります）";
    buttons.push(adminButton("登録を外す", () => confirm(`「${info.label}」の登録を外しますか？${note}`)
      && act(() => send("DELETE", `/api/skills/${encodeURIComponent(info.name)}`), "登録を外しました"), "danger"));
  }

  // 追加のスキルは表示名（プルダウンなどに出す名前）を変えられる
  let labelRow = null;
  if (!info.builtin) {
    const labelInput = el("input", { type: "text", value: info.display_name || "", placeholder: "例: テスト仕様書",
      maxLength: 40, style: "width:200px" });
    labelInput.dataset.admin = "";
    labelRow = el("div", { className: "row" },
      el("span", { className: "label", textContent: "表示名" }), labelInput,
      adminButton("表示名を変更", () => act(() => send("PUT", `/api/skills/${encodeURIComponent(info.name)}/label`,
        { label: labelInput.value }), "表示名を変更しました")));
  }

  const status = info.registered
    ? badge("登録済み", "ok")
    : badge(info.name === "doc" ? "未登録（設計書は実行できません）" : "未登録", info.name === "doc" ? "fail" : "idle");
  const outDir = info.out_subdir ? `<出力先>/${info.out_subdir}/` : "<出力先>/ 直下";
  section.append(...[
    el("h2", {}, info.label, el("span", { className: "muted small mono", textContent: `  ${info.name}` })),
    el("div", { className: "row" }, status, el("span", { className: "badge idle", textContent: info.type_label }),
      info.updated_at ? el("span", { className: "muted small", textContent: `更新 ${fmtTime(info.updated_at)}` }) : null),
    el("div", { className: "muted small mono", textContent: `スキル: ${info.dir}` }),
    el("div", { className: "muted small mono", textContent: `出力: ${outDir}`, style: "margin-bottom:8px" }),
    labelRow,
    info.files.length ? el("div", { className: "scroll", style: "max-height:160px;margin-bottom:10px" },
      el("table", {}, el("tbody", {}, ...info.files.map((f) => el("tr", {},
        el("td", { className: "mono", textContent: f.path }),
        el("td", { className: "muted small", textContent: fmtSize(f.size) })))))) : null,
    el("div", { className: "row" }, ...buttons, folderInput, fileInput),
    msg,
  ].filter(Boolean));
  return section;
}

function renderNewSkill(types) {
  const name = el("input", { type: "text", placeholder: "例: test-spec", maxLength: 40, style: "width:200px" });
  name.dataset.admin = "";
  const label = el("input", { type: "text", placeholder: "例: テスト仕様書（省略可）", maxLength: 40, style: "width:200px" });
  label.dataset.admin = "";
  const type = el("select", {}, el("option", { value: "", textContent: "出力の種類を選択" }),
    ...types.map((t) => el("option", { value: t.type, textContent: t.label })));
  type.dataset.admin = "";
  const msg = el("div", { id: "new-skill-msg", className: "small" });
  const check = () => {
    if (!/^[a-z0-9][a-z0-9_-]{0,39}$/.test(name.value)) throw new Error("スキル名は英小文字・数字・-・_ で、英小文字か数字で始めてください");
    if (!type.value) throw new Error("出力の種類を選んでください");
  };
  const add = async (files, fromFolder) => {
    try {
      msg.className = "small";
      msg.textContent = "処理中…";
      await uploadSkill(name.value, files, fromFolder, type.value, label.value);
      const added = name.value;
      name.value = "";
      label.value = "";
      type.value = "";
      await afterSkillChange(added, "追加しました");
    } catch (err) {
      message("new-skill-msg", err.message);
    }
  };
  const { folderInput, fileInput } = filePickers(add);
  const pick = (input) => () => {
    try { check(); input.click(); } catch (err) { message("new-skill-msg", err.message); }
  };
  return el("section", { className: "wide" },
    el("h2", { textContent: "スキルを追加" }),
    el("p", { className: "muted small", style: "margin-top:0",
      textContent: "スキル名はサーバー上のフォルダ名（skills/<スキル名>/）と出力先のフォルダ名（<出力先>/_<スキル名>/）になります。"
        + "表示名（日本語可・省略可）は実行・定期実行のスキル選択に表示する名前です。省略するとスキル名を表示します。"
        + "文書: ファイルごとに Markdown を作り、前回の出力を引き継いで改訂させます。"
        + "コメントだけ変更: コメント以外のコードが変わっていたら不合格にします。"
        + "コードも変更: 本文が空でないことだけを確かめます（取り込むかどうかは差分を見て判断してください）。" }),
    el("div", { className: "row" },
      el("span", { className: "label", textContent: "スキル名" }), name,
      el("span", { className: "label", textContent: "出力の種類", style: "width:auto" }), type),
    el("div", { className: "row" },
      el("span", { className: "label", textContent: "表示名" }), label),
    el("div", { className: "row" },
      el("span", { className: "label" }),
      adminButton("フォルダを選んで追加", pick(folderInput), "primary"),
      adminButton("SKILL.md だけで追加", pick(fileInput)), folderInput, fileInput),
    msg);
}

async function loadSkills() {
  try {
    const { skills, types } = await api("/api/skills");
    $("skill-list").replaceChildren(...skills.map((info) => renderSkill(info, types)), renderNewSkill(types));
    applyPermissions();
  } catch (err) {
    $("skill-list").replaceChildren(el("section", { className: "wide error", textContent: err.message }));
  }
}

async function viewSkill(name, title) {
  $("skill-viewer-title").textContent = `${title}（SKILL.md）`;
  $("skill-viewer").textContent = "読み込み中…";
  $("skill-viewer-section").classList.remove("hidden");
  try {
    $("skill-viewer").textContent = await api(`/api/skills/${encodeURIComponent(name)}/content`);
  } catch (err) {
    $("skill-viewer").textContent = err.message;
  }
  $("skill-viewer-section").scrollIntoView({ behavior: "smooth" });
}

// --- 起動・定期更新 --------------------------------------------------------------------------

let lastJobKey = null;
let timer = null;

async function tick() {
  try {
    const job = await refreshStatus();
    await Promise.all([pollLog(), refreshSessions()]);
    // ジョブが変わった（定期実行が始まった）・終わったら、履歴・生成物・定期実行の次回予定を読み直す。
    // 短い実行は確認の間に始まって終わることがあるため、「実行中→終了」ではなく run_id と状態の変化で判断する。
    // 定期実行の入力欄は、入力中の内容を消さないよう書き換えない
    const jobKey = job ? `${job.run_id}:${job.running}` : "";
    if (jobKey !== lastJobKey) {
      await Promise.all([refreshHistory(), refreshOutputs()]);
      renderScheduleInfo(await api("/api/schedule"));
      lastJobKey = jobKey;
    }
  } catch (err) {
    if (err.status === 401) await start();
  }
}

async function start() {
  clearInterval(timer);
  if (!(await refreshMe())) return;
  await loadConfig();
  await Promise.all([refreshHistory(), refreshOutputs(), loadSchedule()]);
  await tick();
  timer = setInterval(tick, POLL_MS);
}

start().catch((err) => { $("auth-error").textContent = err.message; });
