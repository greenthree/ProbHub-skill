        (() => {
            const saved = localStorage.getItem('probhub-theme');
            const preferred = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
            document.documentElement.dataset.theme = saved === 'light' || saved === 'dark' ? saved : preferred;
        })();
