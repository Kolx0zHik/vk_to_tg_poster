document.addEventListener("DOMContentLoaded", () => {
    const state = {
        config: null,
        communities: [],
        avatarCache: {},
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
        groupsList: document.getElementById("groupsList"),

        logsContainer: document.getElementById("logsContainer"),
        logsModal: document.getElementById("logsModal"),
        openLogsBtn: document.getElementById("openLogsBtn"),
        closeLogsBtn: document.getElementById("closeLogsBtn"),
        refreshLogsBtn: document.getElementById("refreshLogsBtn"),

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

    function renderGroups() {
        if (!state.communities.length) {
            els.groupsList.innerHTML = '<div class="empty-state">Список групп пуст. Добавьте первую группу для начала работы.</div>';
            return;
        }

        els.groupsList.innerHTML = state.communities
            .map((group, idx) => {
                const avatar = group.icon
                    ? `<img src="${group.icon}" alt="avatar">`
                    : (group.name || group.id || "VK").slice(0, 2).toUpperCase();
                return `
                    <div class="group-item" data-index="${idx}">
                        <div class="group-head">
                            <div class="group-info">
                                <div class="group-avatar">${avatar}</div>
                                <div class="group-fields">
                                    <input type="text" data-field="name" value="${group.name || ""}" placeholder="Имя сообщества">
                                    <input type="text" data-field="id" value="${group.id || ""}" placeholder="ID или ссылка">
                                </div>
                            </div>
                            <label class="switch-label">
                                <span class="label-text">Активно</span>
                                <input type="checkbox" data-field="active" ${group.active ? "checked" : ""}>
                                <span class="slider"></span>
                            </label>
                            <button class="btn-icon-danger" data-action="remove" title="Удалить">
                                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/></svg>
                            </button>
                        </div>
                        <div class="group-types">
                            ${["text", "photo", "video", "audio", "link"]
                                .map(
                                    (type) => `
                                        <label class="type-chip">
                                            <input type="checkbox" data-type="${type}" ${group.content_types?.[type] ? "checked" : ""}>
                                            <span>${type}</span>
                                        </label>
                                    `,
                                )
                                .join("")}
                        </div>
                    </div>
                `;
            })
            .join("");
    }

    function updateGroup(index, updater) {
        state.communities = state.communities.map((item, idx) => (idx === index ? updater(item) : item));
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
                log_file: general.log_file || "logs/poster.log",
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
            const newGroup = {
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
            };
            state.communities.push(newGroup);
            renderGroups();
            els.newGroupInput.value = "";
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

    els.addGroupBtn.addEventListener("click", addGroup);
    els.newGroupInput.addEventListener("keypress", (e) => {
        if (e.key === "Enter") addGroup();
    });

    els.groupsList.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-action='remove']");
        if (!btn) return;
        const groupEl = btn.closest(".group-item");
        const idx = parseInt(groupEl.dataset.index, 10);
        state.communities.splice(idx, 1);
        renderGroups();
    });

    els.groupsList.addEventListener("input", (e) => {
        const groupEl = e.target.closest(".group-item");
        if (!groupEl) return;
        const idx = parseInt(groupEl.dataset.index, 10);
        const field = e.target.getAttribute("data-field");
        if (!field) return;
        updateGroup(idx, (item) => ({
            ...item,
            [field]: e.target.type === "checkbox" ? e.target.checked : e.target.value,
        }));
    });

    els.groupsList.addEventListener("change", (e) => {
        const groupEl = e.target.closest(".group-item");
        if (!groupEl) return;
        const idx = parseInt(groupEl.dataset.index, 10);
        const type = e.target.getAttribute("data-type");
        if (!type) return;
        updateGroup(idx, (item) => ({
            ...item,
            content_types: {
                ...item.content_types,
                [type]: e.target.checked,
            },
        }));
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
