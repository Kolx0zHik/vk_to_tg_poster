document.addEventListener("DOMContentLoaded", () => {
    const state = {
        config: null,
        communities: [],
        avatarCache: {},
        selected: 0,
        query: "",
        addScope: "none",
        addAmount: 0,
        addTypes: {},
        modelOptions: [],
        modelFetchKey: "",
        modelFetchError: "",
        modelFetchInFlight: false,
        modelFetchId: 0,
        logsTab: "journal",
        logLevel: "",
        logQuery: "",
        logEntries: [],
        logAutoTimer: null,
    };

    const els = {
        interval: document.getElementById("interval"),
        cronCustomRow: document.getElementById("cronCustomRow"),
        cronCustom: document.getElementById("cronCustom"),
        filterKeywords: document.getElementById("filterKeywords"),
        refreshAvatars: document.getElementById("refreshAvatars"),
        postsCount: document.getElementById("postsCount"),
        logRetention: document.getElementById("logRetention"),
        timezone: document.getElementById("timezone"),
        blockedKeywords: document.getElementById("blockedKeywords"),
        tgChannel: document.getElementById("tgChannel"),
        saveSettingsBtn: document.getElementById("saveSettingsBtn"),

        aiModal: document.getElementById("aiModal"),
        openAiBtn: document.getElementById("openAiBtn"),
        closeAiBtn: document.getElementById("closeAiBtn"),
        saveAiBtn: document.getElementById("saveAiBtn"),
        aiEnabled: document.getElementById("aiEnabled"),
        aiBaseUrl: document.getElementById("aiBaseUrl"),
        aiModel: document.getElementById("aiModel"),
        aiModelCombobox: document.getElementById("aiModelCombobox"),
        aiModelList: document.getElementById("aiModelList"),
        aiWindow: document.getElementById("aiWindow"),
        aiPrompt: document.getElementById("aiPrompt"),

        newGroupInput: document.getElementById("newGroupInput"),
        addGroupBtn: document.getElementById("addGroupBtn"),
        addGroupToggle: document.getElementById("addGroupToggle"),
        addGroupModal: document.getElementById("addGroupModal"),
        addGroupScope: document.getElementById("addGroupScope"),
        addGroupAmountChips: document.getElementById("addGroupAmountChips"),
        addGroupTypes: document.getElementById("addGroupTypes"),
        addGroupPreview: document.getElementById("addGroupPreview"),
        cancelAddGroupBtn: document.getElementById("cancelAddGroupBtn"),
        groupSearch: document.getElementById("groupSearch"),
        groupsList: document.getElementById("groupsList"),
        groupDetail: document.getElementById("groupDetail"),
        groupsSummary: document.getElementById("groupsSummary"),

        logsContainer: document.getElementById("logsContainer"),
        logsModal: document.getElementById("logsModal"),
        openLogsBtn: document.getElementById("openLogsBtn"),
        closeLogsBtn: document.getElementById("closeLogsBtn"),
        refreshLogsBtn: document.getElementById("refreshLogsBtn"),
        journalContainer: document.getElementById("journalContainer"),
        journalPanel: document.getElementById("journalPanel"),
        logPanel: document.getElementById("logPanel"),
        logsTabs: document.querySelectorAll("[data-logs-tab]"),
        logLevelFilters: document.getElementById("logLevelFilters"),
        logSearch: document.getElementById("logSearch"),
        logAutoRefresh: document.getElementById("logAutoRefresh"),
        runStatus: document.getElementById("runStatus"),

        projectVersion: document.getElementById("projectVersion"),

        removeGroupModal: document.getElementById("removeGroupModal"),
        removeGroupName: document.getElementById("removeGroupName"),
        cancelRemoveGroupBtn: document.getElementById("cancelRemoveGroupBtn"),
        confirmRemoveGroupBtn: document.getElementById("confirmRemoveGroupBtn"),

        toast: document.getElementById("toast"),
        toastMessage: document.getElementById("toastMessage"),
    };

    const cronMap = {
        "5": "*/5 * * * *",
        "10": "*/10 * * * *",
        "30": "*/30 * * * *",
        "60": "0 * * * *",
    };

    const reverseCronMap = {
        "*/5 * * * *": "5",
        "*/10 * * * *": "10",
        "*/30 * * * *": "30",
        "0 * * * *": "60",
    };

    function showToast(message, isError = false) {
        if (!els.toast || !els.toastMessage) {
            return;
        }
        els.toastMessage.textContent = message;
        els.toast.classList.remove("hidden");
        els.toast.style.borderColor = isError ? "#fecaca" : "#e2e8f0";
        setTimeout(() => {
            els.toast.classList.add("hidden");
        }, 3000);
    }

    // FastAPI отдаёт то строку (HTTPException), то список (валидация Pydantic 422).
    function apiErrorMessage(detail, fallback) {
        if (typeof detail === "string" && detail) {
            return detail;
        }
        if (detail && typeof detail === "object" && !Array.isArray(detail)) {
            return detail.message || fallback;
        }
        if (Array.isArray(detail) && detail.length > 0) {
            const first = detail[0] || {};
            const message = String(first.msg || "").replace(/^Value error,\s*/i, "").trim();
            if (message) {
                return message;
            }
        }
        return fallback;
    }

    function closeModelList() {
        if (!els.aiModelList) return;
        els.aiModelList.classList.add("hidden");
        if (els.aiModel) els.aiModel.setAttribute("aria-expanded", "false");
    }

    function resetModelList() {
        state.modelOptions = [];
        state.modelFetchKey = "";
        state.modelFetchError = "";
        state.modelFetchInFlight = false;
        state.modelFetchId += 1;
        closeModelList();
    }

    function selectModel(model) {
        if (!els.aiModel) return;
        els.aiModel.value = model;
        closeModelList();
        els.aiModel.focus();
    }

    function renderModelList() {
        const list = els.aiModelList;
        if (!list || list.classList.contains("hidden")) return;
        const query = els.aiModel.value.trim().toLowerCase();
        let html = "";
        if (state.modelFetchInFlight) {
            html = '<div class="combobox-empty">Загружаем список моделей…</div>';
        } else if (state.modelFetchError) {
            html = `<div class="combobox-empty">${escapeHtml(state.modelFetchError)}</div>`;
        } else if (!state.modelOptions.length) {
            html = '<div class="combobox-empty">Список моделей пуст</div>';
        } else {
            const options = state.modelOptions.filter((model) => !query || model.toLowerCase().includes(query));
            if (!options.length) {
                html = '<div class="combobox-empty">Ничего не найдено</div>';
            } else {
                html = options
                    .map(
                        (model) =>
                            `<div class="combobox-option${model.toLowerCase() === query ? " sel" : ""}" data-model="${escapeHtml(model)}">${escapeHtml(model)}</div>`,
                    )
                    .join("");
            }
        }
        list.innerHTML = html;
    }

    function openModelList() {
        if (!els.aiModelList) return;
        els.aiModelList.classList.remove("hidden");
        els.aiModel.setAttribute("aria-expanded", "true");
        renderModelList();
    }

    async function fetchModels(force = false) {
        if (!els.aiBaseUrl || !els.aiModel || !els.aiModelList) return;
        const baseUrl = els.aiBaseUrl.value.trim();
        const key = baseUrl.replace(/\/+$/, "");
        if (!baseUrl) {
            state.modelOptions = [];
            state.modelFetchKey = "";
            state.modelFetchError = "Сначала укажите Base URL";
            state.modelFetchInFlight = false;
            state.modelFetchId += 1;
            openModelList();
            return;
        }
        if (state.modelFetchInFlight && state.modelFetchKey === key) {
            openModelList();
            return;
        }
        if (!force && state.modelFetchKey === key && state.modelOptions.length) {
            openModelList();
            return;
        }
        const requestId = ++state.modelFetchId;
        state.modelFetchKey = key;
        state.modelFetchError = "";
        state.modelOptions = [];
        state.modelFetchInFlight = true;
        openModelList();
        try {
            const res = await fetch(`/api/llm/models?base_url=${encodeURIComponent(baseUrl)}`);
            const data = await res.json().catch(() => ({}));
            if (requestId !== state.modelFetchId) return;
            if (!res.ok) {
                throw new Error(apiErrorMessage(data?.detail, "Не удалось загрузить список моделей"));
            }
            if (els.aiBaseUrl.value.trim().replace(/\/+$/, "") !== key) return;
            state.modelOptions = Array.isArray(data.models) ? data.models.map(String).filter(Boolean) : [];
        } catch (err) {
            if (requestId === state.modelFetchId && els.aiBaseUrl.value.trim().replace(/\/+$/, "") === key) {
                state.modelFetchError = err.message || "Не удалось загрузить список моделей";
            }
        } finally {
            if (requestId === state.modelFetchId) {
                state.modelFetchInFlight = false;
                renderModelList();
            }
        }
    }

    function cronFromUI() {
        const value = els.interval.value;
        if (value === "custom") {
            return els.cronCustom.value.trim();
        }
        return cronMap[value] || "*/10 * * * *";
    }

    function updateCronUI(cronValue) {
        const preset = reverseCronMap[cronValue] || "custom";
        els.interval.value = preset;
        if (preset === "custom") {
            els.cronCustomRow.classList.remove("hidden");
            els.cronCustom.value = cronValue;
        } else {
            els.cronCustomRow.classList.add("hidden");
            els.cronCustom.value = cronValue;
        }
    }

    const TYPE_META = [
        { key: "text", label: "Текст", icon: '<path d="M4 7V4h16v3"/><path d="M9 20h6"/><path d="M12 4v16"/>' },
        { key: "photo", label: "Фото", icon: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/>' },
        { key: "video", label: "Видео", icon: '<path d="m22 8-6 4 6 4V8Z"/><rect x="2" y="6" width="14" height="12" rx="2"/>' },
        { key: "audio", label: "Аудио", icon: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>' },
        { key: "link", label: "Ссылка", icon: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>' },
    ];

    const ICONS = {
        check: '<path d="M20 6 9 17l-5-5"/>',
        pause: '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
        trash: '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>',
        external: '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6"/><path d="M10 14 21 3"/>',
    };

    function svgIcon(paths) {
        return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
    }

    function escapeHtml(value) {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function plural(count, one, few, many) {
        const mod10 = count % 10;
        const mod100 = count % 100;
        if (mod10 === 1 && mod100 !== 11) return one;
        if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
        return many;
    }

    function initials(group) {
        const source = (group.name || group.id || "VK").trim();
        return source.slice(0, 2).toUpperCase();
    }

    function vkCommunityUrl(value) {
        const raw = String(value || "").trim();
        if (!raw) return "";
        if (/^https?:\/\//i.test(raw)) return raw;
        if (/^-?\d+$/.test(raw)) {
            return raw.startsWith("-") ? `https://vk.com/club${raw.slice(1)}` : `https://vk.com/id${raw}`;
        }
        return `https://vk.com/${raw.replace(/^@/, "")}`;
    }

    function applyAvatar(el, group) {
        if (!el || !group.icon) return;
        const img = document.createElement("img");
        img.src = group.icon;
        img.alt = "";
        el.insertBefore(img, el.firstChild);
    }

    function visibleGroups() {
        const query = state.query.trim().toLowerCase();
        return state.communities
            .map((group, index) => ({ group, index }))
            .filter(({ group }) => {
                if (!query) return true;
                return `${group.name || ""} ${group.id || ""}`.toLowerCase().includes(query);
            });
    }

    function renderSummary() {
        if (!els.groupsSummary) return;
        const total = state.communities.length;
        const paused = state.communities.filter((group) => !group.active).length;
        const parts = [`${total} ${plural(total, "сообщество", "сообщества", "сообществ")}`];
        if (paused) {
            parts.push(`${paused} на паузе`);
        }
        els.groupsSummary.textContent = parts.join(" · ");
    }

    function renderList() {
        if (!state.communities.length) {
            els.groupsList.innerHTML = '<div class="empty-state">Список групп пуст. Добавьте первую группу для начала работы.</div>';
            return;
        }
        const items = visibleGroups();
        if (!items.length) {
            els.groupsList.innerHTML = '<div class="empty-state">Ничего не найдено</div>';
            return;
        }
        els.groupsList.innerHTML = items
            .map(
                ({ group, index }) => `
                <div class="md-item${index === state.selected ? " sel" : ""}${group.active ? "" : " paused"}" data-index="${index}">
                    <div class="avatar sm" data-avatar="${index}">${escapeHtml(initials(group))}<span class="dot${group.active ? "" : " paused"}"></span></div>
                    <div class="md-item-text"><div class="name">${escapeHtml(group.name || group.id || "Без названия")}</div></div>
                </div>
            `,
            )
            .join("");
        items.forEach(({ group, index }) => {
            applyAvatar(els.groupsList.querySelector(`[data-avatar="${index}"]`), group);
        });
    }

    function renderDetail() {
        const group = state.communities[state.selected];
        if (!group) {
            els.groupDetail.innerHTML = '<div class="empty-state">Выберите сообщество из списка</div>';
            return;
        }
        const url = vkCommunityUrl(group.id);
        const displayName = escapeHtml(group.name || group.id || "Без названия");
        const heading = url
            ? `<h3><a href="${escapeHtml(url)}" target="_blank" rel="noopener">${displayName}<span class="ext">${svgIcon(ICONS.external)}</span></a></h3>`
            : `<h3>${displayName}</h3>`;
        els.groupDetail.innerHTML = `
            <div class="md-detail-head">
                <div class="avatar lg" data-avatar-detail>${escapeHtml(initials(group))}<span class="dot${group.active ? "" : " paused"}"></span></div>
                <div>${heading}</div>
            </div>
            <div class="md-block">
                <label>Статус</label>
                <div class="seg">
                    <button type="button" data-active="1" class="${group.active ? "on" : ""}">${svgIcon(ICONS.check)}Активно</button>
                    <button type="button" data-active="0" class="${group.active ? "" : "on warn"}">${svgIcon(ICONS.pause)}На паузе</button>
                </div>
            </div>
            <div class="md-block">
                <label>Типы контента</label>
                <div class="itog">
                    ${TYPE_META.map(
                        (type) => `
                        <button type="button" class="tg${group.content_types?.[type.key] ? " on" : ""}" data-type="${type.key}">
                            ${svgIcon(type.icon)}${type.label}
                        </button>`,
                    ).join("")}
                </div>
            </div>
            <div class="md-detail-foot">
                <button type="button" class="link-danger" data-action="remove">${svgIcon(ICONS.trash)}Удалить сообщество</button>
            </div>
        `;
        applyAvatar(els.groupDetail.querySelector("[data-avatar-detail]"), group);
    }

    function renderGroups() {
        if (state.selected >= state.communities.length) {
            state.selected = Math.max(0, state.communities.length - 1);
        }
        renderSummary();
        renderList();
        renderDetail();
    }

    function updateGroup(index, updater) {
        state.communities = state.communities.map((item, idx) => (idx === index ? updater(item) : item));
    }

    function updateSelected(updater) {
        updateGroup(state.selected, updater);
    }

    function removeGroup() {
        const group = state.communities[state.selected];
        if (!group) return;
        const label = group.name || group.id || "сообщество";
        openRemoveGroupModal(label);
    }

    async function handleStatusChange(active, wasActive) {
        const group = state.communities[state.selected];
        if (!group) return;
        const saved = await persistConfig();
        if (!saved) return;
        if (!active) {
            showToast("Сообщество на паузе");
            return;
        }
        if (!wasActive) {
            await postBackfill(group.id, "none", 0);
            showToast("Продолжаем с текущего места: только новые посты");
        }
    }

    function collectPayload() {
        const general = state.config?.general || {};
        const communities = state.communities.map((group) => ({
            id: (group.id || "").trim(),
            name: (group.name || "").trim(),
            active: Boolean(group.active),
            content_types: group.content_types || {
                text: true,
                photo: true,
                video: true,
                audio: false,
                link: true,
            },
        }));

        return {
            general: {
                cron: cronFromUI(),
                posts_limit: parseInt(els.postsCount.value, 10) || 10,
                vk_api_version: general.vk_api_version || "5.199",
                cache_file: general.cache_file || "data/cache.json",
                log_file: general.log_file || "data/logs/poster.log",
                log_level: general.log_level || "INFO",
                log_rotation: general.log_rotation || { max_bytes: 10485760, backup_count: 7 },
                log_retention_days: parseInt(els.logRetention.value, 10) || 7,
                timezone: (els.timezone.value || "").trim() || "Europe/Moscow",
                blocked_keywords: els.filterKeywords.checked
                    ? (els.blockedKeywords.value || "")
                          .split("\n")
                          .map((item) => item.trim())
                          .filter((item) => item.length > 0)
                    : [],
                refresh_avatars: els.refreshAvatars.checked,
                semantic_dedup: {
                    enabled: els.aiEnabled.checked,
                    window_days: parseInt(els.aiWindow.value, 10) || 4,
                },
            },
            vk: {
                token: "",
            },
            telegram: {
                channel_id: els.tgChannel.value.trim(),
            },
            llm: {
                base_url: els.aiBaseUrl.value.trim(),
                model: els.aiModel.value.trim(),
                prompt: els.aiPrompt.value.trim(),
            },
            communities,
        };
    }

    async function persistConfig() {
        const payload = collectPayload();
        try {
            const res = await fetch("/api/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (!res.ok) {
                const detail = await res.json().catch(() => ({}));
                showToast(apiErrorMessage(detail?.detail, "Ошибка сохранения"), true);
                return false;
            }
            return true;
        } catch (err) {
            showToast("Не удалось сохранить конфиг", true);
            return false;
        }
    }

    async function saveConfig() {
        const saved = await persistConfig();
        if (!saved) {
            return;
        }
        showToast("Конфиг сохранён");
        await loadConfig();
    }

    async function loadConfig() {
        try {
            const res = await fetch("/api/config");
            if (!res.ok) throw new Error("Failed");
            const data = await res.json();
            state.config = data;
            state.avatarCache = data.avatar_cache || {};
            if (els.projectVersion) {
                els.projectVersion.textContent = data.version ? `v${data.version}` : "";
            }
            state.communities = (data.communities || []).map((item) => {
                const cache = state.avatarCache[(item.id || "").toLowerCase()];
                return {
                    ...item,
                    name: item.name || cache?.name || item.id,
                    icon: item.icon || cache?.photo,
                    content_types: item.content_types || {
                        text: true,
                        photo: true,
                        video: true,
                        audio: false,
                        link: true,
                    },
                };
            });

            updateCronUI(data.general?.cron || "*/10 * * * *");
            els.postsCount.value = data.general?.posts_limit || 10;
            els.logRetention.value = data.general?.log_retention_days || 7;
            els.timezone.value = data.general?.timezone || "Europe/Moscow";
            els.refreshAvatars.checked = data.general?.refresh_avatars !== false;
            els.blockedKeywords.value = (data.general?.blocked_keywords || []).join("\n");
            els.filterKeywords.checked = (data.general?.blocked_keywords || []).length > 0;
            els.tgChannel.value = data.telegram?.channel_id || "";

            const dedup = data.general?.semantic_dedup || {};
            els.aiEnabled.checked = Boolean(dedup.enabled);
            els.aiWindow.value = dedup.window_days || 4;
            els.aiBaseUrl.value = data.llm?.base_url || "";
            els.aiModel.value = data.llm?.model || "";
            els.aiPrompt.value = data.llm?.prompt || "";
            resetModelList();

            state.selected = 0;
            state.query = "";
            if (els.groupSearch) {
                els.groupSearch.value = "";
            }
            renderGroups();
        } catch {
            showToast("Не удалось загрузить конфиг", true);
        }
    }

    async function fetchCommunityInfo(value) {
        const res = await fetch(`/api/community_info?value=${encodeURIComponent(value)}`);
        if (!res.ok) throw new Error("Failed");
        return res.json();
    }

    const DEFAULT_TYPES = {
        text: true,
        photo: true,
        video: true,
        audio: false,
        link: true,
    };

    const AMOUNT_PRESETS = {
        posts: [3, 7, 10, 20, 50],
        days: [1, 2, 3, 5, 7],
    };

    const DEFAULT_AMOUNT = {
        posts: 10,
        days: 3,
    };

    function openAddGroup() {
        if (!els.addGroupModal) return;
        resetAddGroupForm();
        els.addGroupModal.classList.remove("hidden");
        els.addGroupModal.setAttribute("aria-hidden", "false");
        els.newGroupInput.focus();
    }

    function closeAddGroup() {
        if (!els.addGroupModal) return;
        els.addGroupModal.classList.add("hidden");
        els.addGroupModal.setAttribute("aria-hidden", "true");
    }

    function resetAddGroupForm() {
        if (els.newGroupInput) els.newGroupInput.value = "";
        if (els.addGroupPreview) {
            els.addGroupPreview.classList.add("hidden");
            els.addGroupPreview.innerHTML = "";
        }
        state.addTypes = { ...DEFAULT_TYPES };
        setAddScope("none");
        renderAddTypes();
    }

    function renderAddTypes() {
        if (!els.addGroupTypes) return;
        els.addGroupTypes.innerHTML = TYPE_META.map(
            (type) => `
            <button type="button" class="tg${state.addTypes[type.key] ? " on" : ""}" data-add-type="${type.key}">
                ${svgIcon(type.icon)}${type.label}
            </button>`,
        ).join("");
    }

    function setAddScope(scope) {
        state.addScope = scope;
        if (els.addGroupScope) {
            els.addGroupScope.querySelectorAll("[data-scope]").forEach((btn) => {
                btn.classList.toggle("on", btn.dataset.scope === scope);
            });
        }
        const chips = els.addGroupAmountChips;
        if (!chips) return;
        const presets = AMOUNT_PRESETS[scope];
        if (!presets) {
            chips.classList.add("hidden");
            chips.innerHTML = "";
            return;
        }
        if (!presets.includes(state.addAmount)) {
            state.addAmount = DEFAULT_AMOUNT[scope];
        }
        chips.classList.remove("hidden");
        chips.innerHTML = presets
            .map(
                (amount) => `
                <button type="button" data-amount="${amount}" class="${amount === state.addAmount ? "on" : ""}">${amount}</button>`,
            )
            .join("");
    }

    function addScopeValue() {
        if (state.addScope === "posts" || state.addScope === "days") {
            return state.addAmount;
        }
        return 0;
    }

    let previewTimer = null;

    function scheduleAddPreview() {
        if (previewTimer) clearTimeout(previewTimer);
        previewTimer = setTimeout(previewAddGroup, 400);
    }

    async function previewAddGroup() {
        if (!els.addGroupPreview) return;
        const raw = els.newGroupInput.value.trim();
        if (!raw) {
            els.addGroupPreview.classList.add("hidden");
            els.addGroupPreview.innerHTML = "";
            return;
        }
        els.addGroupPreview.classList.remove("hidden");
        els.addGroupPreview.innerHTML = '<span class="hint">Проверяем ссылку…</span>';
        try {
            const info = await fetchCommunityInfo(raw);
            const name = info?.name || raw;
            const photo = info?.photo ? `<img src="${escapeHtml(info.photo)}" alt="">` : "";
            const note = info?.name ? "сообщество найдено в VK" : "не удалось проверить — добавим как есть";
            els.addGroupPreview.innerHTML = `
                <div class="avatar sm">${photo}${escapeHtml(name.slice(0, 2).toUpperCase())}</div>
                <div class="md-item-text">
                    <div class="name">${escapeHtml(name)}</div>
                    <div class="hint">${note}</div>
                </div>
            `;
        } catch {
            els.addGroupPreview.innerHTML = '<span class="hint">Не удалось проверить ссылку — добавим как есть</span>';
        }
    }

    async function postBackfill(id, mode, value) {
        try {
            const res = await fetch("/api/backfill", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ id, mode, value }),
            });
            return res.ok;
        } catch {
            return false;
        }
    }

    async function addGroup() {
        const raw = els.newGroupInput.value.trim();
        if (!raw) {
            showToast("Укажите ссылку на сообщество", true);
            return;
        }
        els.addGroupBtn.disabled = true;
        try {
            let info = null;
            try {
                info = await fetchCommunityInfo(raw);
            } catch {
                info = null;
            }
            const group = {
                id: info?.id || raw,
                name: info?.name || raw,
                active: true,
                icon: info?.photo || "",
                content_types: { ...state.addTypes },
            };
            state.communities.push(group);
            state.selected = state.communities.length - 1;
            state.query = "";
            if (els.groupSearch) {
                els.groupSearch.value = "";
            }

            const scope = state.addScope;
            const amount = addScopeValue();
            const saved = await persistConfig();
            if (saved) {
                await postBackfill(group.id, scope, amount);
            }

            closeAddGroup();
            renderGroups();
            showToast(saved ? "Сообщество добавлено" : "Сообщество добавлено, но конфиг не сохранён", !saved);
        } finally {
            els.addGroupBtn.disabled = false;
        }
    }

    function openRemoveGroupModal(label) {
        if (!els.removeGroupModal || !els.removeGroupName) return;
        els.removeGroupName.textContent = label;
        els.removeGroupModal.classList.remove("hidden");
        els.removeGroupModal.setAttribute("aria-hidden", "false");
    }

    function closeRemoveGroupModal() {
        if (!els.removeGroupModal) return;
        els.removeGroupModal.classList.add("hidden");
        els.removeGroupModal.setAttribute("aria-hidden", "true");
    }

    async function confirmRemoveGroup() {
        const group = state.communities[state.selected];
        if (!group) return;
        
        try {
            // Вызвать API для удаления сообщества
            const res = await fetch(`/api/community/${encodeURIComponent(group.id)}`, {
                method: "DELETE",
            });
            
            if (!res.ok) {
                const detail = await res.json().catch(() => ({}));
                showToast(apiErrorMessage(detail?.detail, "Ошибка удаления"), true);
                return;
            }
            
            // Удалить из локального состояния
            state.communities = state.communities.filter((_, idx) => idx !== state.selected);
            closeRemoveGroupModal();
            showToast("Сообщество удалено");
            
            // Перезагрузить конфигурацию, чтобы обновить состояние
            await loadConfig();
        } catch (err) {
            showToast("Не удалось удалить сообщество", true);
        }
    }

    const EVENT_META = {
        published: { icon: "✅", label: "Опубликован", cls: "pub" },
        duplicate: { icon: "❌", label: "Дубль, пропущен", cls: "dup" },
        blocked: { icon: "❌", label: "Заблокировано словом", cls: "block" },
        skipped_type: { icon: "❌", label: "Пропущен по типу контента", cls: "skip" },
        failed: { icon: "❌", label: "Ошибка публикации", cls: "fail" },
        backfill: { icon: "📚", label: "Дозаливка", cls: "back" },
        backfill_failed: { icon: "❌", label: "Дозаливка не удалась", cls: "fail" },
    };

    const LEVEL_META = {
        DEBUG: { short: "отл", cls: "debug" },
        INFO: { short: "инфо", cls: "info" },
        WARNING: { short: "важно", cls: "warning" },
        ERROR: { short: "ошибка", cls: "error" },
        CRITICAL: { short: "крит", cls: "error" },
    };

    function formatRelativeTime(tsSeconds) {
        const ts = Number(tsSeconds) || 0;
        if (!ts) return "";
        const diff = Math.max(0, Math.floor(Date.now() / 1000 - ts));
        if (diff < 60) return "только что";
        if (diff < 3600) return `${Math.floor(diff / 60)} мин назад`;
        if (diff < 86400) return `${Math.floor(diff / 3600)} ч назад`;
        return `${Math.floor(diff / 86400)} дн назад`;
    }

    function formatJournalTime(tsSeconds) {
        const ts = Number(tsSeconds) || 0;
        if (!ts) return "";
        const d = new Date(ts * 1000);
        const pad = (n) => String(n).padStart(2, "0");
        return `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }

    function journalChip(cls, icon, count, title) {
        if (!count) return "";
        return `<span class="jc ${cls}" title="${escapeHtml(title)}">${icon} ${count}</span>`;
    }

    function renderJournalEvent(event) {
        const meta = EVENT_META[event.kind] || { icon: "•", label: event.kind || "событие", cls: "" };
        const link = event.link
            ? `<a class="je-link" href="${escapeHtml(event.link)}" target="_blank" rel="noopener">пост ${event.post_id}</a>`
            : (event.post_id ? `<span class="je-post">пост ${event.post_id}</span>` : "");
        const detail = event.kind === "backfill"
            ? `<span class="je-detail">режим «${escapeHtml(event.mode || "")}», ${event.count || 0}</span>`
            : "";
        const reason = event.reason ? `<span class="je-reason">${escapeHtml(event.reason)}</span>` : "";
        const text = event.text ? `<span class="je-text">${escapeHtml(event.text)}</span>` : "";
        return `<div class="journal-event je-${meta.cls}">
            <span class="je-icon">${meta.icon}</span>
            <span class="je-label">${escapeHtml(meta.label)}</span>
            ${link}${detail}${reason}${text}
        </div>`;
    }

    function renderJournalCommunity(community) {
        const skipped = (community.blocked || 0) + (community.skipped_by_type || 0) + (community.dedup_skipped || 0);
        const chips = [
            journalChip("fetch", "📥", community.fetched, "Получено из VK"),
            journalChip("new", "✨", community.new, "Новых постов"),
            journalChip("pub", "✅", community.published, "Опубликовано"),
            journalChip("skip", "❌", skipped, "Пропущено"),
            journalChip("err", "❌", community.failed, "Ошибок публикации"),
            journalChip("queue", "🕓", community.pending, "Осталось в очереди"),
        ].join("");
        const stateChip = community.status === "paused"
            ? '<span class="jc paused">пауза</span>'
            : (chips || '<span class="jc muted">без изменений</span>');
        const error = community.error ? `<div class="comm-error">${escapeHtml(community.error)}</div>` : "";
        const events = (community.events || []).map(renderJournalEvent).join("");
        return `<div class="comm-block">
            <div class="comm-row">
                <div class="comm-name">${escapeHtml(community.name || community.id || "Без названия")}</div>
                <div class="comm-chips">${stateChip}</div>
                ${events ? '<button type="button" class="comm-toggle" data-toggle-events>Подробнее</button>' : ""}
            </div>
            ${error}
            ${events ? `<div class="comm-events hidden">${events}</div>` : ""}
        </div>`;
    }

    function renderJournal(runs) {
        if (!els.journalContainer) return;
        if (!runs || !runs.length) {
            els.journalContainer.innerHTML =
                '<div class="journal-empty">Запусков ещё не было. Журнал появится после первого запуска публикации.</div>';
            return;
        }
        els.journalContainer.innerHTML = runs.map((run) => {
            const ok = run.ok !== false && !run.error;
            const statusLabel = run.error ? "Сбой" : (ok ? "Успешно" : "С ошибками");
            const when = formatJournalTime(run.finished || run.started);
            const rel = formatRelativeTime(run.finished || run.started);
            const duration = run.duration != null ? `${run.duration} с` : "";
            const communities = (run.communities || []).map(renderJournalCommunity).join("");
            const runError = run.error ? `<div class="run-error">${escapeHtml(run.error)}</div>` : "";
            const empty = !run.communities || !run.communities.length;
            return `<div class="run-card ${ok ? "ok" : "err"}">
                <div class="run-head">
                    <span class="run-badge">${escapeHtml(statusLabel)}</span>
                    <span class="run-when">${escapeHtml(when)} · ${escapeHtml(rel)}</span>
                    <span class="run-meta">${escapeHtml(duration)}${run.version ? " · v" + escapeHtml(run.version) : ""}</span>
                </div>
                ${runError}
                ${empty && !run.error ? '<div class="journal-empty">Сообществ нет.</div>' : communities}
            </div>`;
        }).join("");
    }

    async function loadJournal() {
        try {
            const res = await fetch("/api/journal?runs=10");
            const data = await res.json();
            renderJournal(data.runs || []);
        } catch {
            if (els.journalContainer) {
                els.journalContainer.innerHTML = '<div class="journal-empty">Не удалось загрузить журнал.</div>';
            }
        }
    }

    function filterLogEntriesClient(entries) {
        const order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];
        let result = entries || [];
        if (state.logLevel && order.includes(state.logLevel)) {
            const min = order.indexOf(state.logLevel);
            result = result.filter((entry) => order.indexOf((entry.level || "").toUpperCase()) >= min);
        }
        const query = (state.logQuery || "").trim().toLowerCase();
        if (query) {
            result = result.filter((entry) => String(entry.message || "").toLowerCase().includes(query));
        }
        return result;
    }

    function renderLogEntries() {
        if (!els.logsContainer) return;
        const entries = filterLogEntriesClient(state.logEntries);
        if (!entries.length) {
            els.logsContainer.innerHTML = '<div class="log-empty">Записей нет</div>';
            return;
        }
        els.logsContainer.innerHTML = entries.map((entry) => {
            const level = (entry.level || "").toUpperCase();
            const meta = LEVEL_META[level];
            const badge = meta ? `<span class="log-level log-${meta.cls}">${meta.short}</span>` : "";
            const time = entry.ts ? entry.ts.slice(11, 19) : "";
            return `<div class="log-entry">
                <span class="log-time">${escapeHtml(time)}</span>
                ${badge}
                <span class="log-message">${escapeHtml(entry.message || "")}</span>
            </div>`;
        }).join("");
    }

    async function loadLogs() {
        try {
            const res = await fetch("/api/logs?lines=500");
            const data = await res.json();
            state.logEntries = data.entries || [];
            renderLogEntries();
        } catch {
            showToast("Не удалось загрузить логи", true);
        }
    }

    function setLogsTab(tab) {
        state.logsTab = tab === "log" ? "log" : "journal";
        if (els.journalPanel) els.journalPanel.classList.toggle("hidden", state.logsTab !== "journal");
        if (els.logPanel) els.logPanel.classList.toggle("hidden", state.logsTab !== "log");
        els.logsTabs.forEach((btn) => btn.classList.toggle("active", btn.dataset.logsTab === state.logsTab));
        if (state.logsTab === "log") {
            loadLogs();
        } else {
            loadJournal();
        }
        syncLogAutoRefresh();
    }

    function stopLogAutoRefresh() {
        if (state.logAutoTimer) {
            clearInterval(state.logAutoTimer);
            state.logAutoTimer = null;
        }
    }

    function syncLogAutoRefresh() {
        stopLogAutoRefresh();
        if (els.logAutoRefresh && els.logAutoRefresh.checked && state.logsTab === "log") {
            state.logAutoTimer = setInterval(loadLogs, 5000);
        }
    }

    function renderRunStatus(runs) {
        if (!els.runStatus) return;
        const run = runs && runs[0];
        if (!run) {
            els.runStatus.classList.add("hidden");
            return;
        }
        const communities = run.communities || [];
        const failedCommunities = communities.filter((item) => item.status === "error").length;
        const failedPosts = communities.reduce((sum, item) => sum + (item.failed || 0), 0);
        const ok = run.ok !== false && !run.error;
        let label = "Всё в порядке";
        if (run.error) {
            label = "Сбой запуска";
        } else if (failedCommunities) {
            label = `Ошибки: ${failedCommunities}`;
        } else if (failedPosts) {
            label = `Ошибок публикации: ${failedPosts}`;
        }
        els.runStatus.className = "run-status " + (ok ? "ok" : "err");
        els.runStatus.innerHTML =
            `<span class="run-status-dot"></span><span>${escapeHtml(label)}</span>` +
            `<span class="run-status-when">${escapeHtml(formatRelativeTime(run.finished || run.started))}</span>`;
    }

    async function loadRunStatus() {
        try {
            const res = await fetch("/api/journal?runs=1");
            const data = await res.json();
            renderRunStatus(data.runs || []);
        } catch {
            if (els.runStatus) els.runStatus.classList.add("hidden");
        }
    }

    els.saveSettingsBtn.addEventListener("click", saveConfig);

    if (els.aiBaseUrl) {
        els.aiBaseUrl.addEventListener("input", resetModelList);
    }

    if (els.aiModel) {
        els.aiModel.addEventListener("focus", () => fetchModels(false));
        els.aiModel.addEventListener("input", () => {
            if (!els.aiModelList.classList.contains("hidden")) renderModelList();
        });
        els.aiModel.addEventListener("blur", () => {
            setTimeout(closeModelList, 120);
        });
    }

    if (els.aiModelList) {
        els.aiModelList.addEventListener("mousedown", (e) => {
            const option = e.target.closest("[data-model]");
            if (!option) return;
            e.preventDefault();
            selectModel(option.dataset.model);
        });
    }

    document.addEventListener("click", (e) => {
        if (els.aiModelCombobox && !e.target.closest(".combobox")) closeModelList();
    });

    els.interval.addEventListener("change", () => {
        if (els.interval.value === "custom") {
            els.cronCustomRow.classList.remove("hidden");
        } else {
            els.cronCustomRow.classList.add("hidden");
            els.cronCustom.value = cronMap[els.interval.value] || "*/10 * * * *";
        }
    });

    if (els.addGroupToggle) {
        els.addGroupToggle.addEventListener("click", () => openAddGroup());
    }

    if (els.addGroupScope) {
        els.addGroupScope.addEventListener("click", (e) => {
            const btn = e.target.closest("[data-scope]");
            if (btn) {
                setAddScope(btn.dataset.scope);
            }
        });
    }

    if (els.addGroupAmountChips) {
        els.addGroupAmountChips.addEventListener("click", (e) => {
            const btn = e.target.closest("[data-amount]");
            if (!btn) return;
            state.addAmount = parseInt(btn.dataset.amount, 10);
            els.addGroupAmountChips.querySelectorAll("[data-amount]").forEach((chip) => {
                chip.classList.toggle("on", chip === btn);
            });
        });
    }

    if (els.addGroupTypes) {
        els.addGroupTypes.addEventListener("click", (e) => {
            const btn = e.target.closest("[data-add-type]");
            if (!btn) return;
            const key = btn.dataset.addType;
            state.addTypes[key] = !state.addTypes[key];
            btn.classList.toggle("on", state.addTypes[key]);
        });
    }

    if (els.newGroupInput) {
        els.newGroupInput.addEventListener("input", scheduleAddPreview);
    }

    if (els.addGroupModal) {
        els.addGroupModal.addEventListener("click", (e) => {
            if (e.target === els.addGroupModal) {
                closeAddGroup();
            }
        });
    }

    if (els.removeGroupModal) {
        els.removeGroupModal.addEventListener("click", (e) => {
            if (e.target === els.removeGroupModal) {
                closeRemoveGroupModal();
            }
        });
    }

    if (els.cancelAddGroupBtn) {
        els.cancelAddGroupBtn.addEventListener("click", () => closeAddGroup());
    }

    if (els.cancelRemoveGroupBtn) {
        els.cancelRemoveGroupBtn.addEventListener("click", () => closeRemoveGroupModal());
    }

    if (els.confirmRemoveGroupBtn) {
        els.confirmRemoveGroupBtn.addEventListener("click", confirmRemoveGroup);
    }

    els.addGroupBtn.addEventListener("click", addGroup);
    els.newGroupInput.addEventListener("keypress", (e) => {
        if (e.key === "Enter") addGroup();
    });

    if (els.groupSearch) {
        els.groupSearch.addEventListener("input", () => {
            state.query = els.groupSearch.value;
            renderList();
        });
    }

    els.groupsList.addEventListener("click", (e) => {
        const item = e.target.closest(".md-item");
        if (!item) return;
        state.selected = parseInt(item.dataset.index, 10);
        renderList();
        renderDetail();
    });

    els.groupDetail.addEventListener("click", (e) => {
        const statusBtn = e.target.closest("[data-active]");
        if (statusBtn) {
            const active = statusBtn.dataset.active === "1";
            const wasActive = Boolean(state.communities[state.selected]?.active);
            updateSelected((item) => ({ ...item, active }));
            renderGroups();
            handleStatusChange(active, wasActive);
            return;
        }

        const typeBtn = e.target.closest("[data-type]");
        if (typeBtn) {
            const key = typeBtn.dataset.type;
            let enabled = false;
            updateSelected((item) => {
                enabled = !(item.content_types && item.content_types[key]);
                return { ...item, content_types: { ...item.content_types, [key]: enabled } };
            });
            typeBtn.classList.toggle("on", enabled);
            return;
        }

        const removeBtn = e.target.closest("[data-action='remove']");
        if (removeBtn) {
            removeGroup();
        }
    });

    function openLogs() {
        if (els.logsModal) {
            els.logsModal.classList.remove("hidden");
            els.logsModal.setAttribute("aria-hidden", "false");
        }
        setLogsTab(state.logsTab || "journal");
    }

    function closeLogs() {
        stopLogAutoRefresh();
        if (els.logsModal) {
            els.logsModal.classList.add("hidden");
            els.logsModal.setAttribute("aria-hidden", "true");
        }
    }

    function openAi() {
        if (els.aiModal) {
            els.aiModal.classList.remove("hidden");
            els.aiModal.setAttribute("aria-hidden", "false");
        }
        closeModelList();
    }

    function closeAi() {
        closeModelList();
        if (els.aiModal) {
            els.aiModal.classList.add("hidden");
            els.aiModal.setAttribute("aria-hidden", "true");
        }
    }

    if (els.openAiBtn) {
        els.openAiBtn.addEventListener("click", () => openAi());
    }

    if (els.closeAiBtn) {
        els.closeAiBtn.addEventListener("click", () => closeAi());
    }

    if (els.saveAiBtn) {
        els.saveAiBtn.addEventListener("click", saveConfig);
    }

    if (els.aiModal) {
        els.aiModal.addEventListener("click", (e) => {
            if (e.target === els.aiModal) {
                closeAi();
            }
        });
    }

    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && els.aiModelList && !els.aiModelList.classList.contains("hidden")) {
            closeModelList();
            return;
        }
        if (e.key === "Escape") {
            closeLogs();
            closeAi();
            closeAddGroup();
            closeRemoveGroupModal();
        }
    });

    if (els.openLogsBtn) {
        els.openLogsBtn.addEventListener("click", () => openLogs());
    }

    if (els.closeLogsBtn) {
        els.closeLogsBtn.addEventListener("click", () => closeLogs());
    }

    if (els.refreshLogsBtn) {
        els.refreshLogsBtn.addEventListener("click", () => {
            if (state.logsTab === "log") {
                loadLogs();
            } else {
                loadJournal();
            }
        });
    }

    els.logsTabs.forEach((btn) => {
        btn.addEventListener("click", () => setLogsTab(btn.dataset.logsTab));
    });

    if (els.logLevelFilters) {
        els.logLevelFilters.addEventListener("click", (e) => {
            const btn = e.target.closest("[data-level]");
            if (!btn) return;
            state.logLevel = btn.dataset.level || "";
            els.logLevelFilters.querySelectorAll(".log-filter").forEach((item) => {
                item.classList.toggle("active", item === btn);
            });
            renderLogEntries();
        });
    }

    if (els.logSearch) {
        els.logSearch.addEventListener("input", () => {
            state.logQuery = els.logSearch.value || "";
            renderLogEntries();
        });
    }

    if (els.logAutoRefresh) {
        els.logAutoRefresh.addEventListener("change", syncLogAutoRefresh);
    }

    if (els.journalContainer) {
        els.journalContainer.addEventListener("click", (e) => {
            const toggle = e.target.closest("[data-toggle-events]");
            if (!toggle) return;
            const block = toggle.closest(".comm-block");
            const events = block ? block.querySelector(".comm-events") : null;
            if (!events) return;
            const nowHidden = events.classList.toggle("hidden");
            toggle.textContent = nowHidden ? "Подробнее" : "Свернуть";
        });
    }

    if (els.runStatus) {
        els.runStatus.addEventListener("click", () => {
            state.logsTab = "journal";
            openLogs();
        });
    }

    if (els.logsModal) {
        els.logsModal.addEventListener("click", (e) => {
            if (e.target === els.logsModal) {
                closeLogs();
            }
        });
    }

    loadConfig();
    loadRunStatus();
});
