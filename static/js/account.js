(() => {
    const body = document.body;
    const openButtons = document.querySelectorAll("[data-sidebar-open]");
    const closeButtons = document.querySelectorAll("[data-sidebar-close]");

    const openSidebar = () => body.classList.add("sidebar-open");
    const closeSidebar = () => body.classList.remove("sidebar-open");

    openButtons.forEach((button) => button.addEventListener("click", openSidebar));
    closeButtons.forEach((button) => button.addEventListener("click", closeSidebar));
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") closeSidebar();
    });

    const root = document.querySelector("[data-chart-root]");
    if (!root) return;

    const bars = root.querySelector("[data-chart-bars]");
    const empty = root.querySelector("[data-chart-empty]");
    const buttons = root.querySelectorAll("[data-period]");
    if (!bars) return;

    let chartData = {};
    try {
        chartData = JSON.parse(root.dataset.chart || "{}");
    } catch (_error) {
        chartData = {};
    }

    const renderChart = (period) => {
        const values = Array.isArray(chartData[period]) ? chartData[period] : [];
        bars.innerHTML = "";

        if (!values.length) {
            bars.hidden = true;
            if (empty) empty.hidden = false;
            return;
        }

        bars.hidden = false;
        if (empty) empty.hidden = true;
        const maxValue = Math.max(...values.map((item) => Number(item.value) || 0), 1);

        values.forEach((item) => {
            const column = document.createElement("span");
            column.className = "chart-column";
            column.title = `${item.label}: ${Number(item.value).toLocaleString("ru-RU")}`;

            const bar = document.createElement("span");
            bar.className = "chart-bar";
            bar.style.height = `${Math.max(8, Math.round((Number(item.value) / maxValue) * 112))}px`;

            const label = document.createElement("span");
            label.className = "chart-label";
            label.textContent = item.label;

            column.append(bar, label);
            bars.append(column);
        });
    };

    buttons.forEach((button) => {
        button.addEventListener("click", () => {
            buttons.forEach((item) => item.classList.remove("is-active"));
            button.classList.add("is-active");
            renderChart(button.dataset.period || "30");
        });
    });

    renderChart(root.dataset.defaultPeriod || "30");
})();

(() => {
    const body = document.body;

    const coverInput = document.querySelector("[data-cover-input]");
    const coverImage = document.querySelector("[data-cover-preview-image]");
    const coverPlaceholder = document.querySelector("[data-cover-placeholder]");
    const coverName = document.querySelector("[data-cover-name]");
    let coverObjectUrl = null;

    if (coverInput && coverImage) {
        coverInput.addEventListener("change", () => {
            const file = coverInput.files?.[0];
            if (!file) return;
            if (coverObjectUrl) URL.revokeObjectURL(coverObjectUrl);
            coverObjectUrl = URL.createObjectURL(file);
            coverImage.src = coverObjectUrl;
            coverImage.hidden = false;
            if (coverPlaceholder) coverPlaceholder.hidden = true;
            if (coverName) {
                const megabytes = file.size / (1024 * 1024);
                coverName.textContent = `${file.name} · ${megabytes.toFixed(megabytes >= 10 ? 0 : 1)} МБ`;
                coverName.classList.remove("is-error");
            }
        });
    }

    const modal = document.querySelector("[data-track-modal]");
    const modalOpeners = document.querySelectorAll("[data-track-modal-open]");
    const modalClosers = document.querySelectorAll("[data-track-modal-close]");

    const openModal = () => {
        if (!modal) return;
        modal.classList.add("is-open");
        modal.setAttribute("aria-hidden", "false");
        body.classList.add("track-modal-open");
        window.setTimeout(() => modal.querySelector("input:not([type='file'])")?.focus(), 50);
    };

    const closeModal = () => {
        if (!modal) return;
        modal.classList.remove("is-open");
        modal.setAttribute("aria-hidden", "true");
        body.classList.remove("track-modal-open");
    };

    modalOpeners.forEach((button) => button.addEventListener("click", openModal));
    modalClosers.forEach((button) => button.addEventListener("click", closeModal));
    if (modal?.classList.contains("is-open")) body.classList.add("track-modal-open");

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && modal?.classList.contains("is-open")) closeModal();
    });

    document.querySelectorAll("[data-audio-file-input]").forEach((input) => {
        input.addEventListener("change", () => {
            const file = input.files?.[0];
            const label = input.closest(".audio-upload-control")?.querySelector("[data-audio-file-name]");
            if (file && label) label.textContent = file.name;
        });
    });

    document.querySelectorAll("form[data-confirm]").forEach((form) => {
        form.addEventListener("submit", (event) => {
            const message = form.dataset.confirm || "Подтвердите действие.";
            if (!window.confirm(message)) event.preventDefault();
        });
    });

    document.querySelectorAll("form[data-submit-lock]").forEach((form) => {
        form.addEventListener("submit", (event) => {
            const submitters = new Set(form.querySelectorAll("button[type='submit']"));
            if (event.submitter instanceof HTMLButtonElement) submitters.add(event.submitter);
            submitters.forEach((button) => {
                button.disabled = true;
                button.setAttribute("aria-busy", "true");
                const original = button.textContent.trim();
                button.dataset.originalText = original;
                button.textContent = "Сохраняем…";
            });
        });
    });

    let activeAudio = null;
    let activeButton = null;

    const resetAudioButton = () => {
        if (activeButton) activeButton.classList.remove("is-playing");
        activeButton = null;
    };

    document.querySelectorAll("[data-audio-play]").forEach((button) => {
        button.addEventListener("click", async () => {
            const url = button.dataset.audioUrl;
            if (!url) return;

            if (activeAudio && activeButton === button) {
                if (activeAudio.paused) {
                    try {
                        await activeAudio.play();
                        button.classList.add("is-playing");
                    } catch (_error) {
                        window.alert("Не удалось воспроизвести аудиофайл.");
                    }
                } else {
                    activeAudio.pause();
                    button.classList.remove("is-playing");
                }
                return;
            }

            if (activeAudio) {
                activeAudio.pause();
                activeAudio.currentTime = 0;
                resetAudioButton();
            }

            activeAudio = new Audio(url);
            activeButton = button;
            activeAudio.addEventListener("ended", resetAudioButton, { once: true });
            activeAudio.addEventListener("error", () => {
                resetAudioButton();
                window.alert("Аудиофайл недоступен. Проверьте, что он загружен корректно.");
            }, { once: true });
            try {
                await activeAudio.play();
                button.classList.add("is-playing");
            } catch (_error) {
                resetAudioButton();
                window.alert("Не удалось воспроизвести аудиофайл.");
            }
        });
    });
})();

(() => {
    const faqItems = document.querySelectorAll(".support-faq-item");
    faqItems.forEach((item) => {
        item.addEventListener("toggle", () => {
            if (!item.open) return;
            faqItems.forEach((other) => {
                if (other !== item) other.open = false;
            });
        });
    });

    const messageList = document.querySelector(".support-message-list");
    if (messageList) messageList.scrollTop = messageList.scrollHeight;
})();
