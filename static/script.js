document.addEventListener("DOMContentLoaded", () => {
    const state = {
        config: null,
        communities: [],
        avatarCache: {},
        selected: 0,
        query: "",
    };

    const els = {
        interval: document.getElementById("interval"),
        cronCustomRow: document.getElementById("cronCustomRow"),
        cronCustom: document.getElementById("cronCustom"),
        filterKeywords: document.getElementById("filterKeywords"),
        refreshAvatars: document.getElementById("refreshAvatars"),
        postsCount: document.getElementById("postsCount"),
        logRetention: document.getElementById("logRetention"),
        blockedKeywords: document.getElementById("blockedKeywords"),
        saveSettingsBtn: document.getElementById("saveSettingsBtn"),

        vkToken: document.getElementById("vkToken"),
        tgBotToken: document.getElementById("tgBotToken"),
        tgChannel: document.getElementById("tgChannel"),
        saveTokensBtn: document.getElementById("saveTokensBtn"),
        togglePasswordBtns: document.querySelectorAll(".toggle-password"),

        newGroupInput: document.getElementById("newGroupInput"),
        addGroupBtn: document.getElementById("addGroupBtn"),
        addGroupToggle: document.getElementById("addGroupToggle"),
        addGroupForm: document.getElementById("addGroupForm"),
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

        tokensModal: document.getElementById("tokensModal"),
        openTokensBtn: document.getElementById("openTokensBtn"),
        closeTokensBtn: document.getElementById("closeTokensBtn"),
        projectVersion: document.getElementById("projectVersion"),

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

    function setMaskedToken(input, masked) {
        if (masked) {
            input.value = "********";
            input.dataset.masked = "true";
        } else {
            input.value = "";
            input.dataset.masked = "false";
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
            .map(({ group, index }) => {
                const url = vkCommunityUrl(group.id);
                const name = escapeHtml(group.name || group.id || "Без названия");
                const label = url
                    ? `<a class="name" href="${escapeHtml(url)}" target="_blank" rel="noopener">${name}<span class="ext">${svgIcon(ICONS.external)}</span></a>`
                    : `<span class="name">${name}</span>`;
                return `
                <div class="md-item${index === state.selected ? " sel" : ""}${group.active ? "" : " paused"}" data-index="${index}">
                    <div class="avatar sm" data-avatar="${index}">${escapeHtml(initials(group))}<span class="dot${group.active ? "" : " paused"}"></span></div>
                    <div class="md-item-text">${label}</div>
                </div>
            `;
            })
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
            <div class="md-block">
                <label>Название</label>
                <input type="text" data-field="name" placeholder="Имя сообщества">
            </div>
            <div class="md-block">
                <label>Сообщество в VK</label>
                <div class="ref-view">
                    ${
                        url
                            ? `<a class="linkish" href="${escapeHtml(url)}" target="_blank" rel="noopener">${svgIcon(ICONS.external)}Открыть в VK</a>`
                            : '<span class="hint">Ссылка недоступна</span>'
                    }
                    <button type="button" class="link-muted" data-action="edit-ref">Изменить</button>
                </div>
                <div class="ref-edit hidden">
                    <input type="text" data-field="id" placeholder="Ссылка на сообщество или название">
                </div>
            </div>
            <div class="md-detail-foot">
                <span class="pill">Изменения сохранит кнопка «Сохранить»</span>
                <button type="button" class="link-danger" data-action="remove">${svgIcon(ICONS.trash)}Удалить сообщество</button>
            </div>
        `;
        els.groupDetail.querySelector('[data-field="name"]').value = group.name || "";
        els.groupDetail.querySelector('[data-field="id"]').value = group.id || "";
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
        if (!window.confirm(`Удалить «${label}» из списка?`)) return;
        state.communities = state.communities.filter((_, idx) => idx !== state.selected);
        renderGroups();
        showToast("Сообщество удалено");
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
                blocked_keywords: els.filterKeywords.checked
                    ? (els.blockedKeywords.value || "")
                          .split("\n")
                          .map((item) => item.trim())
                          .filter((item) => item.length > 0)
                    : [],
                refresh_avatars: els.refreshAvatars.checked,
            },
            vk: {
                token:
                    els.vkToken.dataset.masked === "true" && els.vkToken.value === "********"
                        ? ""
                        : els.vkToken.value.trim(),
            },
            telegram: {
                channel_id: els.tgChannel.value.trim(),
                bot_token:
                    els.tgBotToken.dataset.masked === "true" && els.tgBotToken.value === "********"
                        ? ""
                        : els.tgBotToken.value.trim(),
            },
            communities,
        };
    }

    async function saveConfig() {
        const payload = collectPayload();
        try {
            const res = await fetch("/api/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (!res.ok) {
                const detail = await res.json().catch(() => ({}));
                const message = detail?.detail?.message || "Ошибка сохранения";
                showToast(message, true);
                return;
            }
            showToast("Конфиг сохранён");
            await loadConfig();
        } catch (err) {
            showToast("Не удалось сохранить конфиг", true);
        }
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
            els.refreshAvatars.checked = data.general?.refresh_avatars !== false;
            els.blockedKeywords.value = (data.general?.blocked_keywords || []).join("\n");
            els.filterKeywords.checked = (data.general?.blocked_keywords || []).length > 0;
            setMaskedToken(els.vkToken, Boolean(data.vk?.token_set));
            setMaskedToken(els.tgBotToken, Boolean(data.telegram?.bot_token_set));
            els.tgChannel.value = data.telegram?.channel_id || "";

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

    function openAddGroup() {
        if (!els.addGroupForm) return;
        els.addGroupForm.classList.remove("hidden");
        els.newGroupInput.focus();
    }

    function closeAddGroup() {
        if (!els.addGroupForm) return;
        els.addGroupForm.classList.add("hidden");
    }

    async function addGroup() {
        const raw = els.newGroupInput.value.trim();
        if (!raw) return;
        els.addGroupBtn.disabled = true;
        try {
            let info = null;
            try {
                info = await fetchCommunityInfo(raw);
            } catch {
                info = null;
            }
            state.communities.push({
                id: info?.id || raw,
                name: info?.name || raw,
                active: true,
                icon: info?.photo || "",
                content_types: {
                    text: true,
                    photo: true,
                    video: true,
                    audio: false,
                    link: true,
                },
            });
            state.selected = state.communities.length - 1;
            state.query = "";
            if (els.groupSearch) {
                els.groupSearch.value = "";
            }
            els.newGroupInput.value = "";
            closeAddGroup();
            renderGroups();
            showToast("Сообщество добавлено");
        } finally {
            els.addGroupBtn.disabled = false;
        }
    }

    async function loadLogs() {
        try {
            const res = await fetch("/api/logs?lines=50");
            const data = await res.json();
            els.logsContainer.innerHTML = "";
            const lines = data.lines || [];
            if (!lines.length) {
                els.logsContainer.innerHTML = '<div class="log-entry"><span class="log-message">Логи пусты</span></div>';
                return;
            }
            lines.forEach((line) => {
                els.logsContainer.insertAdjacentHTML(
                    "beforeend",
                    `<div class="log-entry"><span class="log-message">${line.replace(/</g, "&lt;")}</span></div>`,
                );
            });
        } catch {
            showToast("Не удалось загрузить логи", true);
        }
    }

    els.saveSettingsBtn.addEventListener("click", saveConfig);
    els.saveTokensBtn.addEventListener("click", saveConfig);

    els.interval.addEventListener("change", () => {
        if (els.interval.value === "custom") {
            els.cronCustomRow.classList.remove("hidden");
        } else {
            els.cronCustomRow.classList.add("hidden");
            els.cronCustom.value = cronMap[els.interval.value] || "*/10 * * * *";
        }
    });

    els.togglePasswordBtns.forEach((btn) => {
        btn.addEventListener("click", () => {
            const targetId = btn.getAttribute("data-target");
            const input = document.getElementById(targetId);
            const eyeOpen = btn.querySelector(".eye-open");
            const eyeClosed = btn.querySelector(".eye-closed");

            if (input.type === "password") {
                input.type = "text";
                eyeOpen.classList.add("hidden");
                eyeClosed.classList.remove("hidden");
            } else {
                input.type = "password";
                eyeOpen.classList.remove("hidden");
                eyeClosed.classList.add("hidden");
            }
        });
    });

    els.vkToken.addEventListener("input", () => {
        if (els.vkToken.value !== "********") {
            els.vkToken.dataset.masked = "false";
        }
    });

    els.tgBotToken.addEventListener("input", () => {
        if (els.tgBotToken.value !== "********") {
            els.tgBotToken.dataset.masked = "false";
        }
    });

    if (els.addGroupToggle) {
        els.addGroupToggle.addEventListener("click", () => {
            if (els.addGroupForm.classList.contains("hidden")) {
                openAddGroup();
            } else {
                closeAddGroup();
            }
        });
    }

    if (els.cancelAddGroupBtn) {
        els.cancelAddGroupBtn.addEventListener("click", () => {
            els.newGroupInput.value = "";
            closeAddGroup();
        });
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
            updateSelected((item) => ({ ...item, active }));
            renderGroups();
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

        const editRefBtn = e.target.closest("[data-action='edit-ref']");
        if (editRefBtn) {
            const box = els.groupDetail.querySelector(".ref-edit");
            if (box) {
                box.classList.remove("hidden");
                const input = box.querySelector("input");
                if (input) input.focus();
            }
            return;
        }

        const removeBtn = e.target.closest("[data-action='remove']");
        if (removeBtn) {
            removeGroup();
        }
    });

    els.groupDetail.addEventListener("input", (e) => {
        const field = e.target.getAttribute("data-field");
        if (!field) return;
        const value = e.target.value;
        updateSelected((item) => ({ ...item, [field]: value }));
        renderList();
    });

    function openLogs() {
        if (els.logsModal) {
            els.logsModal.classList.remove("hidden");
            els.logsModal.setAttribute("aria-hidden", "false");
        }
        loadLogs();
    }

    function closeLogs() {
        if (els.logsModal) {
            els.logsModal.classList.add("hidden");
            els.logsModal.setAttribute("aria-hidden", "true");
        }
    }

    function openTokens() {
        if (els.tokensModal) {
            els.tokensModal.classList.remove("hidden");
            els.tokensModal.setAttribute("aria-hidden", "false");
        }
    }

    function closeTokens() {
        if (els.tokensModal) {
            els.tokensModal.classList.add("hidden");
            els.tokensModal.setAttribute("aria-hidden", "true");
        }
    }

    if (els.openTokensBtn) {
        els.openTokensBtn.addEventListener("click", () => openTokens());
    }

    if (els.closeTokensBtn) {
        els.closeTokensBtn.addEventListener("click", () => closeTokens());
    }

    if (els.tokensModal) {
        els.tokensModal.addEventListener("click", (e) => {
            if (e.target === els.tokensModal) {
                closeTokens();
            }
        });
    }

    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            closeLogs();
            closeTokens();
        }
    });

    if (els.openLogsBtn) {
        els.openLogsBtn.addEventListener("click", () => openLogs());
    }

    if (els.closeLogsBtn) {
        els.closeLogsBtn.addEventListener("click", () => closeLogs());
    }

    if (els.refreshLogsBtn) {
        els.refreshLogsBtn.addEventListener("click", () => loadLogs());
    }

    if (els.logsModal) {
        els.logsModal.addEventListener("click", (e) => {
            if (e.target === els.logsModal) {
                closeLogs();
            }
        });
    }

    loadConfig();
});
