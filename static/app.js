// ----- Sortable data tables (click th.sortable to sort) -----
(function () {
  const tables = document.querySelectorAll("table.data-table");
  tables.forEach(table => {
    const headers = table.querySelectorAll("th.sortable");
    if (!headers.length) return;
    const tbody = table.tBodies[0];
    if (!tbody) return;

    headers.forEach((th, colIdx) => {
      const colIndex = Array.prototype.indexOf.call(th.parentNode.children, th);
      th.addEventListener("click", () => {
        const isNumber = th.dataset.sortType === "number";
        const current = th.getAttribute("aria-sort");
        let dir;
        if (current === "ascending") dir = "descending";
        else if (current === "descending") dir = "ascending";
        else dir = th.dataset.sortDefault === "desc" ? "descending" : "ascending";

        // Reset siblings
        th.parentNode.querySelectorAll("th.sortable").forEach(other => {
          if (other !== th) other.setAttribute("aria-sort", "none");
        });
        th.setAttribute("aria-sort", dir);

        const rows = Array.from(tbody.querySelectorAll("tr")).filter(
          tr => !tr.querySelector(".empty")
        );
        const getKey = (tr) => {
          const cell = tr.children[colIndex];
          if (!cell) return isNumber ? 0 : "";
          const raw = cell.dataset.sort != null ? cell.dataset.sort : cell.textContent.trim();
          if (isNumber) {
            const n = parseFloat(raw);
            return Number.isNaN(n) ? 0 : n;
          }
          return raw;
        };

        // Group matched sets together so children + ghost stay glued to their
        // parent across re-sorts. A row that's a child or ghost (tx-row-child
        // class — ghosts inherit it) belongs to whichever group came before.
        // The group's sort key comes from its leader (the parent or the
        // standalone unmatched row).
        const groups = [];
        for (const tr of rows) {
          if (tr.classList.contains("tx-row-child") && groups.length) {
            groups[groups.length - 1].push(tr);
          } else {
            groups.push([tr]);
          }
        }

        const mult = dir === "ascending" ? 1 : -1;
        groups.sort((a, b) => {
          const ka = getKey(a[0]);
          const kb = getKey(b[0]);
          if (ka < kb) return -1 * mult;
          if (ka > kb) return  1 * mult;
          return 0;
        });
        for (const grp of groups) {
          for (const tr of grp) tbody.appendChild(tr);
        }
      });
    });
  });
})();

// ----- Shared hierarchical topic→category picker -----
// Two pickers share this UI on the transactions page:
//  - per-row .cat-select (assigning categories to transactions)
//  - .cat-filter-select in the sidebar (filtering by category)
// Exposes window.__hierPicker. Owns single-open-popup state so the two pickers
// don't overlap each other.
(function () {
  let topics = null;
  let catIndex = null;

  function loadTopics() {
    if (topics !== null) return topics;
    const host = document.querySelector(".data-list");
    try {
      topics = JSON.parse(host?.getAttribute("data-picker-topics") || "[]");
    } catch {
      topics = [];
    }
    catIndex = new Map();
    for (const t of topics) {
      for (const c of t.categories || []) {
        catIndex.set(String(c.id), { cat: c, topic: t });
      }
    }
    return topics;
  }
  function getCatIndex() { loadTopics(); return catIndex; }

  let openPopup = null;
  function closeOpenPopup() {
    if (!openPopup) return;
    openPopup.el.remove();
    if (openPopup.cd) openPopup.cd.classList.remove("is-open");
    openPopup.anchor?.setAttribute?.("aria-expanded", "false");
    openPopup = null;
  }
  function isOpenFor(anchor) { return !!(openPopup && openPopup.anchor === anchor); }

  document.addEventListener("mousedown", (e) => {
    if (!openPopup) return;
    if (openPopup.anchor.contains(e.target) || openPopup.el.contains(e.target)) return;
    closeOpenPopup();
  });
  window.addEventListener("resize", closeOpenPopup);
  window.addEventListener("scroll", (e) => {
    if (!openPopup) return;
    if (openPopup.el.contains(e.target)) return;
    closeOpenPopup();
  }, true);

  function flatSearch(q) {
    const results = [];
    for (const t of loadTopics()) {
      for (const c of t.categories) {
        const haystack = (c.name + " " + t.name).toLowerCase();
        if (haystack.includes(q)) results.push({ cat: c, topic: t });
      }
    }
    // Prefer category-name matches over topic-only matches.
    results.sort((a, b) => {
      const ah = a.cat.name.toLowerCase().includes(q) ? 0 : 1;
      const bh = b.cat.name.toLowerCase().includes(q) ? 0 : 1;
      return ah - bh;
    });
    return results.slice(0, 50);
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function makeOption({ label, color, suffix, isPlaceholder, isSelected, onClick }) {
    const item = document.createElement("div");
    item.className = "cd-option";
    if (isPlaceholder) item.classList.add("is-placeholder");
    if (isSelected) item.classList.add("is-selected");
    if (color) {
      const dot = document.createElement("span");
      dot.className = "cd-dot";
      dot.style.background = color;
      item.appendChild(dot);
    }
    const txt = document.createElement("span");
    txt.className = "cd-option-label";
    txt.textContent = label;
    item.appendChild(txt);
    if (suffix) {
      const sfx = document.createElement("span");
      sfx.className = "cd-option-suffix";
      sfx.textContent = suffix;
      item.appendChild(sfx);
    }
    item.addEventListener("mousedown", (e) => e.preventDefault());
    item.addEventListener("click", (e) => { e.stopPropagation(); onClick(); });
    return item;
  }

  // open({ anchor, currentValue, prependOptions, onPick })
  //   anchor:         element to position against (also the toggle-close key)
  //   currentValue:   string id of currently selected category, or ""
  //   prependOptions: optional [{label, color, isPlaceholder, onPick}] shown
  //                   above topics in "topics" mode (pseudo-options like "All"
  //                   or "Uncategorized only")
  //   onPick(catId):  called when a real category is picked
  function open({ anchor, currentValue, prependOptions, onPick }) {
    const ts = loadTopics();
    closeOpenPopup();

    const el = document.createElement("div");
    el.className = "cd-popup cd-popup-hier";
    el.setAttribute("role", "listbox");

    const search = document.createElement("input");
    search.type = "search";
    search.className = "cd-search";
    search.placeholder = "Typ om te zoeken…";
    search.autocomplete = "off";
    el.appendChild(search);

    const list = document.createElement("div");
    list.className = "cd-list";
    el.appendChild(list);

    document.body.appendChild(el);
    const cd = anchor.closest(".cd");
    if (cd) cd.classList.add("is-open");
    anchor.setAttribute?.("aria-expanded", "true");

    // Position popup relative to the anchor (trigger button). Width can be
    // set up front, but the vertical placement needs the actual rendered
    // height, so the flip-upward check runs after renderList() below.
    const anchorRect = anchor.getBoundingClientRect();
    el.style.width = Math.max(220, anchorRect.width + 14) + "px";
    el.style.left = anchorRect.left + "px";
    el.style.top = (anchorRect.bottom + 4) + "px";

    // Once we've decided to open upward, stay upward — prevents the popup
    // from jumping back down as the user filters with the search box.
    let openUp = false;
    function positionPopup() {
      const r = anchor.getBoundingClientRect();
      const popupH = Math.min(360, el.scrollHeight);
      const spaceBelow = window.innerHeight - r.bottom;
      const spaceAbove = r.top;
      if (!openUp && spaceBelow < popupH + 12 && spaceAbove > spaceBelow) {
        openUp = true;
      }
      if (openUp) {
        el.style.top = Math.max(8, r.top - popupH - 4) + "px";
      } else {
        el.style.top = (r.bottom + 4) + "px";
      }
    }

    const state = {
      mode: "topics",   // "topics" | "cats" | "search"
      topicId: null,
      activeIdx: -1,
      flatResults: [],
    };
    openPopup = { anchor, el, cd, state };

    renderList();   // also calls positionPopup() at the end
    setTimeout(() => search.focus(), 0);

    search.addEventListener("input", () => {
      const q = search.value.trim().toLowerCase();
      if (!q) {
        if (state.topicId) state.mode = "cats"; else state.mode = "topics";
        state.flatResults = [];
      } else {
        state.mode = "search";
        state.flatResults = flatSearch(q);
      }
      state.activeIdx = state.flatResults.length || state.mode !== "search" ? 0 : -1;
      renderList();
    });

    search.addEventListener("keydown", (e) => {
      const items = list.querySelectorAll(".cd-option:not(.is-disabled)");
      if (e.key === "ArrowDown") {
        e.preventDefault();
        state.activeIdx = Math.min(items.length - 1, state.activeIdx + 1);
        highlightActive();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        state.activeIdx = Math.max(0, state.activeIdx - 1);
        highlightActive();
      } else if (e.key === "ArrowLeft" && state.mode === "cats") {
        e.preventDefault();
        state.mode = "topics";
        state.topicId = null;
        state.activeIdx = 0;
        renderList();
      } else if (e.key === "Enter") {
        e.preventDefault();
        const active = items[state.activeIdx];
        if (active) active.click();
      } else if (e.key === "Escape") {
        e.preventDefault();
        closeOpenPopup();
        anchor.focus?.();
      }
    });

    function highlightActive() {
      list.querySelectorAll(".cd-option").forEach((el, i) => {
        el.classList.toggle("is-active", i === state.activeIdx);
      });
      const active = list.querySelectorAll(".cd-option")[state.activeIdx];
      if (active) active.scrollIntoView({ block: "nearest" });
    }

    function pickCat(catId) {
      try { onPick(catId); } finally { closeOpenPopup(); }
    }

    function renderList() {
      list.innerHTML = "";
      if (state.mode === "topics") {
        for (const opt of (prependOptions || [])) {
          list.appendChild(makeOption({
            label: opt.label,
            color: opt.color,
            isPlaceholder: !!opt.isPlaceholder,
            onClick: () => { try { opt.onPick(); } finally { closeOpenPopup(); } },
          }));
        }
        for (const t of ts) {
          list.appendChild(makeOption({
            label: t.name,
            color: t.color,
            suffix: `${t.categories.length}`,
            onClick: () => {
              state.mode = "cats";
              state.topicId = t.id;
              state.activeIdx = 0;
              renderList();
            },
          }));
        }
      } else if (state.mode === "cats") {
        const t = ts.find(tt => tt.id === state.topicId);
        const header = document.createElement("div");
        header.className = "cd-header";
        header.innerHTML = `<button type="button" class="cd-back" aria-label="Terug naar onderwerpen">← Onderwerpen</button>
                            <span class="cd-header-title"><span class="cd-dot" style="background:${t?.color || '#94a3b8'};"></span>${escapeHtml(t?.name || "")}</span>`;
        header.querySelector(".cd-back").addEventListener("click", () => {
          state.mode = "topics";
          state.topicId = null;
          state.activeIdx = 0;
          renderList();
        });
        list.appendChild(header);
        if (!t || !t.categories.length) {
          const empty = document.createElement("div");
          empty.className = "cd-empty";
          empty.textContent = "Nog geen categorieën onder dit onderwerp.";
          list.appendChild(empty);
        } else {
          for (const c of t.categories) {
            list.appendChild(makeOption({
              label: c.name,
              color: c.color,
              onClick: () => pickCat(String(c.id)),
              isSelected: String(c.id) === String(currentValue),
            }));
          }
        }
      } else {
        if (!state.flatResults.length) {
          const empty = document.createElement("div");
          empty.className = "cd-empty";
          empty.textContent = "Geen resultaten.";
          list.appendChild(empty);
        } else {
          for (const res of state.flatResults) {
            list.appendChild(makeOption({
              label: res.cat.name,
              suffix: res.topic.name,
              color: res.cat.color,
              onClick: () => pickCat(String(res.cat.id)),
              isSelected: String(res.cat.id) === String(currentValue),
            }));
          }
        }
      }
      const items = list.querySelectorAll(".cd-option:not(.is-disabled)");
      if (items.length) {
        state.activeIdx = Math.max(0, Math.min(state.activeIdx, items.length - 1));
        highlightActive();
      }
      positionPopup();
    }
  }

  window.__hierPicker = { open, close: closeOpenPopup, isOpenFor, getTopics: loadTopics, getCatIndex };
})();

// ----- Transactions: cat-select assigning picker + save + suggestion accept -----
// Per-row picker that writes to a hidden input and POSTs to
// /transactions/<id>/category. Uses window.__hierPicker for the popup UI.
(function () {
  const selects = document.querySelectorAll(".cat-select");
  if (!selects.length) return;

  const getCatIndex = () => window.__hierPicker.getCatIndex();

  function selValue(sel) {
    const hid = sel.parentElement?.querySelector(".cat-select-input");
    return hid ? hid.value : "";
  }
  function setSelValue(sel, v, { silent = false } = {}) {
    const hid = sel.parentElement?.querySelector(".cat-select-input");
    if (hid) hid.value = v || "";
    sel.dataset.value = v || "";
    renderTrigger(sel);
    if (!silent) sel.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function selIsDisabled(sel) {
    return sel.classList.contains("is-disabled");
  }
  function setSelDisabled(sel, disabled) {
    sel.classList.toggle("is-disabled", !!disabled);
  }

  // Track each picker's previous value. Pending rows are server-side
  // uncategorized, so prev should be "" even though we display the suggested
  // category.
  selects.forEach(sel => {
    const initial = sel.dataset.pending === "1" ? "" : (sel.dataset.selected || "");
    sel.dataset.value = sel.dataset.selected || "";
    sel.dataset.prevValue = initial;
    enhancePicker(sel);
  });

  const uncatEl = document.getElementById("uncat-count");
  const adjustUncat = (delta) => {
    if (!uncatEl || !delta) return;
    const cur = parseInt(uncatEl.textContent, 10) || 0;
    uncatEl.textContent = Math.max(0, cur + delta);
  };

  function clearPendingState(row) {
    if (!row) return;
    const sel = row.querySelector(".cat-select");
    if (sel) {
      sel.removeAttribute("data-pending");
      sel.removeAttribute("data-pending-color");
      sel.classList.remove("cat-select-pending");
      sel.removeAttribute("style");
      renderTrigger(sel);
    }
    const acceptBtn = row.querySelector(".btn-accept-sug");
    if (acceptBtn) acceptBtn.remove();
    const rejectBtn = row.querySelector(".btn-reject-sug");
    if (rejectBtn) rejectBtn.remove();
    const hint = row.querySelector(".sug-hint");
    if (hint) hint.remove();
    row.removeAttribute("data-sug-cat");
    row.removeAttribute("data-sug-source");
    row.removeAttribute("data-sug-key");
  }

  function enhancePicker(sel) {
    sel.classList.add("cd");
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "cd-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    const label = document.createElement("span");
    label.className = "cd-label";
    const arrow = document.createElement("span");
    arrow.className = "cd-arrow";
    arrow.setAttribute("aria-hidden", "true");
    trigger.appendChild(label);
    trigger.appendChild(arrow);
    sel.appendChild(trigger);
    renderTrigger(sel);

    trigger.addEventListener("click", (e) => {
      e.stopPropagation();
      if (selIsDisabled(sel)) return;
      if (window.__hierPicker.isOpenFor(trigger)) {
        window.__hierPicker.close();
      } else {
        openPicker(sel);
      }
    });
    trigger.addEventListener("keydown", (e) => {
      if (selIsDisabled(sel)) return;
      if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openPicker(sel);
      }
    });
  }

  function renderTrigger(sel) {
    const trigger = sel.querySelector(".cd-trigger");
    if (!trigger) return;
    const label = trigger.querySelector(".cd-label");
    const value = selValue(sel);
    const isPending = sel.dataset.pending === "1";
    const pendingColor = sel.dataset.pendingColor || "";
    const catIndex = getCatIndex();

    if (!value) {
      if (isPending) {
        // Show the suggested category greyed/colored even though the underlying
        // value is empty (server still considers the row uncategorized).
        const sugId = sel.closest("tr")?.getAttribute("data-sug-cat");
        const entry = sugId ? catIndex.get(String(sugId)) : null;
        if (entry) {
          label.textContent = `${entry.cat.name}`;
        } else {
          label.textContent = "— Ongecategoriseerd —";
        }
      } else {
        label.textContent = "— Ongecategoriseerd —";
      }
      label.classList.add("is-placeholder");
    } else {
      const entry = catIndex.get(String(value));
      label.textContent = entry ? `${entry.cat.name}` : "(onbekend)";
      label.classList.remove("is-placeholder");
    }

    // Pending visual treatment
    sel.classList.toggle("cat-select-pending", isPending);
    if (isPending && pendingColor) {
      trigger.style.backgroundColor = pendingColor + "1f";
      trigger.style.color = pendingColor;
      trigger.style.borderBottomColor = pendingColor + "80";
      trigger.classList.add("cd-trigger-pending");
    } else {
      trigger.style.backgroundColor = "";
      trigger.style.color = "";
      trigger.style.borderBottomColor = "";
      trigger.classList.remove("cd-trigger-pending");
    }
  }

  function openPicker(sel) {
    const trigger = sel.querySelector(".cd-trigger");
    window.__hierPicker.open({
      anchor: trigger,
      currentValue: selValue(sel),
      prependOptions: [{
        label: "— Ongecategoriseerd —",
        color: "#94a3b8",
        isPlaceholder: true,
        onPick: () => setSelValue(sel, ""),
      }],
      onPick: (catId) => setSelValue(sel, String(catId)),
    });
  }

  async function saveCategory(sel, opts = {}) {
    const txId = sel.dataset.tx;
    const row = sel.closest("tr");
    const learn = row.querySelector(".learn-chk");
    const pattern = (row.querySelector(".tx-name")?.textContent || "").trim();
    const currentValue = selValue(sel);
    const createRule = !!(learn && learn.checked && currentValue && pattern);

    const wasPending = sel.dataset.pending === "1";
    const prevValue = sel.dataset.prevValue || "";
    const newValue = currentValue;

    const body = new URLSearchParams();
    body.append("category_id", newValue);
    if (createRule) {
      body.append("create_rule", "1");
      body.append("pattern", pattern);
      body.append("field", "name");
      // Only attach a ±5% band when the suggestion came from the recurring-detector
      // path (subscriptions / monthly bills). For everything else a band makes the
      // rule single-shot — real-world amounts vary and the rule never re-fires.
      if (row.dataset.sugRecurring === "1") {
        const rawAmount = parseFloat(row.dataset.amount);
        if (Number.isFinite(rawAmount) && rawAmount > 0) {
          const amt = Math.abs(rawAmount);
          body.append("amount_min", (amt * 0.95).toFixed(2));
          body.append("amount_max", (amt * 1.05).toFixed(2));
        }
      }
    }
    if (opts.source) body.append("source", opts.source);
    // Pass the suggestion's evidence trail so the server can record it on accept
    // and write a correction if the user later changes their mind.
    if (opts.source === "suggestion_accepted") {
      const sigSource = row.getAttribute("data-sug-source");
      const sigKey = row.getAttribute("data-sug-key");
      if (sigSource) body.append("signal_source", sigSource);
      if (sigKey) body.append("signal_key", sigKey);
    }

    setSelDisabled(sel, true);
    try {
      const res = await fetch(`/transactions/${txId}/category`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body.toString(),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "opslaan mislukt");

      flashRow(row, "ok");

      // Adjust uncategorized counter for THIS row's transition
      let delta = 0;
      if (!prevValue && newValue) delta -= 1;
      else if (prevValue && !newValue) delta += 1;

      // Apply rule results in-place — also clears pending state on those rows
      let visiblyUpdated = 0;
      if (createRule && Array.isArray(data.updated_tx_ids) && data.updated_tx_ids.length) {
        for (const otherId of data.updated_tx_ids) {
          const otherRow = document.querySelector(`tr[data-tx="${otherId}"]`);
          if (!otherRow) continue;
          const otherSel = otherRow.querySelector(".cat-select");
          if (!otherSel) continue;
          const wasUncatOther = !otherSel.dataset.prevValue;
          setSelValue(otherSel, newValue, { silent: true });
          otherSel.dataset.prevValue = newValue;
          clearPendingState(otherRow);
          if (wasUncatOther) visiblyUpdated++;
          flashRow(otherRow, "ok");
        }
        delta -= visiblyUpdated;
      }

      adjustUncat(delta);
      sel.dataset.prevValue = newValue;
      if (wasPending) clearPendingState(row);

      if (createRule && data.rule_applied_to > 0) {
        notify(
          `"${pattern}" vastgezet → ${data.category && data.category.name ? data.category.name : "categorie"}. ${data.rule_applied_to} extra transactie${data.rule_applied_to === 1 ? "" : "s"} automatisch getagd.`
        );
      } else if (createRule && newValue) {
        notify(`"${pattern}" vastgezet. Toekomstige overeenkomsten worden automatisch getagd.`);
      }
    } catch (err) {
      setSelValue(sel, prevValue, { silent: true });
      flashRow(row, "err");
      notify("Kon niet opslaan: " + err.message);
    } finally {
      setSelDisabled(sel, false);
    }
  }

  selects.forEach(sel => {
    sel.addEventListener("change", () => saveCategory(sel));
  });

  // Accept button — confirms a suggestion (saves with source='suggestion_accepted')
  document.querySelectorAll(".btn-accept-sug").forEach(btn => {
    btn.addEventListener("click", async () => {
      const row = btn.closest("tr");
      const sel = row?.querySelector(".cat-select");
      if (!sel) return;
      // The Accept path needs the suggested category to be the current value.
      // Pending rows show the suggestion but the underlying value is "" — set it
      // from data-sug-cat before saving.
      const sugId = row.getAttribute("data-sug-cat");
      if (sugId && !selValue(sel)) setSelValue(sel, sugId, { silent: true });
      if (!selValue(sel)) return;
      btn.disabled = true;
      await saveCategory(sel, { source: "suggestion_accepted" });
    });
  });

  // Reject button — records a correction without changing the row's category.
  // Two rejections of the same (signal_source, signal_key, category) suppress
  // that suggestion entirely on future imports.
  document.querySelectorAll(".btn-reject-sug").forEach(btn => {
    btn.addEventListener("click", async () => {
      const row = btn.closest("tr");
      if (!row) return;
      const txId = row.getAttribute("data-tx");
      const suggestedCat = row.getAttribute("data-sug-cat");
      const sigSource = row.getAttribute("data-sug-source");
      const sigKey = row.getAttribute("data-sug-key");
      if (!txId || !suggestedCat || !sigSource || !sigKey) {
        notify("Kan niet afwijzen — metadata van suggestie ontbreekt.");
        return;
      }
      btn.disabled = true;
      const body = new URLSearchParams();
      body.append("suggested_category_id", suggestedCat);
      body.append("signal_source", sigSource);
      body.append("signal_key", sigKey);
      try {
        const res = await fetch(`/transactions/${txId}/reject_suggestion`, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: body.toString(),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "afwijzen mislukt");
        // Reset dropdown to truly uncategorized state
        const sel = row.querySelector(".cat-select");
        if (sel) {
          setSelValue(sel, "", { silent: true });
          sel.dataset.prevValue = "";
        }
        clearPendingState(row);
        flashRow(row, "ok");
        notify("Suggestie afgewezen. Wordt na nog één afwijzing niet meer voorgesteld.");
      } catch (err) {
        flashRow(row, "err");
        notify("Kon niet afwijzen: " + err.message);
        btn.disabled = false;
      }
    });
  });

  function flashRow(row, kind) {
    const color = kind === "ok" ? "rgba(92,110,54,0.18)" : "rgba(142,58,35,0.18)";
    const orig = row.style.background;
    row.style.transition = "background .4s";
    row.style.background = color;
    setTimeout(() => (row.style.background = orig), 600);
  }

  function notify(msg) {
    let host = document.getElementById("toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "toast-host";
      host.style.cssText =
        "position:fixed;bottom:24px;right:24px;display:flex;flex-direction:column;gap:8px;z-index:50;";
      document.body.appendChild(host);
    }
    const el = document.createElement("div");
    el.className = "toast-msg";
    el.textContent = msg;
    host.appendChild(el);
    setTimeout(() => el.remove(), 3500);
  }
})();

// ----- Transactions: hierarchical filter dropdown (sidebar Category filter) -----
// Wraps select[name="category"] with the same topic→category picker as the
// per-row assigning dropdown. The native select stays in the DOM (hidden via
// .cd-native) so onchange="this.form.submit()" keeps working — the picker just
// writes .value and dispatches a change event.
(function () {
  const sels = document.querySelectorAll("select.cat-filter-select, select.cat-rule-select");
  if (!sels.length || !window.__hierPicker) return;
  sels.forEach(enhance);

  function enhance(sel) {
    const isFilter = sel.classList.contains("cat-filter-select");
    const placeholderLabel = isFilter ? "Alle" : "Categorie…";

    // Build the .cd wrapper around the native select. Same DOM shape as the
    // generic cd enhancer so existing .cd / .cd-trigger / .cd-label styles apply.
    const wrap = document.createElement("div");
    wrap.className = "cd";

    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "cd-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");

    const label = document.createElement("span");
    label.className = "cd-label";

    const arrow = document.createElement("span");
    arrow.className = "cd-arrow";
    arrow.setAttribute("aria-hidden", "true");

    trigger.appendChild(label);
    trigger.appendChild(arrow);

    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(trigger);
    wrap.appendChild(sel);
    sel.classList.add("cd-native");
    sel.setAttribute("tabindex", "-1");
    sel.setAttribute("aria-hidden", "true");

    function syncLabel() {
      const opt = sel.options[sel.selectedIndex];
      const txt = opt ? (opt.textContent || "").trim() : "";
      const isEmpty = !opt || opt.value === "";
      label.textContent = isEmpty ? placeholderLabel : txt;
      label.classList.toggle("is-placeholder", isEmpty);
    }
    syncLabel();
    sel.addEventListener("change", syncLabel);

    function pick(v) {
      sel.value = v;
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    }

    trigger.addEventListener("click", (e) => {
      e.stopPropagation();
      if (window.__hierPicker.isOpenFor(trigger)) {
        window.__hierPicker.close();
        return;
      }
      const cur = sel.value;
      // Only numeric category ids are real picker values; "" and "uncategorized"
      // surface as prepend options above the topic list (filter only).
      const prependOptions = isFilter
        ? [
            { label: "Alle", isPlaceholder: true, onPick: () => pick("") },
            { label: "Alleen ongecategoriseerd", color: "#94a3b8", onPick: () => pick("uncategorized") },
          ]
        : [];
      window.__hierPicker.open({
        anchor: trigger,
        currentValue: /^\d+$/.test(cur) ? cur : "",
        prependOptions,
        onPick: (catId) => pick(String(catId)),
      });
    });

    trigger.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        trigger.click();
      }
    });
  }
})();

// ----- Transactions: hover-to-suggest + drag-to-match + unmatch ------------
// UX model: every parent/unmatched row has a faint ⋮⋮ grab handle on hover.
// Hovering anywhere on such a row fetches its best counterpart and makes the
// counterpart row "glow" so it's easy to find. Grab the handle and drag the
// row vertically toward another row — release it on top of the target to
// commit the match. Releasing on empty space (or on a row already in a match)
// snaps the row back without committing. Children render under their parent
// with a small "unmatch" affordance.
(function () {
  const table = document.querySelector(".data-table");
  if (!table) return;

  const HOVER_DEBOUNCE_MS = 140;    // delay before firing the candidate API
  const candidateCache = new Map(); // tx_id -> {candidate} (null if none)

  // ---- helpers ----
  function rowEl(txId) {
    return table.querySelector(`tr[data-tx="${CSS.escape(String(txId))}"]`);
  }
  function clearGlow() {
    table.querySelectorAll(".tx-row.match-glow").forEach(el => el.classList.remove("match-glow"));
  }
  async function fetchCandidate(txId) {
    if (candidateCache.has(txId)) return candidateCache.get(txId);
    try {
      const res = await fetch(`/api/transactions/${txId}/match_candidate`);
      if (!res.ok) return null;
      const data = await res.json();
      const result = data.candidate || null;
      candidateCache.set(txId, result);
      return result;
    } catch {
      return null;
    }
  }
  function invalidateCacheFor(ids) {
    for (const id of ids) candidateCache.delete(String(id));
    // Cached "no candidate" answers for OTHER rows may now be stale, since
    // unmatching frees up rows that other rows could now pair with.
    candidateCache.clear();
  }

  // ---- hover: suggest counterpart by glowing its row ----
  let hoverTimer = null;
  let lastHoveredTx = null;

  table.addEventListener("mouseover", (e) => {
    const tr = e.target.closest("tr.tx-row");
    if (!tr) return;
    if (tr.classList.contains("tx-row-child")) return;       // children don't initiate
    if (tr.classList.contains("tx-row-parent")) return;      // parents are already matched
    const txId = tr.getAttribute("data-tx");
    if (txId === lastHoveredTx) return;
    lastHoveredTx = txId;
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(async () => {
      const cand = await fetchCandidate(txId);
      if (lastHoveredTx !== txId) return;                    // user moved on
      clearGlow();
      if (!cand) return;
      const target = rowEl(cand.id);
      if (target) target.classList.add("match-glow");
    }, HOVER_DEBOUNCE_MS);
  });
  table.addEventListener("mouseleave", () => {
    lastHoveredTx = null;
    clearTimeout(hoverTimer);
    clearGlow();
  });

  // ---- drag: lift a row vertically and drop it onto its match ----
  let dragCtx = null;

  function canBeDragSource(tr) {
    // Only unmatched rows can be picked up. Children, ghosts, and parents
    // already participate in a match — to change them, unmatch first.
    return tr
        && tr.classList.contains("tx-row")
        && !tr.classList.contains("tx-row-child")
        && !tr.classList.contains("tx-row-parent");
        // tx-row-ghost is a subclass of tx-row-child, so covered above.
  }

  function canBeDropTarget(sourceTr, targetTr) {
    // Strict rule: parent = Debit, child = Credit. Opposite-direction only.
    if (!targetTr || targetTr === sourceTr) return false;
    if (!targetTr.classList.contains("tx-row")) return false;
    if (targetTr.classList.contains("tx-row-child")) return false;  // covers ghosts
    const srcDir = sourceTr.getAttribute("data-direction");
    const tgtDir = targetTr.getAttribute("data-direction");
    if (!srcDir || !tgtDir || srcDir === tgtDir) return false;
    // Already-parent rows (always Debit) can receive new Credit siblings —
    // partial-reimbursement model where N credits offset one debit.
    const targetIsParent = targetTr.classList.contains("tx-row-parent");
    if (targetIsParent && srcDir !== "Credit") return false;
    return true;
  }

  function rowUnderPoint(x, y, exclude) {
    // Disable pointer-events on the floating row so hit-testing sees through it
    // to whatever row sits beneath the cursor.
    const prev = exclude.style.pointerEvents;
    exclude.style.pointerEvents = "none";
    const el = document.elementFromPoint(x, y);
    exclude.style.pointerEvents = prev;
    if (!el) return null;
    const tr = el.closest("tr.tx-row");
    return tr === exclude ? null : tr;
  }

  function onPointerDown(e) {
    const handle = e.target.closest(".drag-handle");
    if (!handle) return;
    const tr = handle.closest("tr.tx-row");
    if (!canBeDragSource(tr)) return;
    e.preventDefault();
    const txId = tr.getAttribute("data-tx");
    dragCtx = {
      tr,
      txId,
      startY: e.clientY,
      target: null,
    };
    tr.classList.add("is-dragging");
    handle.setPointerCapture?.(e.pointerId);

    // Keep the suggestion glow up during the drag — it's just a hint; the
    // user can drop wherever they want.
    fetchCandidate(txId).then((cand) => {
      if (!dragCtx || dragCtx.txId !== txId) return;
      clearGlow();
      if (cand) {
        const target = rowEl(cand.id);
        if (target) target.classList.add("match-glow");
      }
    });
  }

  function onPointerMove(e) {
    if (!dragCtx) return;
    const dy = e.clientY - dragCtx.startY;
    dragCtx.tr.style.transform = `translateY(${dy}px)`;

    const beneath = rowUnderPoint(e.clientX, e.clientY, dragCtx.tr);
    const newTarget = canBeDropTarget(dragCtx.tr, beneath) ? beneath : null;
    if (newTarget !== dragCtx.target) {
      if (dragCtx.target) dragCtx.target.classList.remove("drop-target");
      if (newTarget) newTarget.classList.add("drop-target");
      dragCtx.target = newTarget;
    }
  }

  async function onPointerUp() {
    if (!dragCtx) return;
    const { tr, txId, target } = dragCtx;
    dragCtx = null;

    // Snap-back animation regardless of outcome — a committed match reloads
    // the page right after, so the snap-back is only visible on cancellations.
    tr.style.transition = "transform .25s cubic-bezier(.2,.7,.2,1)";
    tr.style.transform = "translateY(0)";
    setTimeout(() => {
      tr.style.transition = "";
      tr.classList.remove("is-dragging");
    }, 260);
    if (target) target.classList.remove("drop-target");

    if (!target) {
      clearGlow();
      return;
    }
    // Commit the match — backend picks parent/child by direction + date.
    const targetId = target.getAttribute("data-tx");
    try {
      const body = new URLSearchParams({ a_id: txId, b_id: targetId });
      const res = await fetch("/transactions/match", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body.toString(),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "koppelen mislukt");
      window.location.reload();
    } catch (err) {
      clearGlow();
      notifyMatch("Kon niet koppelen: " + err.message);
    }
  }

  // Pointer events bind on the document so a drag survives leaving the row.
  table.addEventListener("pointerdown", onPointerDown);
  document.addEventListener("pointermove", onPointerMove);
  document.addEventListener("pointerup", onPointerUp);
  document.addEventListener("pointercancel", onPointerUp);

  // ---- unmatch button ----
  table.addEventListener("click", async (e) => {
    const btn = e.target.closest(".btn-unmatch");
    if (!btn) return;
    e.preventDefault();
    const txId = btn.getAttribute("data-tx");
    btn.disabled = true;
    try {
      const res = await fetch(`/transactions/${txId}/unmatch`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "ontkoppelen mislukt");
      invalidateCacheFor(data.detached_ids || [txId]);
      window.location.reload();
    } catch (err) {
      btn.disabled = false;
      notifyMatch("Kon niet ontkoppelen: " + err.message);
    }
  });

  function notifyMatch(msg) {
    let host = document.getElementById("toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "toast-host";
      host.style.cssText =
        "position:fixed;bottom:24px;right:24px;display:flex;flex-direction:column;gap:8px;z-index:50;";
      document.body.appendChild(host);
    }
    const el = document.createElement("div");
    el.className = "toast-msg";
    el.textContent = msg;
    host.appendChild(el);
    setTimeout(() => el.remove(), 3500);
  }
})();

// ----- Debounced auto-submit on inputs marked data-auto-submit -----
// GET forms trigger a full page reload, which would otherwise blow away
// focus and the caret position mid-typing. We stash both before submit
// and restore them on the next page load.
(function () {
  const STORAGE_KEY = "autoSubmitFocus";

  // Restore focus from the previous submit, if any.
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (raw) {
      sessionStorage.removeItem(STORAGE_KEY);
      const saved = JSON.parse(raw);
      if (saved && saved.path === location.pathname && saved.name) {
        const target = document.querySelector(
          `input[data-auto-submit][name="${CSS.escape(saved.name)}"]`
        );
        if (target) {
          target.focus({ preventScroll: true });
          const end = target.value.length;
          const s = Math.min(saved.selStart ?? end, end);
          const e = Math.min(saved.selEnd ?? end, end);
          try { target.setSelectionRange(s, e); } catch (_) { /* unsupported input type */ }
        }
      }
    }
  } catch (_) { /* sessionStorage unavailable or JSON garbage — ignore */ }

  const inputs = document.querySelectorAll("input[data-auto-submit]");
  inputs.forEach(input => {
    let timer;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      const ms = parseInt(input.dataset.autoSubmit, 10) || 400;
      timer = setTimeout(() => {
        if (!input.form) return;
        try {
          sessionStorage.setItem(STORAGE_KEY, JSON.stringify({
            path: location.pathname,
            name: input.name,
            selStart: input.selectionStart,
            selEnd: input.selectionEnd,
          }));
        } catch (_) { /* private mode / quota — focus restore just won't happen */ }
        input.form.submit();
      }, ms);
    });
  });
})();

// ----- Page-wide drag & drop CSV upload -----
(function () {
  const overlay = document.getElementById("page-dropzone");
  if (!overlay) return;

  let dragDepth = 0;

  function hasFiles(e) {
    if (!e.dataTransfer) return false;
    const types = Array.from(e.dataTransfer.types || []);
    return types.includes("Files");
  }

  function show() { overlay.classList.add("active"); }
  function hide() { overlay.classList.remove("active"); dragDepth = 0; }

  document.addEventListener("dragenter", (e) => {
    if (!hasFiles(e)) return;
    dragDepth++;
    show();
  });

  document.addEventListener("dragleave", (e) => {
    if (!hasFiles(e)) return;
    dragDepth--;
    if (dragDepth <= 0) hide();
  });

  document.addEventListener("dragover", (e) => {
    if (hasFiles(e)) e.preventDefault();
  });

  document.addEventListener("drop", async (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    hide();

    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (!file) return;
    if (!/\.csv$/i.test(file.name)) {
      alert("Sleep een CSV-bestand.");
      return;
    }

    const fd = new FormData();
    fd.append("file", file);
    try {
      const resp = await fetch("/upload", { method: "POST", body: fd, redirect: "follow" });
      window.location.href = resp.redirected ? resp.url : "/transactions";
    } catch (err) {
      alert("Uploaden mislukt: " + (err && err.message ? err.message : err));
    }
  });

  // If the user drags off the window without dropping, clear the overlay
  window.addEventListener("blur", hide);
})();

// ----- Rules page: preview button -----
(function () {
  const btn = document.querySelector(".rule-preview-btn");
  if (!btn) return;
  const form = btn.closest("form");
  if (!form) return;
  const result = form.querySelector(".rule-preview-result");

  btn.addEventListener("click", async () => {
    const pattern = (form.querySelector("[name=pattern]")?.value || "").trim();
    const field = form.querySelector("[name=field]")?.value || "name";
    const wb = form.querySelector("[name=word_boundary]:checked");
    const amountMin = (form.querySelector("[name=amount_min]")?.value || "").trim();
    const amountMax = (form.querySelector("[name=amount_max]")?.value || "").trim();

    if (!pattern) {
      showResult("Voer eerst een patroon in.", "over-broad");
      return;
    }

    const body = new URLSearchParams();
    body.append("pattern", pattern);
    body.append("field", field);
    if (wb && wb.value === "1") body.append("word_boundary", "1");
    if (amountMin) body.append("amount_min", amountMin);
    if (amountMax) body.append("amount_max", amountMax);

    btn.disabled = true;
    try {
      const res = await fetch("/api/rules/preview", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body.toString(),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "voorbeeld mislukt");
      renderPreview(data);
    } catch (err) {
      showResult("Voorbeeld mislukt: " + err.message, "over-broad");
    } finally {
      btn.disabled = false;
    }
  });

  function showResult(html, cls) {
    result.hidden = false;
    result.className = "rule-preview-result" + (cls ? " " + cls : "");
    result.innerHTML = html;
  }

  function renderPreview(data) {
    const total = data.count || 0;
    if (total === 0) {
      showResult("Geen transacties komen overeen met dit patroon.");
      return;
    }
    const cls = total > 50 ? "over-broad" : "";
    let html = `Komt overeen met <strong>${total}</strong> transactie${total === 1 ? "" : "s"} `
             + `· <strong>${data.uncategorized_count}</strong> ongecategoriseerd`
             + `, ${data.already_categorized_count} al gecategoriseerd.`;
    if (cls) {
      html += ` <em>Dit lijkt erg breed — bekijk de voorbeelden zorgvuldig.</em>`;
    }
    if (Array.isArray(data.samples) && data.samples.length) {
      html += '<div class="preview-samples">';
      for (const s of data.samples) {
        const sign = s.direction === "Debit" ? "−" : "+";
        const amt = s.amount != null
          ? sign + "€ " + Number(s.amount).toFixed(2).replace(".", ",")
          : "";
        const rowCls = s.is_categorized ? "preview-cat" : "preview-uncat";
        html += `<div class="preview-sample ${rowCls}">`
              +   `<span class="preview-date">${escapeHtml(s.date)}</span>`
              +   `<span>${escapeHtml(s.name)}</span>`
              +   `<span class="preview-amt">${amt}</span>`
              + `</div>`;
      }
      html += "</div>";
    }
    showResult(html, cls);
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }
})();

// ----- Custom dropdown — replaces the visible chrome of every <select>. ----
// Progressive enhancement: the native <select> stays in the DOM so existing
// change handlers (inline onchange="this.form.submit()", category save,
// validation) keep firing. The popup is just a styled overlay.
(function () {
  const selects = document.querySelectorAll("select:not([data-cd-skip])");
  if (!selects.length) return;

  const valueDesc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");
  let openCtx = null;

  function closeOpen() { if (openCtx) openCtx.close(); }

  document.addEventListener("mousedown", (e) => {
    if (openCtx && !openCtx.wrap.contains(e.target) && !openCtx.popup.contains(e.target)) {
      openCtx.close();
    }
  });
  window.addEventListener("scroll", (e) => {
    if (!openCtx) return;
    // Ignore scrolls happening inside the popup itself
    if (openCtx.popup && (e.target === openCtx.popup || openCtx.popup.contains(e.target))) return;
    closeOpen();
  }, true);
  window.addEventListener("resize", closeOpen);

  selects.forEach(enhance);

  function enhance(sel) {
    const wrap = document.createElement("div");
    wrap.className = "cd";

    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "cd-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    if (sel.title) trigger.title = sel.title;

    const label = document.createElement("span");
    label.className = "cd-label";

    const arrow = document.createElement("span");
    arrow.className = "cd-arrow";
    arrow.setAttribute("aria-hidden", "true");

    trigger.appendChild(label);
    trigger.appendChild(arrow);

    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(trigger);
    wrap.appendChild(sel);
    sel.classList.add("cd-native");
    sel.setAttribute("tabindex", "-1");
    sel.setAttribute("aria-hidden", "true");

    let popup = null;
    let activeIdx = -1;
    let typeahead = "";
    let typeaheadTimer = null;

    const ctx = { wrap, get popup() { return popup; }, close };

    syncFromSelect();

    new MutationObserver(syncFromSelect)
      .observe(sel, { attributes: true, attributeFilter: ["disabled", "style", "class"] });

    // Intercept programmatic .value writes on this instance so we re-sync.
    // Other code (rule application, suggestion reject) does `sel.value = "..."`
    // without dispatching change — without this, the trigger label would drift.
    Object.defineProperty(sel, "value", {
      configurable: true,
      get() { return valueDesc.get.call(this); },
      set(v) { valueDesc.set.call(this, v); syncFromSelect(); },
    });

    sel.addEventListener("change", syncFromSelect);

    trigger.addEventListener("click", (e) => {
      e.stopPropagation();
      if (sel.disabled) return;
      popup ? close() : open();
    });

    trigger.addEventListener("keydown", (e) => {
      if (sel.disabled) return;
      if (!popup) {
        if (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open();
        }
        return;
      }
      if (e.key === "ArrowDown")      { e.preventDefault(); moveActive(1); }
      else if (e.key === "ArrowUp")   { e.preventDefault(); moveActive(-1); }
      else if (e.key === "Home")      { e.preventDefault(); activeIdx = 0; highlightActive(); }
      else if (e.key === "End")       { e.preventDefault(); activeIdx = sel.options.length - 1; highlightActive(); }
      else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(activeIdx); }
      else if (e.key === "Escape")    { e.preventDefault(); close(); }
      else if (e.key === "Tab")       { close(); }
      else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
        typeahead += e.key.toLowerCase();
        clearTimeout(typeaheadTimer);
        typeaheadTimer = setTimeout(() => { typeahead = ""; }, 500);
        for (let k = 1; k <= sel.options.length; k++) {
          const idx = (activeIdx + k) % sel.options.length;
          const opt = sel.options[idx];
          if (opt.disabled) continue;
          if ((opt.textContent || "").trim().toLowerCase().startsWith(typeahead)) {
            activeIdx = idx;
            highlightActive();
            break;
          }
        }
      }
    });

    function syncFromSelect() {
      const opt = sel.options[sel.selectedIndex];
      const txt = opt ? (opt.textContent || "").trim() : "";
      label.textContent = txt;
      label.classList.toggle("is-placeholder", !!opt && opt.value === "");

      wrap.classList.toggle("is-disabled", sel.disabled);
      trigger.disabled = sel.disabled;
      trigger.tabIndex = sel.disabled ? -1 : 0;

      // Mirror inline color hints (cat-select pending suggestion uses these)
      trigger.style.backgroundColor   = sel.style.backgroundColor   || "";
      trigger.style.color             = sel.style.color             || "";
      trigger.style.borderBottomColor = sel.style.borderColor       || "";

      trigger.classList.toggle("cd-trigger-pending",
        sel.classList.contains("cat-select-pending"));

      if (popup) buildOptions();
    }

    function open() {
      if (sel.disabled) return;
      closeOpen();
      popup = document.createElement("div");
      popup.className = "cd-popup";
      popup.setAttribute("role", "listbox");
      buildOptions();
      document.body.appendChild(popup);
      position();
      wrap.classList.add("is-open");
      trigger.setAttribute("aria-expanded", "true");
      activeIdx = sel.selectedIndex >= 0 ? sel.selectedIndex : 0;
      highlightActive();
      const activeEl = popup.children[activeIdx];
      if (activeEl) activeEl.scrollIntoView({ block: "nearest" });
      openCtx = ctx;
    }

    function close() {
      if (!popup) return;
      popup.remove();
      popup = null;
      wrap.classList.remove("is-open");
      trigger.setAttribute("aria-expanded", "false");
      if (openCtx === ctx) openCtx = null;
    }

    function position() {
      const r = trigger.getBoundingClientRect();
      const vh = window.innerHeight;
      popup.style.width = (r.width + 14) + "px";
      popup.style.left = r.left + "px";
      const popupH = Math.min(280, popup.scrollHeight);
      const spaceBelow = vh - r.bottom;
      if (spaceBelow < popupH + 12 && r.top > popupH + 12) {
        popup.style.top = (r.top - popupH - 4) + "px";
      } else {
        popup.style.top = (r.bottom + 4) + "px";
      }
    }

    function buildOptions() {
      popup.innerHTML = "";
      Array.from(sel.options).forEach((opt, i) => {
        const item = document.createElement("div");
        item.className = "cd-option";
        item.setAttribute("role", "option");
        if (opt.disabled) item.classList.add("is-disabled");
        if (i === sel.selectedIndex) item.classList.add("is-selected");
        const txt = (opt.textContent || "").trim();
        if (opt.value === "") item.classList.add("is-placeholder");

        if (opt.dataset.color) {
          const dot = document.createElement("span");
          dot.className = "cd-dot";
          dot.style.background = opt.dataset.color;
          item.appendChild(dot);
        }
        const txtSpan = document.createElement("span");
        txtSpan.textContent = txt;
        item.appendChild(txtSpan);

        item.addEventListener("mouseenter", () => {
          if (opt.disabled) return;
          activeIdx = i;
          highlightActive();
        });
        item.addEventListener("mousedown", (e) => e.preventDefault());
        item.addEventListener("click", (e) => {
          e.stopPropagation();
          if (opt.disabled) return;
          choose(i);
        });
        popup.appendChild(item);
      });
    }

    function highlightActive() {
      Array.from(popup.children).forEach((el, i) => {
        el.classList.toggle("is-active", i === activeIdx);
      });
      const el = popup.children[activeIdx];
      if (el) el.scrollIntoView({ block: "nearest" });
    }

    function moveActive(delta) {
      const n = sel.options.length;
      if (!n) return;
      let i = activeIdx;
      for (let step = 0; step < n; step++) {
        i = (i + delta + n) % n;
        if (!sel.options[i].disabled) break;
      }
      activeIdx = i;
      highlightActive();
    }

    function choose(i) {
      if (i < 0 || i >= sel.options.length) return;
      const opt = sel.options[i];
      if (opt.disabled) return;
      const changed = sel.selectedIndex !== i;
      sel.selectedIndex = i;
      if (changed) sel.dispatchEvent(new Event("change", { bubbles: true }));
      syncFromSelect();
      close();
      if (!trigger.disabled) trigger.focus();
    }
  }
})();

// ----- Page-load animations: tile-value count-up + bar-fill grow-in --------
(function () {
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduceMotion) return;

  // Parse a rendered value back to a number. Handles three formats:
  //   "€ 1.234,56" / "€ -100,00"  (Dutch euros from the server's eur filter)
  //   "25%"                       (percentage)
  //   "5"                         (plain integer)
  function parseValue(text) {
    const t = (text || "").trim();
    if (!t || t === "—") return null;
    if (t.endsWith("%")) {
      const n = parseFloat(t.slice(0, -1).trim().replace(",", "."));
      return Number.isFinite(n) ? n : null;
    }
    if (t.includes("€")) {
      const cleaned = t.replace(/[€\s]/g, "").replace("−", "-");
      const num = cleaned.replace(/\./g, "").replace(",", ".");
      const n = parseFloat(num);
      return Number.isFinite(n) ? n : null;
    }
    const n = parseFloat(t.replace(/\./g, "").replace(",", "."));
    return Number.isFinite(n) ? n : null;
  }

  // Build a formatter that mimics the input string's shape.
  function makeFormatter(sample) {
    if (sample.endsWith("%")) {
      return (v) => `${Math.round(v)}%`;
    }
    if (sample.includes("€")) {
      return (v) => {
        const sign = v < 0 ? "-" : "";
        const abs = Math.abs(v);
        const s = abs.toFixed(2);
        const [intPart, decPart] = s.split(".");
        const withThou = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ".");
        return `€ ${sign}${withThou},${decPart}`;
      };
    }
    return (v) => Math.round(v).toString();
  }

  function animateNumber(el, target, formatter, duration, delay) {
    el.textContent = formatter(0);
    const start = performance.now() + delay;
    function tick(now) {
      if (now < start) { requestAnimationFrame(tick); return; }
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3); // ease-out cubic
      el.textContent = formatter(target * eased);
      if (t < 1) requestAnimationFrame(tick);
      else el.textContent = formatter(target);
    }
    requestAnimationFrame(tick);
  }

  function start() {
    // Tile values — count from 0 to their rendered number
    const tiles = document.querySelectorAll(".tile-value");
    tiles.forEach((el, i) => {
      // Skip tiles whose content is just a placeholder dash or contains markup
      // (the largest-tx tile has nested text we shouldn't rewrite).
      if (el.children.length > 0) return;
      const original = el.textContent.trim();
      const num = parseValue(original);
      if (num == null) return;
      const formatter = makeFormatter(original);
      animateNumber(el, num, formatter, 1500, i * 140);
    });

    // Bar fills — grow from 0 to their rendered width with a small stagger
    const bars = document.querySelectorAll(".bar-fill");
    bars.forEach((el, i) => {
      const target = el.style.width;
      if (!target || target === "0%" || target === "0") return;
      el.style.width = "0%";
      // Two rAFs ensure the browser commits width:0 before the transition target
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          setTimeout(() => { el.style.width = target; }, 180 + i * 90);
        });
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();

// Right-click context menu on transaction rows — jump to the Rules page with
// the selected field's value prefilled into the pattern input and the field
// dropdown set, so the user only needs to pick a category.
(function () {
  const FIELDS = [
    { key: "name", label: "Naam naar Regel" },
    { key: "counterparty", label: "Tegenpartij naar Regel" },
    { key: "notifications", label: "Notificatie naar Regel" },
  ];

  let menu = null;
  let currentRow = null;

  function ensureMenu() {
    if (menu) return menu;
    menu = document.createElement("div");
    menu.className = "tx-ctx-menu";
    menu.setAttribute("role", "menu");
    menu.hidden = true;
    for (const f of FIELDS) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tx-ctx-item";
      btn.dataset.field = f.key;
      btn.setAttribute("role", "menuitem");
      btn.textContent = f.label;
      menu.appendChild(btn);
    }
    document.body.appendChild(menu);
    menu.addEventListener("click", onMenuClick);
    return menu;
  }

  function hide() {
    if (!menu || menu.hidden) return;
    menu.hidden = true;
    currentRow = null;
  }

  function syncItems(row) {
    for (const btn of menu.querySelectorAll(".tx-ctx-item")) {
      const val = (row.getAttribute("data-" + btn.dataset.field) || "").trim();
      btn.disabled = !val;
      btn.classList.remove("is-copied");
      btn.textContent = FIELDS.find(f => f.key === btn.dataset.field).label;
    }
  }

  function positionMenu(x, y) {
    menu.hidden = false;
    const rect = menu.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const left = (x + rect.width > vw - 8) ? Math.max(8, vw - rect.width - 8) : x;
    const top = (y + rect.height > vh - 8) ? Math.max(8, vh - rect.height - 8) : y;
    menu.style.left = left + "px";
    menu.style.top = top + "px";
  }

  function onMenuClick(ev) {
    const btn = ev.target.closest(".tx-ctx-item");
    if (!btn || btn.disabled || !currentRow) return;
    const field = btn.dataset.field;
    const value = (currentRow.getAttribute("data-" + field) || "").trim();
    if (!value) return;
    const url = `/rules?pattern=${encodeURIComponent(value)}&field=${encodeURIComponent(field)}`;
    window.location.href = url;
  }

  document.addEventListener("contextmenu", (ev) => {
    const row = ev.target.closest(".tx-row");
    if (!row) { hide(); return; }
    // Let form controls keep their native context menu so users can paste
    // into the category picker without surprises.
    const tag = (ev.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea" || tag === "option") return;
    if (ev.target.closest(".cell-cat, .cd-popup, .cd-trigger")) return;
    ev.preventDefault();
    ensureMenu();
    currentRow = row;
    syncItems(row);
    positionMenu(ev.clientX, ev.clientY);
  });

  document.addEventListener("click", (ev) => {
    if (!menu || menu.hidden) return;
    if (ev.target.closest(".tx-ctx-menu")) return;
    hide();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") hide();
  });
  window.addEventListener("scroll", hide, true);
  window.addEventListener("resize", hide);
})();
