(() => {
    const body = document.body;
    const toggle = document.querySelector('[data-menu-toggle]');
    const menu = document.querySelector('[data-mobile-menu]');

    const closeMenu = () => {
        if (!toggle || !menu) return;
        toggle.setAttribute('aria-expanded', 'false');
        toggle.setAttribute('aria-label', 'Открыть меню');
        menu.classList.remove('is-open');
        body.classList.remove('menu-open');
    };

    if (toggle && menu) {
        toggle.addEventListener('click', () => {
            const willOpen = toggle.getAttribute('aria-expanded') !== 'true';
            toggle.setAttribute('aria-expanded', String(willOpen));
            toggle.setAttribute('aria-label', willOpen ? 'Закрыть меню' : 'Открыть меню');
            menu.classList.toggle('is-open', willOpen);
            body.classList.toggle('menu-open', willOpen);
        });
        menu.querySelectorAll('a').forEach((link) => link.addEventListener('click', closeMenu));
        window.addEventListener('resize', () => {
            if (window.innerWidth > 760) closeMenu();
        });
    }

    document.querySelectorAll('.faq-item button').forEach((button) => {
        button.addEventListener('click', () => {
            const item = button.closest('.faq-item');
            const list = item.closest('.faq-list');
            const opening = !item.classList.contains('is-open');

            list.querySelectorAll('.faq-item').forEach((other) => {
                other.classList.remove('is-open');
                const otherButton = other.querySelector('button');
                otherButton.setAttribute('aria-expanded', 'false');
                otherButton.querySelector('b').textContent = '+';
            });

            if (opening) {
                item.classList.add('is-open');
                button.setAttribute('aria-expanded', 'true');
                button.querySelector('b').textContent = '×';
            }
        });
    });

    const setupSlider = (name, autoplay = 0) => {
        const root = document.querySelector(`[data-slider="${name}"]`);
        const dotsRoot = document.querySelector(`[data-dots="${name}"]`);
        if (!root || !dotsRoot) return;

        const slides = [...root.children];
        let current = Math.max(0, slides.findIndex((slide) => slide.classList.contains('is-active')));
        let timer;

        const dots = slides.map((_, index) => {
            const dot = document.createElement('button');
            dot.type = 'button';
            dot.setAttribute('aria-label', `Показать слайд ${index + 1}`);
            dot.addEventListener('click', () => show(index, true));
            dotsRoot.append(dot);
            return dot;
        });

        const restart = () => {
            if (!autoplay || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
            clearInterval(timer);
            timer = setInterval(() => show(current + 1), autoplay);
        };

        const show = (index, manual = false) => {
            current = (index + slides.length) % slides.length;
            slides.forEach((slide, slideIndex) => slide.classList.toggle('is-active', slideIndex === current));
            dots.forEach((dot, dotIndex) => dot.classList.toggle('is-active', dotIndex === current));
            if (manual) restart();
        };

        document.querySelector(`[data-slider-prev="${name}"]`)?.addEventListener('click', () => show(current - 1, true));
        document.querySelector(`[data-slider-next="${name}"]`)?.addEventListener('click', () => show(current + 1, true));
        show(current);
        restart();
    };

    setupSlider('hero', 5200);
    setupSlider('services', 0);
})();
