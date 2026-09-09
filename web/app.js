/* liveryAutoPackge 前端逻辑（从零新建涂装模式） */
"use strict";

const $ = (id) => document.getElementById(id);
const api = {
  get(path) {
    return fetch(path).then((r) => r.json());
  },
  post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then((r) => r.json());
  },
};

const state = {
  info: null,
  profiles: [],
  payload: null,      // /api/scan（无源文件夹）返回的档案骨架
  values: {},         // 当前字段值（key -> value）
  outputDir: "",
  profileId: "",      // 当前选中的机模档案 id
  iconSetup: null,    // 档案图标自动生成信息
  iconSrc: "",        // 用户手动选定的图标图片（绝对路径）
  texItems: [],       // 档案固定的部位
  texCustom: [],      // 用户追加的自定义部位
  busy: false,
};

/* ---------------------------------------------------------------- 工具 */
function toast(msg, isError) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("error", !!isError);
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), isError ? 5200 : 2600);
}

function hideToast() {
  const t = $("toast");
  clearTimeout(t._timer);
  t.classList.add("hidden");
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function scrollTo(el) {
  el.scrollIntoView({ behavior: "smooth", block: "start" });
}

/* 与后端一致的本地命名预览 */
function fillTemplate(tpl, values) {
  return String(tpl || "").replace(/\{(\w+)\}/g, (m, k) => (values[k] == null ? "" : values[k]));
}
function sanitizeName(name) {
  let s = String(name || "").replace(/[\\/:*?"<>|\u0000-\u001f]/g, "");
  s = s.replace(/\s+/g, " ").trim().replace(/[. ]+$/, "");
  return s;
}
function previewName() {
  const p = state.payload ? state.payload.profile : null;
  if (!p || !p.naming) return "Livery_…";
  const n = p.naming || {};
  const prefix = sanitizeName(fillTemplate(n.prefixTemplate, state.values)).trim();
  const coreTpl = n.nameTemplate || n.coreTemplate || "";
  const core = sanitizeName(fillTemplate(coreTpl, state.values)).trim();
  const parts = [prefix, core].filter(Boolean).join(" ");
  const final = parts || "Livery_…";
  $("namePreview").textContent = final;
  return final;
}

/* ---------------------------------------------------------------- 初始化 */
async function init() {
  try {
    state.info = await api.get("/api/info");
    $("metaVersion").textContent = "v" + state.info.version;
    $("metaOutput").textContent = state.info.outputDir;
    $("outputDir").value = state.info.outputDir;
    state.outputDir = state.info.outputDir;
    state.profiles = state.info.profiles || [];
    fillProfileSelect();
    const first = pickDefaultProfile();
    if (first) {
      $("profileSelect").value = first;
      await loadProfile(first);
    } else {
      toast("没有可用的机模档案", true);
    }
  } catch (e) {
    toast("无法连接本地服务：" + e, true);
  }
}

function fillProfileSelect() {
  const sel = $("profileSelect");
  sel.innerHTML = "";
  for (const p of state.profiles) {
    const o = document.createElement("option");
    o.value = p.id;
    o.textContent = p.name + (p.fallback ? "（通用兜底）" : "");
    sel.appendChild(o);
  }
}

function pickDefaultProfile() {
  const list = state.profiles;
  if (!list.length) return "";
  const first = list.find((p) => !p.fallback);
  return (first || list[0]).id;
}

function setBusyUi(on) {
  $("btnPackage").disabled = on;
}

/* ---------------------------------------------------------------- 加载档案骨架（从零新建，无源文件夹） */
async function loadProfile(pid) {
  if (state.busy) return;
  const chosen = pid || pickDefaultProfile();
  if (!chosen) { toast("请先选择机模档案", true); return; }
  state.busy = true;
  setBusyUi(true);
  $("fieldsHost").innerHTML = '<div class="empty">正在加载机模档案…</div>';
  $("texHost").innerHTML = '<div class="empty">正在加载部位清单…</div>';
  $("card-textures").classList.add("hidden");
  try {
    const payload = await api.post("/api/scan", { profileId: chosen });
    if (payload.error) { toast(payload.error, true); return; }
    state.payload = payload;
    state.profileId = payload.profileId || chosen;
    $("profileSelect").value = state.profileId;
    state.iconSrc = "";
    renderProfileInfo(payload);
    renderFields(payload);
    renderTextures(payload);
    rebuildValues();
    previewName();
    hideToast();
    scrollTo($("card-options"));
  } catch (e) {
    toast("加载机模档案出错：" + e, true);
  } finally {
    state.busy = false;
    setBusyUi(false);
  }
}

function renderProfileInfo(payload) {
  const prof = (payload && payload.profile) || {};
  const vendorLine = [prof.vendor, prof.aircraft].filter(Boolean).join(" · ");
  const lines = [vendorLine, prof.description].filter(Boolean);
  if (prof.installHint) lines.push("安装位置：" + prof.installHint);
  $("profileInfo").textContent = lines.join("\n");
  $("profileInfo").style.whiteSpace = "pre-line";

  const tag = $("profileTag");
  tag.textContent = prof.hasConfig ? "支持交互配置" : "纯贴图组装";
  tag.style.background = prof.hasConfig ? "#e3f5ea" : "#eef5fb";
  tag.style.color = prof.hasConfig ? "#1e7a48" : "#49637c";

  const cfgTag = $("cfgTag");
  cfgTag.textContent = prof.hasConfig ? "部分选项将写入机模配置文件" : "无配置文件（按贴图组装）";
  cfgTag.style.background = prof.hasConfig ? "#e3f5ea" : "#eef5fb";
  cfgTag.style.color = prof.hasConfig ? "#1e7a48" : "#49637c";
}

/* ---------------------------------------------------------------- 档案切换 */
$("profileSelect").addEventListener("change", (e) => {
  const chosen = e.target.value;
  if (chosen === state.profileId) return;
  toast("正在加载机模档案…");
  loadProfile(chosen);
});

/* ---------------------------------------------------------------- 输出文件夹 */
$("outputDir").addEventListener("input", (e) => {
  state.outputDir = e.target.value.trim();
});

$("btnBrowseOutput").addEventListener("click", async () => {
  const res = await api.post("/api/browse-dialog", { title: "选择打包输出文件夹" });
  if (res.error || res.cancelled) {
    if (res.error) toast("本机无法弹出文件夹选择框：" + res.error, true);
    return;
  }
  if (res.path) {
    $("outputDir").value = res.path;
    state.outputDir = res.path;
  }
});

/* ---------------------------------------------------------------- 渲染字段表单 */
function renderFields(payload) {
  const host = $("fieldsHost");
  host.innerHTML = "";
  const fields = payload.fields || [];
  if (!fields.length) {
    host.innerHTML = '<div class="empty">该机模档案没有可填写的字段。</div>';
    return;
  }
  // 按 group 分组（保留出现顺序）
  const groups = [];
  const index = {};
  for (const f of fields) {
    const g = f.group || "涂装信息";
    if (!(g in index)) { index[g] = groups.length; groups.push({ name: g, items: [] }); }
    groups[index[g]].items.push(f);
  }

  for (const grp of groups) {
    const box = document.createElement("div");
    box.className = "field-group";
    const title = document.createElement("div");
    title.className = "group-title";
    title.textContent = grp.name;
    const configCount = grp.items.filter((f) => f.target === "config").length;
    if (configCount && grp.items.length === configCount) {
      const b = document.createElement("span");
      b.className = "g-badge";
      b.textContent = "写入机模配置";
      title.appendChild(b);
    } else if (configCount) {
      const b = document.createElement("span");
      b.className = "g-badge";
      b.textContent = `${configCount} 项写入配置`;
      title.appendChild(b);
    }
    box.appendChild(title);

    const grid = document.createElement("div");
    grid.className = "form-grid";
    for (const f of grp.items) {
      grid.appendChild(buildField(f));
    }
    box.appendChild(grid);
    host.appendChild(box);
  }
}

function buildField(f) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const name = document.createElement("span");
  name.className = "field-name";
  name.textContent = f.label || f.key;
  wrap.appendChild(name);

  let ctl;
  if (f.type === "bool") {
    ctl = buildBoolSwitch(f);
  } else if (f.type === "choice") {
    ctl = buildChoice(f);
  } else {
    const input = document.createElement("input");
    input.type = "text";
    input.className = "textbox";
    input.dataset.key = f.key;
    input.placeholder = f.placeholder || "";
    input.value = f.value == null ? "" : String(f.value);
    input.addEventListener("input", onValueInput);
    ctl = input;
  }
  if (ctl) wrap.appendChild(ctl);
  if (f.help) wrap.appendChild(helpEl(f.help));
  return wrap;
}

function helpEl(text) {
  const s = document.createElement("span");
  s.className = "field-help";
  s.textContent = text;
  return s;
}

function buildBoolSwitch(f) {
  const row = document.createElement("div");
  row.className = "switch-row";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.className = "switch";
  cb.dataset.key = f.key;
  cb.dataset.role = "bool";
  const cur = f.value == null ? f.default : f.value;
  cb.checked = String(cur).toUpperCase() === "YES" || String(cur).toUpperCase() === "TRUE";
  cb.addEventListener("change", onValueInput);

  const track = document.createElement("span");
  track.className = "switch-track";
  const setOn = (on) => {
    cb.checked = on;
    cb.dispatchEvent(new Event("change", { bubbles: true }));
  };
  track.addEventListener("click", (e) => { e.preventDefault(); setOn(!cb.checked); });
  row.appendChild(cb);
  row.appendChild(track);

  const labels = document.createElement("div");
  labels.className = "switch-labels";
  const on = document.createElement("span"); on.className = "on"; on.textContent = "是 / YES";
  const off = document.createElement("span"); off.className = "off"; off.textContent = "否 / NO";
  on.addEventListener("click", (e) => { e.preventDefault(); setOn(true); });
  off.addEventListener("click", (e) => { e.preventDefault(); setOn(false); });
  labels.appendChild(on); labels.appendChild(off);
  row.appendChild(labels);
  return row;
}

function buildChoice(f) {
  const row = document.createElement("div");
  row.className = "choice-row";

  const sel = document.createElement("select");
  sel.className = "select";
  sel.dataset.key = f.key;
  sel.dataset.role = "choice";

  const opts = f.options || [];
  let cur = f.value == null ? f.default : f.value;
  cur = String(cur);
  const hasCustom = f.allowCustom && !opts.some((o) => String(o.value) === cur);

  opts.forEach((o) => {
    const e = document.createElement("option");
    e.value = o.value;
    e.textContent = o.label != null ? `${o.label}` : o.value;
    if (String(o.value) === cur) e.selected = true;
    sel.appendChild(e);
  });
  if (hasCustom || (f.allowCustom && !opts.length)) {
    const e = document.createElement("option");
    e.value = "__custom__";
    e.textContent = hasCustom ? `自定义：${cur}` : "自定义…";
    if (hasCustom) e.selected = true;
    sel.appendChild(e);
  }

  const text = document.createElement("input");
  text.type = "text";
  text.className = "textbox";
  text.dataset.role = "choice-custom";
  text.value = hasCustom ? cur : "";
  text.placeholder = f.placeholder || "输入自定义值";
  if (!hasCustom) text.classList.add("hidden");

  const update = () => {
    const isCustom = sel.value === "__custom__";
    text.classList.toggle("hidden", !isCustom);
    if (!isCustom && text.value) text.value = "";
  };
  sel.addEventListener("change", (ev) => { update(); onValueInput(ev); });
  text.addEventListener("input", onValueInput);

  row.appendChild(sel);
  if (f.allowCustom) row.appendChild(text);
  return row;
}

/* ---------------------------------------------------------------- 取值 */
function rebuildValues() {
  if (!state.payload) return;
  state.values = {};
  const fields = state.payload.fields || [];
  for (const f of fields) {
    const el = findFieldEl(f.key);
    if (el) {
      state.values[f.key] = readValue(f, el);
    } else {
      state.values[f.key] = f.value == null ? (f.default != null ? f.default : "") : f.value;
    }
  }
  previewName();
}

function findFieldEl(key) {
  const host = $("fieldsHost");
  return host.querySelector(`[data-key="${CSS.escape(key)}"]`);
}

function readValue(f, el) {
  if (el.dataset.role === "choice") {
    if (el.value === "__custom__") {
      const text = $("fieldsHost").querySelector(`[data-key="${CSS.escape(f.key)}"][data-role="choice-custom"]`);
      return text ? text.value.trim() : "";
    }
    return el.value;
  }
  if (el.type === "checkbox") return el.checked ? "YES" : "NO";
  return el.value == null ? "" : String(el.value).trim();
}

function onValueInput(ev) {
  if (!state.payload) return;
  const el = (ev && ev.target) || document.activeElement;
  if (el && el.dataset && el.dataset.key) {
    const f = (state.payload.fields || []).find((x) => x.key === el.dataset.key);
    if (f) state.values[f.key] = readValue(f, el);
  }
  previewName();
}

/* ---------------------------------------------------------------- 部位贴图（从零选择图片） */
function renderTextures(payload) {
  const tp = (payload && payload.textureParts) || {};
  state.texItems = ((tp.parts) || []).map((p) => ({
    custom: false,
    label: p.label || p.target,
    help: p.help || "",
    target: p.target,
    src: "",
  }));
  state.texCustom = [];
  state.iconSetup = (payload && payload.iconSetup) || null;
  state.iconSrc = "";
  const card = $("card-textures");
  card.classList.remove("hidden");
  $("texNote").textContent = tp.note ||
    "为下面的部位「选择图片…」，程序会把图片放到机模认识的名字/位置；不选的部位不会生成。";
  drawIcon();
  drawTextures();
}

/* ---------------------------------------------------------------- 涂装图标 */
function iconStatusText() {
  const ic = state.iconSetup;
  if (!ic) return "";
  if (state.iconSrc) return "已指定：" + state.iconSrc;
  if (ic.autoSource) {
    return "未指定 → 不会生成图标；若你已为「" + ic.autoSource +
      "」这个部位选了图片，打包时会自动用它生成";
  }
  return "未指定 → 打包结果不会生成涂装图标（可选）";
}

function drawIcon() {
  const host = $("iconHost");
  if (!host) return;
  host.innerHTML = "";
  const ic = state.iconSetup;
  if (!ic) {
    host.classList.add("hidden");
    return;
  }
  host.classList.remove("hidden");

  const box = document.createElement("div");
  box.className = "field tex-field icon-field";

  const name = document.createElement("span");
  name.className = "field-name";
  name.textContent = "涂装图标";
  box.appendChild(name);

  const fname = document.createElement("span");
  fname.className = "mono tex-fname";
  fname.textContent = "→ " + ic.bigName + " + " + ic.thumbName;
  box.appendChild(fname);

  const pathline = document.createElement("div");
  pathline.className = "tex-pathline mono";
  pathline.textContent = iconStatusText();
  box.appendChild(pathline);

  const row = document.createElement("div");
  row.className = "tex-row";

  const pick = document.createElement("button");
  pick.type = "button";
  pick.className = "btn btn-accent";
  pick.textContent = "📁 选择图片…";
  pick.addEventListener("click", pickIcon);
  row.appendChild(pick);

  if (state.iconSrc) {
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "btn";
    clear.textContent = "✕ 清除（自动）";
    clear.addEventListener("click", () => { state.iconSrc = ""; drawIcon(); });
    row.appendChild(clear);
  }
  box.appendChild(row);

  box.appendChild(helpEl(
    state.iconSrc
      ? "将用这张图片生成大图标与缩略图；PNG 会原样使用，其它格式自动转成 PNG。"
      : "X-Plane 的涂装列表需要 *_icon11.png 作为选择界面缩略图。可「选择图片…」直接指定；"
        + (ic.autoSource ? "也可为「" + ic.autoSource + "」部位选图后让程序自动生成。" : "")
  ));
  host.appendChild(box);
}

async function pickIcon() {
  const ic = state.iconSetup;
  if (!ic) return;
  const startDir = (state.info && state.info.browseStart) || "";
  const res = await api.post("/api/browse-file", {
    title: "选择一张图片作为涂装图标（将生成 " + ic.bigName + " 与缩略图）",
    startDir,
  });
  if (res.error) { toast("本机无法弹出文件选择框：" + res.error, true); return; }
  if (res.cancelled) return;
  if (res.path) { state.iconSrc = res.path; drawIcon(); }
}

function allTexItems() { return state.texItems.concat(state.texCustom); }
function itemAt(idx) { return allTexItems()[idx]; }

function texStatusText(it) {
  if (it.src) return "已指定：" + it.src;
  return "未选择 → 打包结果将不生成该文件（可选）";
}

function texRow(it, idx) {
  const box = document.createElement("div");
  box.className = "field tex-field";
  box.dataset.idx = idx;

  const name = document.createElement("span");
  name.className = "field-name";
  name.textContent = it.custom ? (it.label || "自定义部位") : it.label;
  box.appendChild(name);

  const pathline = document.createElement("div");
  pathline.className = "tex-pathline mono";
  pathline.textContent = texStatusText(it);
  box.appendChild(pathline);

  const row = document.createElement("div");
  row.className = "tex-row";

  if (it.custom) {
    const tgt = document.createElement("input");
    tgt.type = "text";
    tgt.className = "textbox mono tex-target";
    tgt.value = it.target || "";
    tgt.placeholder = "目标路径，如 objects/wingR.dds";
    tgt.addEventListener("change", (e) => { it.target = e.target.value.trim(); drawTextures(); });
    row.appendChild(tgt);
  }

  const pick = document.createElement("button");
  pick.type = "button";
  pick.className = "btn btn-accent";
  pick.textContent = "📁 选择图片…";
  pick.addEventListener("click", () => pickTexture(idx));
  row.appendChild(pick);

  if (it.src) {
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "btn";
    clear.textContent = "✕ 清除";
    clear.addEventListener("click", () => { it.src = ""; drawTextures(); });
    row.appendChild(clear);
  }
  if (it.custom) {
    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn";
    del.textContent = "删除";
    del.addEventListener("click", () => {
      state.texCustom = state.texCustom.filter((x) => x !== it);
      drawTextures();
    });
    row.appendChild(del);
  }
  box.appendChild(row);
  if (it.help) box.appendChild(helpEl(it.help));
  return box;
}

function drawTextures() {
  const host = $("texHost");
  host.innerHTML = "";
  const items = allTexItems();
  if (!items.length) {
    host.innerHTML = '<div class="empty">该机模档案未声明固定部位；若需把某张图放到指定文件名，点下方「＋ 添加自定义部位」。</div>';
    return;
  }
  const wrap = document.createElement("div");
  wrap.className = "tex-list";
  items.forEach((it, i) => wrap.appendChild(texRow(it, i)));
  host.appendChild(wrap);
}

async function pickTexture(idx) {
  const it = itemAt(idx);
  if (!it) return;
  const startDir = (state.info && state.info.browseStart) || "";
  const res = await api.post("/api/browse-file", {
    title: "选择「" + (it.label || it.target) + "」要用的图片",
    startDir,
  });
  if (res.error) { toast("本机无法弹出文件选择框：" + res.error, true); return; }
  if (res.cancelled) return;
  if (res.path) { it.src = res.path; drawTextures(); }
}

$("btnAddTex").addEventListener("click", () => {
  if (!state.payload) { toast("请先选择机模档案", true); return; }
  state.texCustom.push({
    custom: true, label: "自定义部位", help: "",
    target: "", src: "",
  });
  drawTextures();
});

function collectTextureMap() {
  const map = {};
  for (const it of allTexItems()) {
    if (!it.src) continue;
    const tgt = (it.target || "").trim().replace(/\\/g, "/");
    if (!tgt) {
      toast("“" + (it.label || "自定义部位") + "”已选图片，但缺少目标路径，请先填写（如 objects/fuselage.dds）", true);
      return null;
    }
    map[tgt] = it.src;
  }
  return map;
}

/* ---------------------------------------------------------------- 打包 */
$("btnPackage").addEventListener("click", async () => {
  if (!state.payload) { toast("请先等待机模档案加载完成", true); return; }
  rebuildValues();
  const outputDir = $("outputDir").value.trim() ||
    state.outputDir || (state.info && state.info.outputDir) || "";
  if (!outputDir) { toast("请填写输出文件夹", true); return; }

  const texMap = collectTextureMap();
  if (texMap === null) return; // 已 toast 提示
  const body = {
    profileId: state.profileId,
    outputDir,
    values: state.values,
    textureMap: texMap,
    iconPath: state.iconSrc || null,
    overwrite: !!$("overwriteChk").checked,
  };

  state.busy = true;
  $("btnPackage").disabled = true;
  $("btnPackage").textContent = "打包中…";
  try {
    let res = await api.post("/api/package", body);
    if (res.error && /已存在/.test(res.error)) {
      const ok = confirm("输出文件夹已存在同名内容：\n\n" + res.error + "\n\n是否覆盖并重新打包？");
      if (ok) {
        body.overwrite = true;
        res = await api.post("/api/package", body);
      } else {
        toast("已取消", false);
        return;
      }
    }
    if (res.error) { toast(res.error, true); return; }
    showResult(res);
    scrollTo($("card-result"));
    toast("打包完成 ✔");
  } catch (e) {
    toast("打包出错：" + e, true);
  } finally {
    state.busy = false;
    $("btnPackage").disabled = false;
    $("btnPackage").textContent = "🚀 开始打包";
  }
});

function showResult(res) {
  const card = $("card-result");
  card.classList.remove("hidden");

  const actions = $("resultActions");
  const warns = res.warnings || [];
  actions.innerHTML = "";
  if ((res.actions || []).length) {
    const ul = document.createElement("ul");
    for (const a of res.actions || []) {
      const li = document.createElement("li");
      li.textContent = a;
      ul.appendChild(li);
    }
    actions.appendChild(ul);
  }
  if (warns.length) {
    const b = document.createElement("b");
    b.textContent = "注意：";
    actions.appendChild(b);
    const ul2 = document.createElement("ul");
    for (const w of warns) {
      const li = document.createElement("li");
      li.textContent = w;
      li.style.color = "#96600a";
      ul2.appendChild(li);
    }
    actions.appendChild(ul2);
  }

  $("resultPath").textContent = res.target;
  $("resultPath").title = res.target;

  // 图标预览（能通过 web 提供到的则显示；本地输出图片无法直读，仅展示文件名）
  const iconBox = $("resultIcons");
  iconBox.innerHTML = "";
  const names = [];
  if (res.icons && res.icons.big) names.push(res.icons.big);
  if (res.icons && res.icons.thumb) names.push(res.icons.thumb);
  (res.icons && res.icons.existing || []).forEach((n) => names.push(n));
  if (names.length) {
    iconBox.innerHTML = "<div class='hint'>已生成 / 保留图标：" +
      names.map((n) => "<b class='mono'>" + esc(n) + "</b>").join("，") + "</div>";
  }
}

init();
