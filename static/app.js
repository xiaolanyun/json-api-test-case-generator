(() => {
  "use strict";

  const state = {
    fields: [],
    selected: new Set(),
    activeTab: "all",
    result: null,
  };

  const byId = (id) => document.getElementById(id);
  const jsonInput = byId("json-input");
  const configSection = byId("config-section");
  const resultSection = byId("result-section");
  const fieldTableBody = document.querySelector("#field-table tbody");
  const previewHead = document.querySelector("#preview-table thead");
  const previewBody = document.querySelector("#preview-table tbody");

  function setMessage(id, text = "", success = false) {
    const node = byId(id);
    node.textContent = text;
    node.classList.toggle("success", success);
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", "\"": "&quot;",
    }[char]));
  }

  async function api(path, payload) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await response.json().catch(() => ({ ok: false, error: "服务返回格式不正确。" }));
    if (!response.ok || !body.ok) throw new Error(body.error || "操作失败，请稍后重试。");
    return body;
  }

  function updateSteps(step) {
    document.querySelectorAll(".steps li").forEach((item, index) => {
      item.classList.toggle("active", index === step - 1);
      item.classList.toggle("done", index < step - 1);
    });
  }

  function isSelectable(field) {
    return byId("container-switch").checked || !field.is_container;
  }

  function selectedVisibleFields() {
    const filter = byId("field-search").value.trim().toLowerCase();
    return state.fields.filter((field) => {
      const searchable = `${field.var_name} ${field.raw_name} ${field.path}`.toLowerCase();
      return !filter || searchable.includes(filter);
    });
  }

  function updateCountAndEstimate() {
    const selectable = state.fields.filter(isSelectable);
    for (const name of [...state.selected]) {
      if (!selectable.some((field) => field.var_name === name)) state.selected.delete(name);
    }
    const count = state.selected.size;
    byId("selected-count").textContent = count;
    byId("estimate").textContent = count
      ? `预计生成：缺失 ${count + 1} 条、为空 ${count + 1} 条、类型错误 ${count + 1} 条；汇总 ${count * 3 + 1} 条。`
      : "尚未选择必填字段：仍可生成，但四个 CSV 都只包含 1 条正常基准用例。";
  }

  function renderFieldTree() {
    const children = new Map();
    state.fields.forEach((field) => {
      const parent = field.parent || "ROOT";
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(field);
    });
    const renderBranch = (parent) => {
      const items = children.get(parent) || [];
      if (!items.length) return "";
      return `<ul>${items.map((field) => `<li><code>${escapeHtml(field.var_name)}</code><span class="tree-type">${escapeHtml(field.type)} · ${escapeHtml(field.path)}</span>${renderBranch(field.var_name)}</li>`).join("")}</ul>`;
    };
    byId("field-tree-content").innerHTML = renderBranch("ROOT") || "<p class=\"muted\">未发现可展示字段。</p>";
  }

  function renderFieldTable() {
    const visibleFields = selectedVisibleFields();
    fieldTableBody.innerHTML = visibleFields.map((field) => {
      const disabled = !isSelectable(field);
      const checked = state.selected.has(field.var_name) && !disabled;
      return `<tr>
        <td><input class="field-check" type="checkbox" data-name="${escapeHtml(field.var_name)}" ${checked ? "checked" : ""} ${disabled ? "disabled" : ""} aria-label="${escapeHtml(field.var_name)} 是否必填"></td>
        <td><code>${escapeHtml(field.var_name)}</code></td>
        <td>${escapeHtml(field.raw_name)}</td>
        <td><code>${escapeHtml(field.path)}</code></td>
        <td>${escapeHtml(field.type)}</td>
        <td>${escapeHtml(field.parent)}</td>
        <td>${field.is_container ? "是" : "否"}</td>
        <td title="${escapeHtml(field.sample_value)}">${escapeHtml(field.sample_value)}</td>
      </tr>`;
    }).join("") || "<tr><td colspan=\"8\" class=\"muted\">没有匹配字段。</td></tr>";
    renderFieldTree();
    updateCountAndEstimate();
  }

  function validateCodes() {
    const normal = Number(byId("normal-code").value);
    const error = Number(byId("error-code").value);
    if (!Number.isInteger(normal) || normal < 100 || normal > 599 || !Number.isInteger(error) || error < 100 || error > 599) {
      throw new Error("正常和异常预期状态码都必须是 100 至 599 的整数。\n");
    }
    return { default_normal_code: normal, default_error_code: error, validate_container_fields: byId("container-switch").checked };
  }

  function prettyRequestBody(body) {
    try { return JSON.stringify(JSON.parse(body), null, 2); } catch (_) { return body; }
  }

  async function copyRequestBody() {
    const content = byId("body-content").textContent;
    const status = byId("copy-body-status");
    if (!content) return;
    try {
      let copied = false;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        try {
          await navigator.clipboard.writeText(content);
          copied = true;
        } catch (_) {
          // 某些本地浏览器策略会拒绝 Clipboard API，继续使用兼容复制方式。
        }
      }
      if (!copied) {
        const temporaryInput = document.createElement("textarea");
        temporaryInput.value = content;
        temporaryInput.setAttribute("readonly", "");
        temporaryInput.style.position = "fixed";
        temporaryInput.style.opacity = "0";
        document.body.appendChild(temporaryInput);
        temporaryInput.select();
        copied = document.execCommand("copy");
        temporaryInput.remove();
        if (!copied) throw new Error("copy failed");
      }
      status.textContent = "已复制";
    } catch (_) {
      status.textContent = "复制失败，请手动复制";
    }
    window.setTimeout(() => { status.textContent = ""; }, 1800);
  }

  function renderResult() {
    const { header, previews, summary } = state.result;
    const labelMap = {
      field_count: "字段总数", required_count: "必填字段", missing_count: "缺失用例", empty_count: "空值用例", type_count: "类型错误", all_count: "汇总用例",
    };
    byId("result-summary").innerHTML = Object.entries(summary).map(([key, value]) =>
      `<div class="summary-item"><span>${labelMap[key]}</span><strong>${value}</strong></div>`
    ).join("");

    previewHead.innerHTML = `<tr>${header.map((column) => `<th>${escapeHtml(column)}</th>`).join("")}</tr>`;
    const rows = previews[state.activeTab];
    previewBody.innerHTML = rows.map((row, index) => `<tr>${header.map((column) => {
      if (column === "request_body") return `<td><button class="request-body-button" data-row="${index}" type="button">查看 JSON</button></td>`;
      return `<td>${escapeHtml(row[column])}</td>`;
    }).join("")}</tr>`).join("") || "<tr><td class=\"muted\">没有可预览的用例。</td></tr>";
  }

  async function analyze() {
    setMessage("input-message");
    byId("analyze-button").disabled = true;
    byId("analyze-button").textContent = "分析中…";
    try {
      const result = await api("/api/analyze", { json_text: jsonInput.value });
      state.fields = result.fields;
      state.selected.clear();
      state.result = null;
      resultSection.classList.add("hidden");
      byId("analysis-summary").textContent = `已识别 ${result.field_count} 个字段；顶层类型：${result.top_level_type}。请勾选需要验证的必填字段。`;
      configSection.classList.remove("hidden");
      updateSteps(2);
      renderFieldTable();
      configSection.scrollIntoView({ behavior: "smooth", block: "start" });
      setMessage("input-message", "字段分析完成。", true);
    } catch (error) {
      configSection.classList.add("hidden");
      resultSection.classList.add("hidden");
      updateSteps(1);
      setMessage("input-message", error.message);
    } finally {
      byId("analyze-button").disabled = false;
      byId("analyze-button").textContent = "分析字段";
    }
  }

  async function generate() {
    setMessage("config-message");
    try {
      const config = validateCodes();
      byId("generate-button").disabled = true;
      byId("generate-button").textContent = "生成中…";
      state.result = await api("/api/generate", {
        json_text: jsonInput.value,
        required_fields: [...state.selected],
        config,
      });
      state.activeTab = "all";
      document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === state.activeTab));
      renderResult();
      resultSection.classList.remove("hidden");
      updateSteps(3);
      setMessage("config-message", "测试用例生成完成。", true);
      resultSection.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      setMessage("config-message", error.message.trim());
    } finally {
      byId("generate-button").disabled = false;
      byId("generate-button").textContent = "生成测试用例";
    }
  }

  byId("analyze-button").addEventListener("click", analyze);
  byId("format-button").addEventListener("click", () => {
    try {
      jsonInput.value = JSON.stringify(JSON.parse(jsonInput.value), null, 2);
      setMessage("input-message", "JSON 已格式化。", true);
    } catch (_) { setMessage("input-message", "JSON 格式错误，无法格式化。请先修正后再试。"); }
  });
  byId("clear-button").addEventListener("click", () => {
    if (jsonInput.value && !window.confirm("确定清空当前 JSON 和已分析结果吗？")) return;
    jsonInput.value = "";
    state.fields = [];
    state.selected.clear();
    configSection.classList.add("hidden");
    resultSection.classList.add("hidden");
    updateSteps(1);
    setMessage("input-message");
  });
  byId("file-input").addEventListener("change", async (event) => {
    const [file] = event.target.files;
    if (!file) return;
    if (file.size > 1024 * 1024) { setMessage("input-message", "文件超过 1 MB 限制。\n"); return; }
    if (jsonInput.value && !window.confirm("上传文件将覆盖当前编辑区内容，是否继续？")) { event.target.value = ""; return; }
    jsonInput.value = await file.text();
    event.target.value = "";
    setMessage("input-message", "文件已载入，请点击“分析字段”。", true);
  });
  byId("field-search").addEventListener("input", renderFieldTable);
  fieldTableBody.addEventListener("change", (event) => {
    if (!event.target.matches(".field-check")) return;
    const name = event.target.dataset.name;
    if (event.target.checked) state.selected.add(name); else state.selected.delete(name);
    updateCountAndEstimate();
  });
  byId("container-switch").addEventListener("change", renderFieldTable);
  byId("select-visible-button").addEventListener("click", () => {
    selectedVisibleFields().filter(isSelectable).forEach((field) => state.selected.add(field.var_name));
    renderFieldTable();
  });
  byId("select-leaves-button").addEventListener("click", () => {
    state.fields.filter((field) => !field.is_container).forEach((field) => state.selected.add(field.var_name));
    renderFieldTable();
  });
  byId("clear-selection-button").addEventListener("click", () => { state.selected.clear(); renderFieldTable(); });
  byId("generate-button").addEventListener("click", generate);
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    state.activeTab = tab.dataset.tab;
    document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item === tab));
    renderResult();
  }));
  previewBody.addEventListener("click", (event) => {
    const button = event.target.closest(".request-body-button");
    if (!button || !state.result) return;
    const body = state.result.previews[state.activeTab][Number(button.dataset.row)].request_body;
    byId("body-content").textContent = prettyRequestBody(body);
    byId("copy-body-status").textContent = "";
    byId("body-dialog").showModal();
  });
  byId("copy-body-button").addEventListener("click", copyRequestBody);
  byId("close-dialog").addEventListener("click", () => byId("body-dialog").close());
  document.querySelectorAll(".download-button").forEach((button) => button.addEventListener("click", () => {
    if (!state.result) return;
    window.location.assign(`/api/download/${state.result.job_id}/${button.dataset.file}`);
  }));
})();
