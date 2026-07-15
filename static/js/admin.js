(() => {
    const body = document.body;
    document.querySelectorAll('[data-admin-sidebar-open]').forEach((button) => button.addEventListener('click', () => body.classList.add('admin-sidebar-open')));
    document.querySelectorAll('[data-admin-sidebar-close]').forEach((button) => button.addEventListener('click', () => body.classList.remove('admin-sidebar-open')));
    document.addEventListener('keydown', (event) => { if (event.key === 'Escape') body.classList.remove('admin-sidebar-open'); });

    document.querySelectorAll('[data-admin-file-input]').forEach((input) => input.addEventListener('change', () => {
        const label = input.closest('label')?.querySelector('[data-admin-file-name]');
        const file = input.files?.[0];
        if (label && file) label.textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} МБ`;
    }));

    document.querySelectorAll('form[data-confirm]').forEach((form) => form.addEventListener('submit', (event) => {
        if (!window.confirm(form.dataset.confirm || 'Подтвердите действие.')) event.preventDefault();
    }));

    document.querySelectorAll('form[data-submit-lock]').forEach((form) => form.addEventListener('submit', (event) => {
        const submitter = event.submitter;
        if (!(submitter instanceof HTMLButtonElement)) return;
        window.setTimeout(() => {
            submitter.disabled = true;
            submitter.setAttribute('aria-busy', 'true');
        }, 0);
    }));
})();
